"""Password hashing for local user accounts.

Wraps bcrypt directly (passlib's bcrypt backend is broken against bcrypt
5.x on newer Pythons). Bcrypt has a 72-byte ceiling on the secret — we
clamp here so a long passphrase doesn't raise at hash time and so verify
sees the same bytes as hash.
"""

import bcrypt

_MAX_BCRYPT_BYTES = 72


def _clamp(password):
    if isinstance(password, str):
        password = password.encode("utf-8")
    return password[:_MAX_BCRYPT_BYTES]


def hash_password(password):
    return bcrypt.hashpw(_clamp(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password, password_hash):
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(_clamp(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False
