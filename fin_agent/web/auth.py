import hashlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import delete, select

from fin_agent.web.models import AuthSession, User

PASSWORDS = PasswordHasher()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(cookie):
    return digest(cookie + ':csrf')


def normalize_username(value):
    value = value.strip().casefold()
    if not re.fullmatch(r'[a-z0-9][a-z0-9_.-]{2,63}', value):
        raise ValueError('Username must contain 3–64 ASCII letters, numbers, dots, underscores or hyphens')
    return value


def create_user(database, username, password, role='user'):
    username = normalize_username(username)
    if not 12 <= len(password) <= 256:
        raise ValueError('Password must contain 12–256 characters')
    if role not in {'user', 'admin'}:
        raise ValueError('Unknown role')
    with database.transaction() as db:
        user = User(username=username, password_hash=PASSWORDS.hash(password), role=role)
        db.add(user)
        db.flush()
        return user.id


def set_disabled(database, username, disabled):
    with database.transaction() as db:
        user = db.scalar(select(User).where(User.username == normalize_username(username)))
        if user is None:
            raise ValueError('Unknown account')
        user.disabled = disabled
        db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))


def verify_password(encoded, password):
    try:
        return PASSWORDS.verify(encoded, password)
    except VerificationError:
        return False


def new_session():
    return secrets.token_urlsafe(48)
