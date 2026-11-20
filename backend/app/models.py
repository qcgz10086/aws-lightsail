from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # R-3：JWT 吊销版本；改密/登出时 +1，使此前签发的所有 token 失效。
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    monthly_limit_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 用户级默认：新建实例设置时的默认勾选，不强制所有实例
    auto_stop_on_limit_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    credentials: Mapped[list["AwsCredential"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    instance_settings: Mapped[list["InstanceSetting"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    traffic_usages: Mapped[list["TrafficUsage"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    operation_logs: Mapped[list["OperationLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    # 赠金记录：用户手动录入，AWS 没有 API 能直接查官方剩余余额，
    # 剩余值由 Cost Explorer 累计抵扣额估算（见 services/billing.py）。
    credit_records: Mapped[list["CreditRecord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def has_credentials(self) -> bool:
        return bool(self.credentials)

    @property
    def default_credential(self) -> "AwsCredential | None":
        if not self.credentials:
            return None
        for c in self.credentials:
            if c.is_default:
                return c
        return self.credentials[0]


class AwsCredential(Base):
    __tablename__ = "aws_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    access_key_id_enc: Mapped[str] = mapped_column(Text, nullable=False)
    secret_access_key_enc: Mapped[str] = mapped_column(Text, nullable=False)
    # 该账号单独的代理（Fernet 加密存；为空=直连）。代理 URL 可能含认证
    # 信息（http://user:pass@host:port），与密钥同等对待，绝不明文存。
    proxy_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    last_confirmed_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    auto_stop_conflict: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Lightsail Service Quotas：Instances 配额单位为 vCPU/Region（社区常称 5V/8V/32V）
    vcpu_quota: Mapped[float | None] = mapped_column(Float, nullable=True)
    vcpu_tier: Mapped[str | None] = mapped_column(String(32), nullable=True)
    static_ip_quota: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_vcpu: Mapped[float | None] = mapped_column(Float, nullable=True)
    used_instance_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quota_region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quota_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    quota_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="credentials")
    instance_settings: Mapped[list["InstanceSetting"]] = relationship(
        back_populates="credential", cascade="all, delete-orphan", passive_deletes=True
    )
    traffic_usages: Mapped[list["TrafficUsage"]] = relationship(
        back_populates="credential", cascade="all, delete-orphan", passive_deletes=True
    )


class InstanceSetting(Base):
    __tablename__ = "instance_settings"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "credential_id", "region", "instance_name", name="uq_instance_setting"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    credential_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("aws_credentials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_name: Mapped[str] = mapped_column(String(128), nullable=False)
    monthly_limit_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 勾选后超限才自动关机；默认 False 仅告警
    auto_stop_on_limit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_stop_conflict: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    auto_stop_configured: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("0"), nullable=False)
    auto_stop_account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="instance_settings")
    credential: Mapped[AwsCredential] = relationship(back_populates="instance_settings")


class TrafficUsage(Base):
    __tablename__ = "traffic_usage"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "credential_id",
            "region",
            "instance_name",
            "year_month",
            "account_id",
            name="uq_traffic_usage",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    credential_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("aws_credentials.id", ondelete="CASCADE"), nullable=False, index=True
    )
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    instance_name: Mapped[str] = mapped_column(String(128), nullable=False)
    year_month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    # Empty string is the unknown-identity sentinel, so the composite unique key
    # also protects rows when STS cannot confirm an account.
    account_id: Mapped[str] = mapped_column(String(12), default="", server_default=text("''"), nullable=False)
    network_in_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    network_out_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="traffic_usages")
    credential: Mapped[AwsCredential] = relationship(back_populates="traffic_usages")


class CreditRecord(Base):
    """Manual grant with explicit historical account and exact decimal amount.

    No per-grant consumption is inferred from account-wide cost differences.
    total_amount is retained solely for old client/storage compatibility.
    """

    __tablename__ = "credit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    credential_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("aws_credentials.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    credit_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    total_amount_decimal: Mapped[str | None] = mapped_column(Text, nullable=True)
    amount_precision: Mapped[str] = mapped_column(String(32), default="legacy_float", server_default=text("'legacy_float'"), nullable=False)
    account_id: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="credit_records")
    credential: Mapped["AwsCredential | None"] = relationship()


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="success")
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="operation_logs")


class DataSnapshot(Base):
    __tablename__ = "data_snapshots"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id: Mapped[int] = mapped_column(Integer, ForeignKey("aws_credentials.id", ondelete="CASCADE"), nullable=False, index=True)
    account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_success: Mapped[float | None] = mapped_column(Float, nullable=True)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    next_retry_at: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False, default=0, index=True)


class OperationTask(Base):
    __tablename__ = "operation_tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_operation_task_idempotency"),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','partial','reconciliation_required','cancelled','resolved')",
            name="ck_operation_task_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    credential_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    account_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    credential_version: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_payload: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    current_step: Mapped[str | None] = mapped_column(String(96), nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperationTaskStep(Base):
    __tablename__ = "operation_task_steps"
    __table_args__ = (
        UniqueConstraint("task_id", "sequence", name="uq_operation_task_step_sequence"),
        CheckConstraint("status IN ('intent','succeeded','failed','unknown','compensated')", name="ck_operation_task_step_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("operation_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(96), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="intent")
    intent_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperationTaskLock(Base):
    __tablename__ = "operation_task_locks"

    lock_key: Mapped[str] = mapped_column(String(192), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("operation_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    account_scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lock_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
