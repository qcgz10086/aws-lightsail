import logging

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User
from app.security import hash_password

logger = logging.getLogger(__name__)


def seed_admin(db: Session) -> None:
    """仅在用户表为空且提供了管理员密码时创建管理员。

    ADMIN_PASSWORD 为必填配置（见 app.config）：未配置或为默认值时
    应用在启动时即已拒绝，正常情况下不会走到这里；此处再守一道，
    绝不以默认密码创建管理员。
    """
    count = db.query(User).count()
    if count > 0:
        return
    settings = get_settings()
    if not (settings.admin_password or "").strip():
        logger.error("未配置 ADMIN_PASSWORD，跳过创建管理员账号")
        return
    admin = User(
        username=settings.admin_username,
        password_hash=hash_password(settings.admin_password),
        is_admin=True,
    )
    db.add(admin)
    db.commit()
    logger.warning(
        "已创建默认管理员账号 username=%s（请尽快修改密码）",
        settings.admin_username,
    )
