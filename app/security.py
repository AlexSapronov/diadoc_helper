import base64
import hashlib
import hmac
import os
import sys


def hash_password(value: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(value.encode('utf-8'), salt=salt, n=2**14, r=8, p=1)
    return base64.urlsafe_b64encode(salt).decode() + ':' + base64.urlsafe_b64encode(digest).decode()


def check_password(value: str, encoded: str) -> bool:
    try:
        salt_b64, digest_b64 = encoded.split(':', 1)
        salt = base64.urlsafe_b64decode(salt_b64)
        expected = base64.urlsafe_b64decode(digest_b64)
        actual = hashlib.scrypt(value.encode('utf-8'), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


if __name__ == '__main__':
    import getpass
    pw = getpass.getpass('New admin password (16+ chars recommended): ')
    if len(pw) < 12:
        sys.exit('Password must have at least 12 characters')
    confirm = getpass.getpass('Repeat password: ')
    if pw != confirm:
        sys.exit('Passwords do not match')
    print(hash_password(pw))
