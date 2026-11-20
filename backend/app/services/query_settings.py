"""Read and update the database-owned settings and periodic collection schedule."""
from datetime import datetime, timezone
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import User
from app.settings_models import QuerySettingChange, QuerySettings


def _view(row: QuerySettings) -> dict:
    return {"query_interval_ms": row.query_interval_ms,
            "traffic_interval_minutes": row.traffic_interval_minutes,
            "next_sync_at": datetime.fromtimestamp(row.next_sync_at, timezone.utc),
            "updated_at": datetime.fromtimestamp(row.updated_at, timezone.utc)}


def _settings(db: Session) -> QuerySettings:
    row = db.get(QuerySettings, 1)
    if row is None:
        raise RuntimeError("Query settings have not been initialized")
    return row


def get_query_settings(db: Session) -> dict:
    return _view(_settings(db))


def save_query_settings(db: Session, user_id: int, token_version: int,
                        interval_ms: int, period_minutes: int) -> dict:
    db.rollback()
    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
    admin = db.get(User, user_id)
    if admin is None or not admin.is_admin or admin.token_version != token_version:
        db.rollback()
        raise PermissionError("Administrator authorization changed")
    row = _settings(db)
    if (row.query_interval_ms, row.traffic_interval_minutes) != (interval_ms, period_minutes):
        now = time.time()
        db.add(QuerySettingChange(user_id=user_id, changed_at=now,
            old_query_interval_ms=row.query_interval_ms, new_query_interval_ms=interval_ms,
            old_traffic_interval_minutes=row.traffic_interval_minutes, new_traffic_interval_minutes=period_minutes))
        if row.traffic_interval_minutes != period_minutes:
            row.next_sync_at = now + period_minutes * 60
        row.query_interval_ms = interval_ms
        row.traffic_interval_minutes = period_minutes
        row.updated_at = now
    view = _view(row)
    db.commit()
    return view


def claim_due_sync(engine, now: float | None = None) -> float | None:
    now = time.time() if now is None else now
    with Session(engine) as db:
        row = _settings(db)
        if row.next_sync_at > now and row.pending_sync_at is None:
            return None
        # A crash during dispatch leaves the same slot available for replay.
        db.rollback()
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = _settings(db)
        if row.next_sync_at <= now:
            if row.pending_sync_at is None:
                row.pending_sync_at = row.next_sync_at
            row.next_sync_at = now + row.traffic_interval_minutes * 60
        pending = row.pending_sync_at
        db.commit()
        return pending


def finish_due_sync(engine, pending: float) -> None:
    with Session(engine) as db, db.begin():
        db.execute(update(QuerySettings).where(QuerySettings.id == 1,
            QuerySettings.pending_sync_at == pending).values(pending_sync_at=None))


def query_interval_seconds() -> float:
    from app.database import SessionLocal

    with SessionLocal() as db:
        return db.scalars(select(QuerySettings.query_interval_ms).where(QuerySettings.id == 1)).one() / 1000
