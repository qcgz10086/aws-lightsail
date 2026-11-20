from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.password_strength import validate_secret_key, validate_strong_password

# 已公开的默认值 / 占位值：启动时一律拒绝，绝不能用于生产。
# （H-1/H-2 修复：SECRET_KEY / ENCRYPTION_KEY / ADMIN_PASSWORD 均为必填、无默认值）
_DENIED_SECRET_KEYS = {
    "dev-insecure-secret-change-me",
    "change-me-to-a-long-random-string",
    "change-me",
}
_DENIED_ADMIN_PASSWORDS = {
    "admin123",
    "change-me",
    "change-me-set-a-strong-admin-password",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 必填，无默认值：缺失时 pydantic 直接报 Field required，拒绝启动。
    secret_key: str
    encryption_key: str
    admin_username: str = "admin"
    # 必填，无默认值：seed 只在提供了密码时才创建管理员。
    admin_password: str
    database_url: str = "sqlite:////data/app.db"
    cors_origins: str = ""
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days
    jwt_algorithm: str = "HS256"
    # R-5：登录 Cookie 的 Secure 标志；生产走 HTTPS 时设 true（默认 False，纯 HTTP 可用）。
    cookie_secure: bool = False
    # R-9：默认关闭 /docs、/redoc、/openapi.json；仅 ENABLE_DOCS=true 时开启。
    enable_docs: bool = False

    @model_validator(mode="after")
    def _reject_weak_or_placeholder_secrets(self) -> "Settings":
        self.secret_key = (self.secret_key or "").strip()
        self.encryption_key = (self.encryption_key or "").strip()
        self.admin_password = (self.admin_password or "").strip()

        if (
            not self.secret_key
            or self.secret_key in _DENIED_SECRET_KEYS
            or self.secret_key.startswith("change-me")
        ):
            raise ValueError(
                "SECRET_KEY 未配置或是公开的默认值/占位值："
                "请在 .env 中设置强随机 SECRET_KEY，拒绝启动"
            )
        # R-1：只要求长度不够——32 位以上的低熵值（如 "a"*32）也要拒绝。
        try:
            validate_secret_key(self.secret_key)
        except ValueError as exc:
            raise ValueError(f"{exc}，拒绝启动") from exc

        if (
            not self.encryption_key
            or self.encryption_key.startswith("change-me")
        ):
            raise ValueError(
                "ENCRYPTION_KEY 未配置或是占位值："
                "请在 .env 中设置 Fernet.generate_key() 生成的密钥，拒绝启动"
            )
        try:
            Fernet(self.encryption_key.encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError(
                "ENCRYPTION_KEY 不是合法的 Fernet key："
                "请用 Fernet.generate_key() 生成，拒绝启动"
            ) from exc

        if (
            not self.admin_password
            or self.admin_password in _DENIED_ADMIN_PASSWORDS
            or self.admin_password.startswith("change-me")
        ):
            raise ValueError(
                "ADMIN_PASSWORD 未配置或是默认密码 admin123："
                "请在 .env 中设置强管理员密码，拒绝启动"
            )
        # R-1：只防已知默认值不够——ADMIN_PASSWORD 须满足与实例密码同一强度标准。
        try:
            validate_strong_password(self.admin_password, field_name="ADMIN_PASSWORD")
        except ValueError as exc:
            raise ValueError(f"{exc}，拒绝启动") from exc
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        # R-8：默认不放行任何跨域来源；只有 CORS_ORIGINS 里明确列出的才放行。
        # 生产由 nginx 同源反代，不依赖 CORS；本地开发需要时在 .env 里显式写明来源。
        if not self.cors_origins.strip():
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
