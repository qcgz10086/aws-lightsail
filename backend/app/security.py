import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from app.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def create_access_token(
    subject: str,
    extra: dict[str, Any] | None = None,
    token_version: int = 0,
) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    # R-3：ver 为签发时用户的 token_version（改密/登出后递增，旧 token 失效）；
    # jti 为本次签发的唯一标识。
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expire,
        "ver": token_version,
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("无效或过期的令牌") from exc


# R-10：登录时用户不存在也用该固定 dummy 哈希做一次 bcrypt 校验，
# 使耗时与用户存在时相当，堵住用户枚举时序侧信道。
# 该哈希对应一个永远不会被用作真实密码的固定 dummy 口令，本身不是任何账号的凭证。
_DUMMY_PASSWORD_HASH = "$2b$12$THojW9XOsgOUk4waDtWZY.p.LY7Cc4GJCM3DL6LLnrfqJ2ykiInka"


def check_dummy_password(plain: str) -> bool:
    """仅用于计时对齐：做一次真实 bcrypt 校验，恒返回 False。"""
    verify_password(plain, _DUMMY_PASSWORD_HASH)
    return False


def _fernet() -> Fernet:
    settings = get_settings()
    key = (settings.encryption_key or "").strip()
    if not key:
        raise RuntimeError("未配置 ENCRYPTION_KEY，无法加密 AWS 凭证")
    try:
        return Fernet(key.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("ENCRYPTION_KEY 无效，请使用 Fernet.generate_key() 生成") from exc


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("凭证解密失败，可能 ENCRYPTION_KEY 已更换，请重新绑定 AWS Key") from exc


def mask_access_key(access_key_id: str) -> str:
    if len(access_key_id) <= 8:
        return "*" * len(access_key_id)
    return f"{access_key_id[:4]}****{access_key_id[-4:]}"
