"""Persistent read-only discovery and query state, separate from mutation tasks."""
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class QueryAccount(Base):
    __tablename__ = "query_accounts"
    __table_args__ = (UniqueConstraint("user_id", "account_key", name="uq_query_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_key: Mapped[str] = mapped_column(String(64))
    discovery_state: Mapped[str] = mapped_column(String(24), default="pending")
    last_global_round_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_global_started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_global_finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class QueryRegion(Base):
    __tablename__ = "query_regions"
    __table_args__ = (UniqueConstraint("user_id", "account_key", "region", name="uq_query_region"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_key: Mapped[str] = mapped_column(String(64), index=True)
    region: Mapped[str] = mapped_column(String(32))
    observed_state: Mapped[str] = mapped_column(String(16), default="unknown")
    opt_status: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    opt_checked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_success_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_failure_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[float] = mapped_column(Float, default=0)


class QueryRound(Base):
    __tablename__ = "query_rounds"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    credential_id: Mapped[int | None] = mapped_column(
        ForeignKey("aws_credentials.id", ondelete="SET NULL"), nullable=True, index=True)
    account_key: Mapped[str] = mapped_column(String(64), index=True)
    account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    credential_version: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    region_filter: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # NULL releases the uniqueness fence when a round finishes.
    active_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    targets_fixed: Mapped[bool] = mapped_column(Boolean, default=False)
    force_refresh: Mapped[bool] = mapped_column(Boolean, default=False)
    worker_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class QueryTarget(Base):
    __tablename__ = "query_targets"
    __table_args__ = (UniqueConstraint("round_id", "region", name="uq_query_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[str] = mapped_column(ForeignKey("query_rounds.id", ondelete="CASCADE"), index=True)
    region: Mapped[str] = mapped_column(String(32))
    snapshot_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    auxiliary_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[float] = mapped_column(Float, default=0)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)
