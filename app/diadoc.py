"""Minimal Diadoc public API client. No proprietary SDK required."""
import asyncio
import time
from uuid import UUID

import httpx
from cryptography.fernet import Fernet

from . import config
from .db import database


class DiadocError(Exception):
    pass


def guid(raw: str) -> str:
    try:
        return str(UUID(raw))
    except (TypeError, ValueError, AttributeError):
        raise ValueError('Некорректный идентификатор ящика') from None


class TokenManager:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._access = ''
        self._expires = 0.0
        self._cipher = Fernet(config.TOKEN_ENCRYPTION_KEY.encode('ascii'))

    def _refresh_token(self):
        with database() as c:
            row = c.execute("SELECT encrypted_value FROM secrets WHERE key='diadoc_refresh'").fetchone()
            return (self._cipher.decrypt(row['encrypted_value'].encode()).decode()
                    if row else config.DIA_REFRESH_TOKEN)

    def _save_refresh_token(self, new_token):
        encrypted = self._cipher.encrypt(new_token.encode()).decode()
        with database() as c:
            c.execute("INSERT INTO secrets(key,encrypted_value) VALUES('diadoc_refresh',?) "
                      "ON CONFLICT(key) DO UPDATE SET encrypted_value=excluded.encrypted_value", (encrypted,))

    async def access_token(self, force=False):
        if not force and self._access and time.monotonic() < self._expires:
            return self._access
        async with self._lock:
            if not force and self._access and time.monotonic() < self._expires:
                return self._access
            refresh = self._refresh_token()
            if not config.DIA_CLIENT_ID or not config.DIA_CLIENT_SECRET or not refresh:
                raise DiadocError('Интеграция не настроена: укажите client_id, client_secret и refresh_token.')
            try:
                async with httpx.AsyncClient(timeout=25) as client:
                    res = await client.post(config.IDENTITY_BASE + '/connect/token', data={
                        'grant_type': 'refresh_token',
                        'client_id': config.DIA_CLIENT_ID,
                        'client_secret': config.DIA_CLIENT_SECRET,
                        'refresh_token': refresh,
                    })
                    res.raise_for_status()
                    obj = res.json()
                access = obj['access_token']
                rotated = obj['refresh_token']
                self._save_refresh_token(rotated)  # Persist BEFORE exposing the new access token.
                self._access = access
                self._expires = time.monotonic() + max(30, int(obj.get('expires_in', 3600)) - 300)
                return self._access
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                # Don't include server response or secret-bearing request headers in error messages.
                raise DiadocError('Не удалось обновить токен Диадока. Проверьте ключи, права и срок жизни refresh-токена.') from exc


class Diadoc:
    def __init__(self, token_manager):
        self.tokens = token_manager

    async def request(self, method, path, *, params=None, body=None):
        token = await self.tokens.access_token()
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=35) as client:
                    response = await client.request(
                        method, config.DIA_API_BASE + path, params=params, json=body,
                        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'})
                if response.status_code == 401 and attempt == 0:
                    token = await self.tokens.access_token(force=True)
                    continue
                if response.status_code in (202, 204):
                    return None
                if response.status_code >= 400:
                    explanations = {400: 'Некорректные параметры или неподдерживаемая операция',
                                    401: 'Ошибка авторизации', 402: 'Нет действующей API-подписки',
                                    403: 'Недостаточно прав для работы с ящиком',
                                    404: 'Ресурс не найден', 409: 'Конфликт операции',
                                    429: 'Превышен лимит запросов'}
                    raise DiadocError(f'Диадок: {explanations.get(response.status_code, "ошибка API")} (HTTP {response.status_code}).')
                if not response.content:
                    return None
                return response.json()
            except httpx.HTTPError as exc:
                raise DiadocError('Нет связи с API Диадока или превышено время ожидания.') from exc
            except ValueError as exc:
                raise DiadocError('Диадок вернул неожиданный формат ответа.') from exc

    async def organizations(self):
        result = await self.request('GET', '/GetMyOrganizations', params={'autoRegister': 'false'})
        return result.get('Organizations', []) if result else []

    async def groups(self, box_id):
        found = []
        for page in range(1, 101):
            r = await self.request('GET', '/GetCounteragentGroups',
                                   params={'boxId': guid(box_id), 'page': page, 'count': 100})
            part = (r or {}).get('Groups', [])
            found.extend(part)
            if len(part) < 100:
                break
        return found

    async def counterparties(self, box_id, status):
        result = []
        key = None
        for _ in range(1000):
            params = {'myBoxId': guid(box_id), 'counteragentStatus': status, 'pageSize': 100}
            if key:
                params['afterIndexKey'] = key
            response = await self.request('GET', '/V3/GetCounteragents', params=params)
            part = (response or {}).get('Counteragents', [])
            result.extend(part)
            if not part or len(part) < 100:
                return result
            new_key = part[-1].get('IndexKey')
            if not new_key or new_key == key:
                raise DiadocError('Диадок вернул некорректную пагинацию контрагентов.')
            key = new_key
        raise DiadocError('Слишком много страниц списка контрагентов.')

    async def find_by_inn(self, inn, kpp=''):
        params = {'inn': inn}
        if kpp:
            params['kpp'] = kpp
        return (await self.request('GET', '/GetOrganizationsByInnKpp', params=params) or {}).get('Organizations', [])

    async def acquire(self, own_box, cp_box='', inn='', message=''):
        body = {'MessageToCounteragent': message[:2000]}
        if cp_box:
            body['BoxId'] = guid(cp_box)
        else:
            body['Inn'] = inn
        result = await self.request('POST', '/V3/AcquireCounteragent',
                                    params={'myBoxId': guid(own_box)}, body=body)
        if not result or not result.get('TaskId'):
            raise DiadocError('Диадок не вернул идентификатор операции приглашения.')
        return result['TaskId']

    async def acquire_result(self, own_box, task_id):
        return await self.request('GET', '/V3/AcquireCounteragentResult',
                                  params={'myBoxId': guid(own_box), 'taskId': guid(task_id)})

    async def assign_group(self, own_box, cp_box, group_id):
        await self.request('POST', '/AddCounteragentToGroup', params={
            'boxId': guid(own_box), 'counteragentGroupId': guid(group_id),
            'counteragentBoxId': guid(cp_box)})
