import os
import tempfile
from cryptography.fernet import Fernet
from app.security import hash_password

# Must be configured before app.main (or app.service) is imported.
os.environ['DATA_DIR'] = tempfile.mkdtemp(prefix='edo-control-tests-')
os.environ['APP_PASSWORD_HASH'] = hash_password('test-admin-password-12345')
os.environ['APP_SESSION_SECRET'] = 'test-session-secret-that-is-longer-than-32'
os.environ['TOKEN_ENCRYPTION_KEY'] = Fernet.generate_key().decode()
os.environ['SESSION_HTTPS_ONLY'] = 'false'
