import pytest
from app.db import database, init_db, one, rows
from app.service import Service, DEFAULT_GROUP

A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'
CP = '33333333-3333-4333-8333-333333333333'
GROUP_A = '44444444-4444-4444-8444-444444444444'
GROUP_B = '88888888-8888-4888-8888-888888888888'


class FakeDiadoc:
    def __init__(self):
        self.relationships = {'A': 'InvitesMe'}
        self.group_assignments = []
        self.tasks = {}
        self.current_group = DEFAULT_GROUP
        self.fail_group = False
        self.acquired = []
        self.create_requests = 0
        self.groups_by_box = {
            A: [{'CounteragentGroupId': DEFAULT_GROUP, 'Name': 'По умолчанию'},
                {'CounteragentGroupId': GROUP_A, 'Name': 'ОП СПБ',
                 'Departments': {'DepartmentIds': ['aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa']}}],
            B: [{'CounteragentGroupId': DEFAULT_GROUP, 'Name': 'По умолчанию'},
                {'CounteragentGroupId': GROUP_B, 'Name': 'Бухгалтерия'}],
        }

    async def organizations(self):
        return [{'ShortName': 'ООО Наша', 'Inn': '1111111111', 'Kpp': '111111111',
                 'Boxes': [{'BoxIdGuid': A, 'Title': 'ООО Наша'},
                           {'BoxIdGuid': B, 'Title': 'ООО Вторая'}]}]

    async def groups(self, box_id):
        return self.groups_by_box[box_id]

    async def create_department_group(self, *args, **kwargs):
        self.create_requests += 1
        raise AssertionError('The application must never create groups')

    async def counterparties(self, box_id, status):
        if box_id != A or self.relationships['A'] != status:
            return []
        return [{'Organization': {'ShortName': 'ООО Клиент', 'Inn': '1234567890',
                 'Boxes': [{'BoxIdGuid': CP}]}, 'CurrentStatus': status,
                 'CounteragentGroupId': self.current_group}]

    async def acquire(self, own_box, cp_box='', inn='', message='', department_id=''):
        self.acquired.append((own_box, cp_box, inn, department_id))
        task_id = '55555555-5555-4555-8555-555555555555'
        self.tasks[task_id] = cp_box or CP
        if self.relationships['A'] == 'InvitesMe':
            self.relationships['A'] = 'IsMyCounteragent'
        else:
            self.relationships['A'] = 'IsInvitedByMe'
        return task_id

    async def acquire_result(self, own_box, task_id):
        return {'BoxId': self.tasks[task_id]}

    async def assign_group(self, own_box, cp_box, group_id):
        if self.fail_group:
            from app.diadoc import DiadocError
            raise DiadocError('temporary failure')
        self.group_assignments.append((own_box, cp_box, group_id))
        self.current_group = group_id


async def setup_service(api):
    svc = Service(api)
    assert await svc.discover_organizations() == 2
    with database() as c:
        c.execute('UPDATE organizations SET enabled=1 WHERE box_id IN (?,?)', (A, B))
    await svc.sync_one(A)
    await svc.sync_one(B)
    return svc


@pytest.mark.asyncio
async def test_incoming_requires_existing_group_and_assigns_after_accept():
    init_db()
    api = FakeDiadoc()
    svc = await setup_service(api)
    assert svc.verify_group(A, GROUP_A)[1]['name'] == 'ОП СПБ'
    with pytest.raises(ValueError):
        svc.verify_group(A, DEFAULT_GROUP)
    with pytest.raises(ValueError):
        svc.verify_group(A, GROUP_B)  # Group belongs to a different legal entity.
    with pytest.raises(ValueError):
        await svc.accept(A, CP, '')
    assert not api.acquired
    intent_id = await svc.accept(A, CP, GROUP_A)
    await svc.sync_one(A)
    assert one('SELECT assign_state FROM intents WHERE id=?', (intent_id,))['assign_state'] == 'assigned'
    assert api.group_assignments == [(A, CP, GROUP_A)]
    assert api.acquired == [(A, CP, '', '')]
    assert api.create_requests == 0
    assert one('SELECT target_group_id FROM intents WHERE id=?', (intent_id,))['target_group_id'] == GROUP_A


@pytest.mark.asyncio
async def test_retries_group_after_temporary_failure():
    api = FakeDiadoc()
    svc = await setup_service(api)
    api.fail_group = True
    intent_id = await svc.accept(A, CP, GROUP_A)
    await svc.sync_one(A)
    assert one('SELECT assign_state FROM intents WHERE id=?', (intent_id,))['assign_state'] == 'pending'
    api.fail_group = False
    await svc.sync_one(A)
    assert one('SELECT assign_state FROM intents WHERE id=?', (intent_id,))['assign_state'] == 'assigned'


@pytest.mark.asyncio
async def test_outgoing_notification_when_invite_accepted():
    api = FakeDiadoc()
    api.relationships['A'] = 'IsInvitedByMe'
    svc = await setup_service(api)
    api.relationships['A'] = 'IsMyCounteragent'
    await svc.sync_one(A)
    notices = rows("SELECT * FROM notifications WHERE kind='accepted_outgoing'")
    assert notices and notices[-1]['cp_box_id'] == CP


@pytest.mark.asyncio
async def test_outgoing_uses_existing_group_without_adding_or_creating_group():
    api = FakeDiadoc()
    api.relationships['A'] = 'IsInvitedByMe'
    svc = await setup_service(api)
    intent_id = await svc.invite(A, GROUP_A, CP, '1234567890', 'ООО Клиент')
    assert one('SELECT target_group_id FROM intents WHERE id=?', (intent_id,))['target_group_id'] == GROUP_A
    assert api.acquired == [(A, CP, '1234567890', '')]
    assert api.create_requests == 0
    assert api.group_assignments == []  # Assignment follows the counterparty's acceptance.


@pytest.mark.asyncio
async def test_group_is_required_and_must_be_owned_by_selected_box():
    api = FakeDiadoc()
    svc = await setup_service(api)
    with pytest.raises(ValueError):
        await svc.invite(A, '', CP, '1234567890', 'ООО Клиент')
    with pytest.raises(ValueError):
        await svc.invite(A, GROUP_B, CP, '1234567890', 'ООО Клиент')
    with pytest.raises(ValueError):
        await svc.accept(A, CP, DEFAULT_GROUP)
    assert not api.acquired
    assert api.create_requests == 0


@pytest.mark.asyncio
async def test_deleted_group_is_not_recreated_or_replaced_with_default():
    api = FakeDiadoc()
    svc = await setup_service(api)
    intent_id = await svc.accept(A, CP, GROUP_A)
    api.groups_by_box[A] = [{'CounteragentGroupId': DEFAULT_GROUP, 'Name': 'По умолчанию'}]
    await svc.sync_one(A)
    saved = one('SELECT assign_state,error,target_group_id FROM intents WHERE id=?', (intent_id,))
    assert saved['assign_state'] == 'pending'
    assert 'не существует' in saved['error']
    assert saved['target_group_id'] == GROUP_A
    assert api.create_requests == 0
    assert not api.group_assignments
