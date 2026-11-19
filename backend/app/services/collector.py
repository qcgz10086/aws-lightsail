"""定时流量采集 + 可选超限自动关机。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal
from app.models import AwsCredential, User
from app.services import lightsail as ls
from app.services.aws_clients import select_scan_credentials


logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def sync_user_traffic(db: Session, user: User, credential_id: int | None = None,
                      idempotency_key: str | None = None) -> list[dict]:
    """Persist one account task per selected identity; never mutate AWS here."""
    from app.services.task_submission import submit
    selected = select_scan_credentials(user.credentials) if credential_id is None else [
        credential for credential in user.credentials if credential.id == credential_id]
    if credential_id is not None and not selected:
        from app.services.traffic import NoCredentialsError
        raise NoCredentialsError("凭证不存在")
    key = idempotency_key or str(uuid.uuid4())
    return [submit(db, user, "traffic_sync", cred.id,
                   idempotency_key=f"{key}:{cred.id}") for cred in selected]


def sync_all_users() -> None:
    # A deterministic interval bucket survives scheduler retries. The default
    # traffic window is materialized once on acceptance, after idempotency lookup.
    from app.services.task_queue import TaskConflict
    from app.services.task_submission import submit
    interval = max(5, int(get_settings().collect_interval_minutes or 60)) * 60
    bucket = int(datetime.now(timezone.utc).timestamp()) // interval
    with SessionLocal() as db:
        from app.services.task_queue import queue_for
        queue_for(db.get_bind()).recover(expired_only=True)
        users = db.query(User).filter(User.id.in_(db.query(AwsCredential.user_id))).all()
        for user in users:
            for cred in select_scan_credentials(user.credentials):
                try:
                    submit(db, user, "traffic_sync", cred.id,
                           idempotency_key=f"scheduler:{bucket}:{user.id}:{cred.id}")
                except Exception:
                    db.rollback()
                    logger.warning("流量任务未入队 user_id=%s credential_id=%s", user.id, cred.id)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    settings = get_settings()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        sync_all_users,
        "interval",
        minutes=max(5, int(settings.collect_interval_minutes or 60)),
        id="traffic_collector",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "流量采集调度已启动，间隔 %s 分钟",
        settings.collect_interval_minutes,
    )
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
