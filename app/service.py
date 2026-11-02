"""Sync state machine: selected existing Diadoc group persists before each invite."""
import asyncio
from .db import database, one, rows, utcnow, audit
from .diadoc import DiadocError, guid

DEFAULT_GROUP = '00000000-0000-0000-0000-000000000000'
STATUS_LABELS = {'InvitesMe': 'Входящее', 'IsInvitedByMe': 'Исходящее',
                 'IsMyCounteragent': 'Принято', 'Rejected': 'Отклонено'}


class Service:
    def __init__(self, api):
        self.api = api
        self.sync_lock = asyncio.Lock()

    async def discover_organizations(self):
        orgs = await self.api.organizations()
        count = 0
        with database() as c:
            for org in orgs:
                for box in org.get('Boxes', []):
                    raw_id = box.get('BoxIdGuid')
                    if not raw_id:
                        continue
                    box_id = guid(raw_id)
                    name = box.get('Title') or org.get('ShortName') or org.get('FullName') or box_id
                    c.execute('''INSERT INTO organizations(box_id,name,inn,kpp) VALUES (?,?,?,?)
                        ON CONFLICT(box_id) DO UPDATE SET name=excluded.name,
                        inn=excluded.inn,kpp=excluded.kpp''',
                        (box_id, name, org.get('Inn',''), org.get('Kpp','')))
                    count += 1
        return count

    async def sync_all(self):
        if self.sync_lock.locked():
            return {'busy': True}
        async with self.sync_lock:
            results = []
            for org in rows('SELECT * FROM organizations WHERE enabled=1 ORDER BY name'):
                try:
                    await self.sync_one(org['box_id'])
                    results.append((org['name'], 'ok'))
                except Exception as exc:
                    # One organization's error must not prevent syncing all other boxes.
                    message = str(exc) if isinstance(exc, (DiadocError, ValueError)) else 'Внутренняя ошибка синхронизации'
                    with database() as c:
                        c.execute('UPDATE organizations SET last_error=? WHERE box_id=?', (message, org['box_id']))
                    results.append((org['name'], message))
            return {'results': results}

    async def sync_one(self, box_id):
        box_id = guid(box_id)
        org = one('SELECT * FROM organizations WHERE box_id=? AND enabled=1', (box_id,))
        if not org:
            raise ValueError('Организация не найдена или отключена')
        # Resolve async AcquireCounteragent tasks first, otherwise auto-accepts
        # can appear as an external connection before the queued group is linked.
        await self.resolve_tasks(box_id)
        groups = await self.api.groups(box_id)
        snapshots = []
        for status in ('InvitesMe', 'IsInvitedByMe', 'IsMyCounteragent', 'Rejected'):
            snapshots.extend(await self.api.counterparties(box_id, status))
        with database() as c:
            c.execute('DELETE FROM groups WHERE box_id=?', (box_id,))
            for group in groups:
                c.execute('INSERT OR REPLACE INTO groups(box_id,group_id,name) VALUES(?,?,?)',
                          (box_id, group['CounteragentGroupId'], group['Name']))
            for cp in snapshots:
                org_data = cp.get('Organization') or {}
                cp_boxes = org_data.get('Boxes') or []
                cp_box = (cp_boxes[0].get('BoxIdGuid') if cp_boxes else '') or cp.get('CounteragentBoxId')
                if not cp_box:
                    continue
                try:
                    cp_box = guid(cp_box)
                except ValueError:
                    continue
                status = cp.get('CurrentStatus') or cp.get('Status') or ''
                prev = c.execute('SELECT status,direction FROM counterparties WHERE box_id=? AND cp_box_id=?',
                                 (box_id, cp_box)).fetchone()
                direction = ('incoming' if status == 'InvitesMe' else
                             'outgoing' if status == 'IsInvitedByMe' else
                             (prev['direction'] if prev else 'unknown'))
                pending = c.execute('''SELECT direction FROM intents WHERE box_id=? AND cp_box_id=? AND assign_state='pending'
                    ORDER BY id DESC LIMIT 1''', (box_id, cp_box)).fetchone()
                if pending:
                    direction = pending['direction']
                group_id = cp.get('CounteragentGroupId') or DEFAULT_GROUP
                invitation_doc = cp.get('InvitationDocumentId') or {}
                doc_str = ':'.join(str(invitation_doc.get(k,'')) for k in ('MessageId','EntityId'))
                c.execute('''INSERT INTO counterparties(box_id,cp_box_id,name,inn,kpp,direction,status,
                           current_group_id,invitation_doc_id,last_event_ticks,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(box_id,cp_box_id)
                           DO UPDATE SET name=excluded.name,inn=excluded.inn,kpp=excluded.kpp,
                           direction=excluded.direction,status=excluded.status,
                           current_group_id=excluded.current_group_id,
                           invitation_doc_id=excluded.invitation_doc_id,
                           last_event_ticks=excluded.last_event_ticks,updated_at=excluded.updated_at''',
                          (box_id, cp_box, org_data.get('ShortName') or org_data.get('FullName') or cp_box,
                           org_data.get('Inn',''), org_data.get('Kpp',''), direction, status, group_id,
                           doc_str, str(cp.get('LastEventTimestampTicks','')), utcnow()))
                # Notify only for an observed transition or an app-originated invitation.
                if status == 'IsMyCounteragent':
                    an_intent = c.execute('''SELECT id FROM intents WHERE box_id=? AND cp_box_id=?
                        AND direction='outgoing' ORDER BY id DESC LIMIT 1''', (box_id, cp_box)).fetchone()
                    if direction == 'outgoing' and ((prev and prev['status'] != status) or (not prev and an_intent)):
                        c.execute('''INSERT INTO notifications(box_id,cp_box_id,kind,text,created_at)
                                     VALUES(?,?,?,?,?)''', (box_id, cp_box, 'accepted_outgoing',
                                     'Контрагент принял исходящее приглашение', utcnow()))
                    # New partners who bypassed our accept/send screen need manual group triage.
                    waiting_same_inn = c.execute("SELECT 1 FROM intents WHERE box_id=? AND cp_inn=? AND assign_state='pending' LIMIT 1",
                         (box_id, org_data.get('Inn',''))).fetchone()
                    if org['last_sync'] and not prev and not pending and not waiting_same_inn and group_id == DEFAULT_GROUP:
                        c.execute('''INSERT INTO notifications(box_id,cp_box_id,kind,text,created_at)
                                     VALUES(?,?,?,?,?)''', (box_id, cp_box, 'external_unassigned',
                                     'Партнерские отношения установлены вне приложения: выберите группу Диадока', utcnow()))
            c.execute('UPDATE organizations SET last_sync=?,last_error=? WHERE box_id=?',
                      (utcnow(), '', box_id))
        await self.apply_groups(box_id)

    async def resolve_tasks(self, box_id):
        for intent in rows('''SELECT * FROM intents WHERE box_id=? AND send_state='processing'
                              AND task_id<>'' ORDER BY id DESC LIMIT 200''', (box_id,)):
            try:
                result = await self.api.acquire_result(box_id, intent['task_id'])
                if not result:
                    continue
                new_box = result.get('BoxId') or intent['cp_box_id']
                new_box = guid(new_box)
                with database() as c:
                    c.execute('''UPDATE intents SET cp_box_id=?,send_state='sent',error='',updated_at=?
                                 WHERE id=?''', (new_box, utcnow(), intent['id']))
                    audit(c, box_id, new_box, 'invitation_sent', intent['direction'])
            except DiadocError as exc:
                with database() as c:
                    c.execute('UPDATE intents SET error=? WHERE id=?', (str(exc), intent['id']))

    async def apply_groups(self, box_id):
        intents = rows('''SELECT i.*, cp.status, cp.current_group_id FROM intents i
                          LEFT JOIN counterparties cp ON cp.box_id=i.box_id AND cp.cp_box_id=i.cp_box_id
                          WHERE i.box_id=? AND i.assign_state='pending'
                          ORDER BY i.id DESC LIMIT 250''', (box_id,))
        for intent in intents:
            if intent['status'] != 'IsMyCounteragent' or not intent['cp_box_id']:
                continue
            if intent['current_group_id'] == intent['target_group_id']:
                with database() as c:
                    c.execute('UPDATE intents SET assign_state=?,updated_at=?,error=? WHERE id=?',
                              ('assigned', utcnow(), '', intent['id']))
                continue
            # A group could have been deleted after the invitation was sent: never silently use default.
            group = one('SELECT group_id FROM groups WHERE box_id=? AND group_id=?',
                        (box_id, intent['target_group_id']))
            if not group:
                # Do not create or modify groups in the customer's existing Diadoc structure.
                with database() as c:
                    c.execute('UPDATE intents SET error=? WHERE id=?',
                              ('Выбранная группа больше не существует в Диадоке. Нужна ручная смена группы.', intent['id']))
                continue
            try:
                await self.api.assign_group(box_id, intent['cp_box_id'], intent['target_group_id'])
                with database() as c:
                    c.execute('''UPDATE intents SET assign_state='assigned',send_state='sent',
                                 error='',updated_at=? WHERE id=?''', (utcnow(), intent['id']))
                    c.execute('''UPDATE counterparties SET current_group_id=?
                                 WHERE box_id=? AND cp_box_id=?''',
                              (intent['target_group_id'], box_id, intent['cp_box_id']))
                    audit(c, box_id, intent['cp_box_id'], 'group_assigned', intent['target_group_id'])
            except DiadocError as exc:
                with database() as c:
                    c.execute('UPDATE intents SET error=? WHERE id=?', (str(exc), intent['id']))

    def verify_group(self, box_id, group_id):
        box_id, group_id = guid(box_id), guid(group_id)
        org = one('SELECT * FROM organizations WHERE box_id=? AND enabled=1', (box_id,))
        group = one('SELECT * FROM groups WHERE box_id=? AND group_id=?', (box_id, group_id))
        if not org or not group or group_id == DEFAULT_GROUP:
            raise ValueError('Выберите существующую группу контрагентов Диадока для нужной организации.')
        return org, group

    async def invite(self, box_id, group_id, cp_box_id, inn, cp_name, message=''):
        org, group = self.verify_group(box_id, group_id)
        inn = inn.strip()
        if not inn.isdigit() or len(inn) not in (10,12):
            raise ValueError('Укажите корректный ИНН (10 или 12 цифр).')
        cp_box_id = guid(cp_box_id) if cp_box_id else ''
        with database() as c:
            current = c.execute('''SELECT id FROM intents WHERE box_id=? AND cp_box_id=?
                AND cp_box_id<>'' AND assign_state='pending' ORDER BY id DESC LIMIT 1''',
                (box_id, cp_box_id)).fetchone()
            if current:
                raise ValueError('Для этого ящика уже есть приглашение с ожидающим назначением группы.')
        with database() as c:
            now = utcnow()
            cursor = c.execute('''INSERT INTO intents(box_id,cp_box_id,cp_inn,cp_name,direction,
                target_group_id,send_state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (box_id, cp_box_id, inn, cp_name[:255], 'outgoing', group_id, 'requesting', now, now))
            intent_id = cursor.lastrowid
            audit(c, box_id, cp_box_id, 'outgoing_requested', f'ИНН {inn}, группа {group["name"]}')
        try:
            task_id = await self.api.acquire(box_id, cp_box_id, inn, message)
            with database() as c:
                c.execute('''UPDATE intents SET send_state='processing',task_id=?,updated_at=?
                             WHERE id=?''', (task_id, utcnow(), intent_id))
            return intent_id
        except DiadocError:
            with database() as c:
                c.execute('''UPDATE intents SET send_state='unknown',error=?,updated_at=? WHERE id=?''',
                          ('Не удалось подтвердить отправку. Проверьте статус в Диадоке перед повтором.', utcnow(), intent_id))
            raise

    async def accept(self, box_id, cp_box_id, group_id):
        org, group = self.verify_group(box_id, group_id)
        cp_box_id = guid(cp_box_id)
        cp = one('''SELECT * FROM counterparties WHERE box_id=? AND cp_box_id=?''', (box_id, cp_box_id))
        if not cp or cp['status'] != 'InvitesMe':
            raise ValueError('Входящее приглашение больше не ожидает принятия. Обновите список.')
        pending = one('''SELECT id FROM intents WHERE box_id=? AND cp_box_id=? AND assign_state='pending' ''',
                      (box_id, cp_box_id))
        if pending:
            raise ValueError('Принятие этого приглашения уже запущено.')
        with database() as c:
            now = utcnow()
            cursor = c.execute('''INSERT INTO intents(box_id,cp_box_id,cp_inn,cp_name,direction,
                target_group_id,send_state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (box_id, cp_box_id, cp['inn'], cp['name'], 'incoming', group_id, 'requesting', now, now))
            intent_id = cursor.lastrowid
            audit(c, box_id, cp_box_id, 'incoming_accepted_requested', group['name'])
        try:
            task = await self.api.acquire(box_id, cp_box_id)
            with database() as c:
                c.execute('''UPDATE intents SET task_id=?,send_state='processing',updated_at=? WHERE id=?''',
                          (task, utcnow(), intent_id))
            return intent_id
        except DiadocError:
            with database() as c:
                c.execute('''UPDATE intents SET send_state='unknown',error=?,updated_at=? WHERE id=?''',
                          ('Ответ Диадока не подтверждён. Проверьте приглашение, в том числе наличие вложения с подписью.',
                           utcnow(), intent_id))
            raise
