import re
from fastapi.testclient import TestClient
from app.main import app, mail_href


def test_mailto_encodes_text_and_recipients():
    link = mail_href({'name':'ООО «Наше»','mail_recipients':'first@example.ru,second@example.ru'},
                     {'name':'ООО «Клиент»','inn':'7701234567'})
    assert link.startswith('mailto:first@example.ru,second@example.ru?')
    assert 'subject=' in link and 'body=' in link
    assert '%C2%AB' in link


def test_login_and_csrf_required():
    with TestClient(app) as client:
        no_csrf = client.post('/login',data={'username':'admin','password':'test-admin-password-12345'})
        assert no_csrf.status_code == 422
        page = client.get('/login')
        assert page.status_code == 200
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post('/login',data={'username':'admin','password':'test-admin-password-12345',
                                              'csrf_token':token},follow_redirects=True)
        assert response.status_code == 200
        assert 'Приглашения ЭДО' in response.text
        assert client.get('/organizations').status_code == 200


def test_populated_dashboard_renders_groups_and_action():
    from app.db import database, init_db, utcnow
    init_db()
    a = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    cp = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    g = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc'
    with database() as c:
        c.execute('INSERT OR REPLACE INTO organizations(box_id,name,inn,enabled,mail_recipients) VALUES(?,?,?,?,?)',
                  (a,'ООО «Наше предприятие»','1111111111',1,'manager@example.com'))
        c.execute('INSERT OR REPLACE INTO groups(box_id,group_id,name) VALUES(?,?,?)',
                  (a,g,'Клиенты'))
        c.execute('''INSERT OR REPLACE INTO counterparties(box_id,cp_box_id,name,inn,status,
                     direction,current_group_id,updated_at) VALUES(?,?,?,?,?,?,?,?)''',
                  (a,cp,'ООО «Клиент»','2222222222','IsMyCounteragent','outgoing',g,utcnow()))
    with TestClient(app) as client:
        login_page=client.get('/login')
        token=re.search(r'name="csrf_token" value="([^"]+)"',login_page.text).group(1)
        client.post('/login',data={'username':'admin','password':'test-admin-password-12345','csrf_token':token})
        result=client.get('/?box='+a)
        assert result.status_code==200
        assert 'ООО «Клиент»' in result.text
        assert 'Клиенты' in result.text
        assert 'Изменить группу' in result.text
        assert 'mailto:manager@example.com' in result.text
