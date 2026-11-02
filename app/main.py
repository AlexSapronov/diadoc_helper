import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlencode, quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.exceptions import HTTPException

from . import config
from .db import database, init_db, one, rows, utcnow, audit
from .security import check_password
from .diadoc import Diadoc, DiadocError, TokenManager, guid
from .service import Service, STATUS_LABELS, DEFAULT_GROUP

config.validate_config()
init_db()
tokens = TokenManager()
service = Service(Diadoc(tokens))


async def background_loop():
    while True:
        try:
            await service.sync_all()
        except Exception:
            # Surface per-org errors on screen; keep the worker alive.
            pass
        await asyncio.sleep(config.SYNC_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(background_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title='ЭДО Контроль', docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.APP_SESSION_SECRET,
                   https_only=config.SESSION_HTTPS_ONLY, max_age=8*3600, same_site='lax')
from pathlib import Path
APP_DIR = Path(__file__).resolve().parent
app.mount('/static', StaticFiles(directory=str(APP_DIR / 'static')), name='static')
templates = Jinja2Templates(directory=str(APP_DIR / 'templates'))
templates.env.globals['status_name'] = lambda s: STATUS_LABELS.get(s, s)


def own_orgs():
    return rows('SELECT * FROM organizations ORDER BY name')


def require_login(request):
    if not request.session.get('authenticated'):
        raise HTTPException(303, headers={'Location': '/login'})


def csrf(request):
    token = request.session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        request.session['csrf_token'] = token
    return token


def check_csrf(request, token):
    expected = request.session.get('csrf_token')
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(403, detail='Ошибка защиты формы. Обновите страницу.')


def view(request, template, **context):
    data = {'request': request, 'csrf': csrf(request), 'orgs': own_orgs(),
            'configured': bool(config.DIA_CLIENT_ID and (config.DIA_REFRESH_TOKEN or one("SELECT key FROM secrets WHERE key='diadoc_refresh'"))),
            'user': request.session.get('authenticated')}
    data.update(context)
    return templates.TemplateResponse(request, template, data)


def redirect(path='/', msg=None, error=None):
    query = {}
    if msg:
        query['notice'] = msg
    if error:
        query['error'] = error
    return RedirectResponse(path + (('&' if '?' in path else '?') + urlencode(query) if query else ''), status_code=303)


def mail_href(org, cp):
    recipients = [a.strip() for a in (org.get('mail_recipients') or '').split(',') if a.strip()]
    text = f'Новый контрагент в ЭДО {org["name"]} - {cp["name"]} - {cp["inn"]}'
    return 'mailto:' + ','.join(quote(a, safe='@.+-_') for a in recipients) + '?' + urlencode(
        {'subject': text, 'body': text})


@app.get('/login')
async def login_page(request: Request):
    if request.session.get('authenticated'):
        return redirect()
    return view(request, 'login.html')


@app.post('/login')
async def login(request: Request, username: str = Form(), password: str = Form(), csrf_token: str = Form()):
    check_csrf(request, csrf_token)
    if username == config.APP_USERNAME and check_password(password, config.APP_PASSWORD_HASH):
        request.session.clear()
        request.session['authenticated'] = True
        request.session['csrf_token'] = secrets.token_urlsafe(32)
        return redirect()
    await asyncio.sleep(0.6)
    return view(request, 'login.html', error='Неверный логин или пароль')


@app.post('/logout')
async def logout(request: Request, csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    request.session.clear()
    return redirect('/login')


@app.get('/health')
async def health():
    return {'status': 'ok'}


@app.get('/')
async def index(request: Request, box: str = '', status: str = '', q: str = ''):
    require_login(request)
    orgs = [o for o in own_orgs() if o['enabled']]
    if box and box not in [o['box_id'] for o in orgs]:
        raise HTTPException(404)
    values = []
    predicates = ['1=1']
    if box:
        predicates.append('cp.box_id=?')
        values.append(box)
    if status in STATUS_LABELS:
        predicates.append('cp.status=?')
        values.append(status)
    if q:
        predicates.append('(cp.name LIKE ? OR cp.inn LIKE ?)')
        values.extend([f'%{q[:80]}%', f'%{q[:80]}%'])
    cps = rows(f'''SELECT cp.*, o.name AS org_name, o.mail_recipients,
                      g.name AS group_name, i.target_group_id AS selected_group_id,
                      i.assign_state, i.send_state, i.error AS intent_error,
                      i.id AS intent_id
               FROM counterparties cp JOIN organizations o ON o.box_id=cp.box_id
               LEFT JOIN groups g ON g.box_id=cp.box_id AND g.group_id=cp.current_group_id
               LEFT JOIN intents i ON i.id=(SELECT MAX(ii.id) FROM intents ii
                 WHERE ii.box_id=cp.box_id AND ii.cp_box_id=cp.cp_box_id)
               WHERE o.enabled=1 AND {' AND '.join(predicates)}
               ORDER BY CASE cp.status WHEN 'InvitesMe' THEN 0 WHEN 'IsInvitedByMe' THEN 1
                    WHEN 'IsMyCounteragent' THEN 2 ELSE 3 END, cp.updated_at DESC
               LIMIT 2000''', tuple(values))
    for cp in cps:
        cp['mail_href'] = mail_href({'name': cp['org_name'], 'mail_recipients': cp['mail_recipients']}, cp)
    summaries = rows('''SELECT cp.status,COUNT(*) AS count FROM counterparties cp
        JOIN organizations o ON o.box_id=cp.box_id WHERE o.enabled=1 GROUP BY cp.status''')
    stats = {s['status']: s['count'] for s in summaries}
    pending = rows('''SELECT i.*,o.name AS org_name FROM intents i
        JOIN organizations o ON o.box_id=i.box_id WHERE o.enabled=1 AND i.assign_state='pending'
        ORDER BY i.id DESC LIMIT 100''')
    unread = one('SELECT COUNT(*) AS n FROM notifications WHERE read_at=?', ('',))['n']
    org_groups = {}
    for g in rows('SELECT * FROM groups ORDER BY name'):
        org_groups.setdefault(g['box_id'], []).append(g)
    return view(request, 'index.html', counterparties=cps, stats=stats,
                selected_box=box, selected_status=status, search=q,
                pending=pending, unread=unread, status_labels=STATUS_LABELS, org_groups=org_groups)


@app.get('/organizations')
async def organizations_page(request: Request):
    require_login(request)
    return view(request, 'organizations.html')


@app.post('/organizations/discover')
async def discover(request: Request, csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    try:
        number = await service.discover_organizations()
        return redirect('/organizations', msg=f'Получено ящиков: {number}. Включите нужные.')
    except (DiadocError, ValueError) as exc:
        return redirect('/organizations', error=str(exc))


@app.post('/organizations/{box_id}/enable')
async def toggle_organization(request: Request, box_id: str, enabled: str = Form('0'), csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    box_id = guid(box_id)
    with database() as c:
        c.execute('UPDATE organizations SET enabled=? WHERE box_id=?', (int(enabled == '1'), box_id))
    return redirect('/organizations', msg='Состояние организации сохранено')


@app.post('/organizations/{box_id}/recipients')
async def recipients(request: Request, box_id: str, emails: str = Form(''), csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    box_id = guid(box_id)
    import re
    recipients_list = [x.strip() for x in re.split(r'[,;\s]+', emails) if x.strip()]
    if len(recipients_list) > 20 or any(not re.fullmatch(r'[^@\s,;]+@[^@\s,;.]+(?:\.[^@\s,;.]+)+', x) for x in recipients_list):
        return redirect('/organizations', error='Укажите не более 20 корректных адресов электронной почты')
    with database() as c:
        c.execute('UPDATE organizations SET mail_recipients=? WHERE box_id=?',
                  (','.join(recipients_list), box_id))
    return redirect('/organizations', msg='Получатели письма сохранены')


@app.post('/sync')
async def sync(request: Request, csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    report = await service.sync_all()
    if report.get('busy'):
        return redirect(error='Синхронизация уже идёт')
    failed = [o for o,s in report['results'] if s != 'ok']
    if failed:
        return redirect(error='Ошибки синхронизации: ' + ', '.join(failed[:5]))
    return redirect(msg=f'Синхронизировано организаций: {len(report["results"])}')


@app.get('/invite')
async def invite_form(request: Request, box: str = '', inn: str = '', kpp: str = ''):
    require_login(request)
    orgs = [o for o in own_orgs() if o['enabled']]
    if not box and orgs:
        box = orgs[0]['box_id']
    if box and box not in [o['box_id'] for o in orgs]:
        raise HTTPException(404)
    group_list = rows('SELECT * FROM groups WHERE box_id=? AND group_id<>? ORDER BY name',
                      (box, DEFAULT_GROUP)) if box else []
    search_results = []
    err = ''
    if inn:
        if not inn.isdigit() or len(inn) not in (10, 12):
            err = 'ИНН должен состоять из 10 или 12 цифр'
        else:
            try:
                for o in await service.api.find_by_inn(inn, kpp):
                    for b in o.get('Boxes', []):
                        if b.get('BoxIdGuid'):
                            search_results.append({'box_id': b['BoxIdGuid'],
                                'name': b.get('Title') or o.get('ShortName') or o.get('FullName'),
                                'inn': o.get('Inn',''), 'kpp': o.get('Kpp',''),
                                'roaming': o.get('IsRoaming', False)})
            except DiadocError as exc:
                err = str(exc)
    return view(request, 'invite.html', selected_box=box, inn=inn, kpp=kpp,
                groups=group_list, results=search_results, lookup_error=err)


@app.post('/invite/send')
async def invite_send(request: Request, box_id: str = Form(), group_id: str = Form(),
                      inn: str = Form(), cp_box_id: str = Form(''),
                      cp_name: str = Form(''), message: str = Form(''), csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    try:
        # Resolve identity again server-side so the submitted name/INN cannot impersonate a box.
        if cp_box_id:
            matches = await service.api.find_by_inn(inn)
            real = [(o, b) for o in matches for b in o.get('Boxes', [])
                    if b.get('BoxIdGuid') and guid(b['BoxIdGuid']) == guid(cp_box_id)]
            if not real:
                raise ValueError('Ящик контрагента не соответствует указанному ИНН.')
            real_org, real_box = real[0]
            cp_name = real_box.get('Title') or real_org.get('ShortName') or cp_name
        await service.invite(box_id, group_id, cp_box_id, inn, cp_name, message)
        return redirect(msg='Отправка приглашения запущена. Группа будет назначена после принятия.')
    except (ValueError, DiadocError) as exc:
        return redirect('/invite?'+urlencode({'box':box_id, 'inn':inn}), error=str(exc))


@app.get('/accept/{box_id}/{cp_box_id}')
async def accept_form(request: Request, box_id: str, cp_box_id: str):
    require_login(request)
    box_id, cp_box_id = guid(box_id), guid(cp_box_id)
    cp = one('SELECT * FROM counterparties WHERE box_id=? AND cp_box_id=? AND status=?',
             (box_id, cp_box_id, 'InvitesMe'))
    org = one('SELECT * FROM organizations WHERE box_id=? AND enabled=1', (box_id,))
    if not cp or not org:
        raise HTTPException(404)
    groups = rows('SELECT * FROM groups WHERE box_id=? AND group_id<>? ORDER BY name',
                  (box_id, DEFAULT_GROUP))
    return view(request, 'accept.html', cp=cp, org=org, groups=groups,
                email_link=mail_href(org, cp))


@app.post('/accept/{box_id}/{cp_box_id}')
async def accept(request: Request, box_id: str, cp_box_id: str, group_id: str = Form(),
                 csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    try:
        await service.accept(box_id, cp_box_id, group_id)
        return redirect(msg='Принятие запущено. Назначение группы произойдёт после подтверждения Диадока.')
    except (ValueError, DiadocError) as exc:
        return redirect(f'/accept/{quote(box_id)}/{quote(cp_box_id)}', error=str(exc))


@app.get('/notifications')
async def notifications(request: Request):
    require_login(request)
    items = rows('''SELECT n.*,o.name AS org_name,o.mail_recipients,cp.name AS cp_name,
                          cp.inn AS cp_inn, cp.status, cp.current_group_id
                   FROM notifications n JOIN organizations o ON o.box_id=n.box_id
                   LEFT JOIN counterparties cp ON cp.box_id=n.box_id AND cp.cp_box_id=n.cp_box_id
                   ORDER BY n.id DESC LIMIT 200''')
    for n in items:
        n['mail_href'] = mail_href({'name': n['org_name'], 'mail_recipients': n['mail_recipients']},
                                   {'name': n['cp_name'] or n['cp_box_id'], 'inn': n['cp_inn'] or ''})
    return view(request, 'notifications.html', notifications=items)


@app.post('/notifications/{notification_id}/read')
async def read_notification(request: Request, notification_id: int, csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    with database() as c:
        c.execute('UPDATE notifications SET read_at=? WHERE id=?', (utcnow(), notification_id))
    return redirect('/notifications')


@app.get('/audit')
async def audit_page(request: Request):
    require_login(request)
    return view(request, 'audit.html', logs=rows('SELECT * FROM audit ORDER BY id DESC LIMIT 250'))

@app.post('/counterparty/{box_id}/{cp_box_id}/group')
async def assign_existing(request: Request, box_id: str, cp_box_id: str,
                          group_id: str = Form(), csrf_token: str = Form()):
    require_login(request)
    check_csrf(request, csrf_token)
    try:
        org, group = service.verify_group(box_id, group_id)
        cp_box_id = guid(cp_box_id)
        cp = one('SELECT * FROM counterparties WHERE box_id=? AND cp_box_id=? AND status=?',
                 (box_id, cp_box_id, 'IsMyCounteragent'))
        if not cp:
            raise ValueError('Партнерство ещё не установлено.')
        await service.api.assign_group(box_id, cp_box_id, group_id)
        with database() as c:
            c.execute('UPDATE counterparties SET current_group_id=? WHERE box_id=? AND cp_box_id=?',
                      (group_id, box_id, cp_box_id))
            # A manual override must supersede any outstanding automatic assignment.
            c.execute("UPDATE intents SET target_group_id=?,assign_state='assigned',error='',updated_at=? "
                      "WHERE box_id=? AND cp_box_id=? AND assign_state='pending'",
                      (group_id, utcnow(), box_id, cp_box_id))
            c.execute("UPDATE notifications SET read_at=? WHERE box_id=? AND cp_box_id=? AND kind='external_unassigned'",
                      (utcnow(), box_id, cp_box_id))
            audit(c, box_id, cp_box_id, 'group_assigned_manually', group['name'])
        return redirect(msg='Группа сохранена в Диадоке')
    except (ValueError, DiadocError) as exc:
        return redirect(error=str(exc))
