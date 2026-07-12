import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import SystemSetting


EDITABLE_KEYS = {
    "smtp_host",
    "smtp_port",
    "smtp_use_ssl",
    "smtp_username",
    "smtp_auth_code",
    "smtp_from_name",
    "default_notify_emails",
}
SECRET_KEYS = {"smtp_auth_code"}


def _fernet() -> Fernet:
    secret = get_settings().secret_key.encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("系统配置无法解密，请检查 SECRET_KEY 是否被修改") from exc


def save_settings(db: Session, values: dict[str, str]) -> None:
    for key, value in values.items():
        if key not in EDITABLE_KEYS:
            continue
        existing = db.get(SystemSetting, key)
        if key in SECRET_KEYS and not value:
            continue
        stored = encrypt(value) if key in SECRET_KEYS else value
        if existing:
            existing.value = stored
            existing.encrypted = key in SECRET_KEYS
        else:
            db.add(SystemSetting(key=key, value=stored, encrypted=key in SECRET_KEYS))
    db.commit()


def effective_settings(db: Session) -> Settings:
    base = get_settings()
    updates: dict[str, object] = {}
    for row in db.scalars(select(SystemSetting)).all():
        if row.key not in EDITABLE_KEYS:
            continue
        value = decrypt(row.value) if row.encrypted else row.value
        if row.key == "smtp_port":
            updates[row.key] = int(value)
        elif row.key == "smtp_use_ssl":
            updates[row.key] = value.lower() in {"1", "true", "yes", "on"}
        else:
            updates[row.key] = value
    return base.model_copy(update=updates)


def config_status(db: Session) -> dict[str, bool]:
    current = effective_settings(db)
    return {
        "smtp_secret": bool(current.smtp_username and current.smtp_auth_code),
        "recipients": bool(current.notify_emails),
    }

