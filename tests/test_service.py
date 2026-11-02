import pytest
from app.db import database, init_db, one, rows
from app.service import Service

A='11111111-1111-4111-8111-111111111111'
B='22222222-2222-4222-8222-222222222222'
CP='33333333-3333-4333-8333-333333333333'
GROUP='44444444-4444-4444-8444-444444444444'

class FakeDiadoc:
    def __init__(self):
        self.relationships = {'A': 'InvitesMe'}
        self.group_assignments = []
        self.tasks = {}
        self.current_group = '00000000-0000-0000-0000-000000000000'
        self.fail_group = False

    async def organizations(self):
        return [{'ShortName': 'ООО Наша', 'Inn':'1111111111', 'Kpp':'111111111',
                 'Boxes':[{'BoxIdGuid':A,'Title':'ООО Наша'}, {'BoxIdGuid':B,'Title':'ООО Вторая'}]}]

    async def groups(self, box_id):
        return [{'CounteragentGroupId':GROUP,'Name':'Поставщики'},
                {'CounteragentGroupId':'00000000-0000-0000-0000-000000000000','Name':'По умолчанию'}]

    async def counterparties(self, box_id, status):
        if box_id != A or self.relationships['A'] != status:
            return []
        return [{'Organization':{'ShortName':'ООО Клиент','Inn':'1234567890',
                 'Boxes':[{'BoxIdGuid':CP}]}, 'CurrentStatus': status,
                 'CounteragentGroupId':self.current_group}]

    async def acquire(self, own_box, cp_box='', inn='', message=''):
        task_id='55555555-5555-4555-8555-555555555555'
        self.tasks[task_id]=cp_box or CP
        if self.relationships['A']=='InvitesMe':
            self.relationships['A']='IsMyCounteragent'
        else:
            self.relationships['A']='IsInvitedByMe'
        return task_id

    async def acquire_result(self, own_box, task_id):
        return {'BoxId':self.tasks[task_id]}

    async def assign_group(self, own_box, cp_box, group_id):
        if self.fail_group:
            from app.diadoc import DiadocError
            raise DiadocError('temporary failure')
        self.group_assignments.append((own_box,cp_box,group_id))
        self.current_group = group_id


@pytest.mark.asyncio
async def test_incoming_requires_group_and_assigns_after_accept():
    init_db()
    api = FakeDiadoc()
    svc = Service(api)
    assert await svc.discover_organizations() == 2
    with database() as c:
        c.execute('UPDATE organizations SET enabled=1 WHERE box_id=?',(A,))
    await svc.sync_one(A)
    with pytest.raises(ValueError):
        svc.verify_group(A,'00000000-0000-0000-0000-000000000000')
    assert one('SELECT status FROM counterparties WHERE box_id=? AND cp_box_id=?',(A,CP))['status']=='InvitesMe'
    intent_id=await svc.accept(A,CP,GROUP)
    assert intent_id > 0
    await svc.sync_one(A)
    assert one('SELECT assign_state FROM intents WHERE id=?',(intent_id,))['assign_state']=='assigned'
    assert api.group_assignments == [(A,CP,GROUP)]


@pytest.mark.asyncio
async def test_retries_group_after_temporary_failure():
    init_db()
    api = FakeDiadoc()
    svc = Service(api)
    await svc.discover_organizations()
    with database() as c:
        c.execute('UPDATE organizations SET enabled=1 WHERE box_id=?',(A,))
    await svc.sync_one(A)
    api.fail_group=True
    intent_id=await svc.accept(A,CP,GROUP)
    await svc.sync_one(A)
    assert one('SELECT assign_state FROM intents WHERE id=?',(intent_id,))['assign_state']=='pending'
    api.fail_group=False
    await svc.sync_one(A)
    assert one('SELECT assign_state FROM intents WHERE id=?',(intent_id,))['assign_state']=='assigned'


@pytest.mark.asyncio
async def test_outgoing_notification_when_invite_accepted():
    init_db()
    api=FakeDiadoc()
    api.relationships['A']='IsInvitedByMe'
    svc=Service(api)
    await svc.discover_organizations()
    with database() as c:
        c.execute('UPDATE organizations SET enabled=1 WHERE box_id=?',(A,))
    await svc.sync_one(A)
    api.relationships['A']='IsMyCounteragent'
    await svc.sync_one(A)
    notices=rows("SELECT * FROM notifications WHERE kind='accepted_outgoing'")
    assert notices and notices[-1]['cp_box_id']==CP
