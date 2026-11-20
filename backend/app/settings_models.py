"""Persist the active query settings, a resumable schedule tick and audit history."""
from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


DEFAULT_QUERY_INTERVAL_MS = 200
DEFAULT_TRAFFIC_INTERVAL_MINUTES = 60
MIN_QUERY_INTERVAL_MS = 200
MAX_QUERY_INTERVAL_MS = 5000
MIN_TRAFFIC_INTERVAL_MINUTES = 5
MAX_TRAFFIC_INTERVAL_MINUTES = 525600


class QuerySettings(Base):
    __tablename__ = "query_settings"
    __table_args__ = (
        CheckConstraint("id = 1"),
        CheckConstraint(f"query_interval_ms BETWEEN {MIN_QUERY_INTERVAL_MS} AND {MAX_QUERY_INTERVAL_MS}"),
        CheckConstraint(f"traffic_interval_minutes BETWEEN {MIN_TRAFFIC_INTERVAL_MINUTES} AND {MAX_TRAFFIC_INTERVAL_MINUTES}"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    query_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    traffic_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_sync_at: Mapped[float] = mapped_column(Float, nullable=False)
    pending_sync_at: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class QuerySettingChange(Base):
    __tablename__ = "query_setting_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    changed_at: Mapped[float] = mapped_column(Float, nullable=False)
    old_query_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    new_query_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    old_traffic_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    new_traffic_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False)


class OperationTaskRequest(Base):
    """Resolve a coalesced submission by its own idempotency key after a lost response."""

    __tablename__ = "operation_task_requests"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("operation_tasks.id", ondelete="CASCADE"), index=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)


class TrafficTaskCancellation(Base):
    __tablename__ = "traffic_task_cancellations"

    task_id: Mapped[str] = mapped_column(ForeignKey("operation_tasks.id", ondelete="CASCADE"), primary_key=True)


class TrafficReadFailure(Base):
    __tablename__ = "traffic_read_failures"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    credential_id: Mapped[int] = mapped_column(ForeignKey("aws_credentials.id", ondelete="CASCADE"), primary_key=True)
    credential_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str] = mapped_column(String(32), primary_key=True)
    retry_at: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    details: Mapped[str] = mapped_column(Text, nullable=False)


def initial_settings(now: float) -> dict:
    return {"id": 1, "query_interval_ms": DEFAULT_QUERY_INTERVAL_MS,
            "traffic_interval_minutes": DEFAULT_TRAFFIC_INTERVAL_MINUTES,
            "next_sync_at": now + DEFAULT_TRAFFIC_INTERVAL_MINUTES * 60, "updated_at": now}
