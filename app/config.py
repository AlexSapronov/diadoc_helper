import os
from pathlib import Path

DATA_DIR = Path(os.environ.get('DATA_DIR', '/data'))
DATABASE_PATH = DATA_DIR / 'edo.sqlite3'
APP_USERNAME = os.environ.get('APP_USERNAME', 'admin')
APP_PASSWORD_HASH = os.environ.get('APP_PASSWORD_HASH', '')
APP_SESSION_SECRET = os.environ.get('APP_SESSION_SECRET', '')
TOKEN_ENCRYPTION_KEY = os.environ.get('TOKEN_ENCRYPTION_KEY', '')
DIA_CLIENT_ID = os.environ.get('DIA_CLIENT_ID', '')
DIA_CLIENT_SECRET = os.environ.get('DIA_CLIENT_SECRET', '')
DIA_REFRESH_TOKEN = os.environ.get('DIA_REFRESH_TOKEN', '')
DIA_API_ENV = os.environ.get('DIA_API_ENV', 'production')
DIA_API_BASE = ('https://diadoc-api-staging.kontur.ru' if DIA_API_ENV == 'staging'
                else 'https://diadoc-api.kontur.ru')
IDENTITY_BASE = 'https://identity.kontur.ru'
SESSION_HTTPS_ONLY = os.environ.get('SESSION_HTTPS_ONLY', 'true').lower() == 'true'
SYNC_INTERVAL_SECONDS = max(60, int(os.environ.get('SYNC_INTERVAL_SECONDS', '180')))


def validate_config():
    if not APP_PASSWORD_HASH or not APP_SESSION_SECRET or not TOKEN_ENCRYPTION_KEY:
        raise RuntimeError('Missing APP_PASSWORD_HASH, APP_SESSION_SECRET or TOKEN_ENCRYPTION_KEY in .env')
    if len(APP_SESSION_SECRET) < 32:
        raise RuntimeError('APP_SESSION_SECRET must be at least 32 characters')
    if DIA_API_ENV not in ('production', 'staging'):
        raise RuntimeError('DIA_API_ENV must be production or staging')
    if bool(DIA_CLIENT_ID) != bool(DIA_CLIENT_SECRET):
        raise RuntimeError('Set both DIA_CLIENT_ID and DIA_CLIENT_SECRET')
