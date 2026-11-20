"""定时流量采集 + 可选超限自动关机。"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app import database
from app.models import AwsCredential, User
from app.services.aws_clients import select_scan_credentials
from app.services.query_settings import claim_due_sync, finish_due_sync


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
    from fastapi import HTTPException
    from app.services.task_submission import submit

    slot = claim_due_sync(database.engine)
    if slot is None:
        return
    complete = True
    with database.SessionLocal() as db:
        scopes = [(user.id, cred.id, cred.account_id or f"credential-{cred.id}")
                  for user in db.query(User).filter(User.id.in_(db.query(AwsCredential.user_id))).all()
                  for cred in select_scan_credentials(user.credentials)]
        db.commit()
        for user_id, credential_id, identity in scopes:
            user = db.get(User, user_id)
            if user is not None:
                try:
                    submit(db, user, "traffic_sync", credential_id,
                           idempotency_key=f"scheduler:{slot.hex()}:{user_id}:{identity}")
                except HTTPException as exc:
                    db.rollback()
                    if exc.status_code not in {404, 409}:
                        complete = False
                        logger.warning("流量任务未入队 user_id=%s credential_id=%s", user_id, credential_id)
                except Exception:
                    db.rollback()
                    complete = False
                    logger.warning("流量任务未入队 user_id=%s credential_id=%s", user_id, credential_id)
    if complete:
        finish_due_sync(database.engine, slot)


def _recover_tasks() -> None:
    from app.services.task_queue import queue_for

    queue_for(database.engine).recover(expired_only=True)


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        sync_all_users,
        "interval",
        seconds=1,
        next_run_time=datetime.now(timezone.utc),
        id="traffic_collector",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(_recover_tasks, "interval", minutes=1, id="task_recovery",
                      max_instances=1, coalesce=True)
    scheduler.start()
    _scheduler = scheduler
    logger.info("流量采集调度已启动")
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)
        _scheduler = None
