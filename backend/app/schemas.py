from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserOut"


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    is_admin: bool
    monthly_limit_gb: float | None = None
    auto_stop_on_limit_default: bool = False
    created_at: datetime
    has_credentials: bool = False
    credential_count: int = 0


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6)
    is_admin: bool = False
    monthly_limit_gb: float | None = None
    auto_stop_on_limit_default: bool = False


class UserUpdate(BaseModel):
    password: str | None = Field(default=None, min_length=6)
    is_admin: bool | None = None
    monthly_limit_gb: float | None = None
    auto_stop_on_limit_default: bool | None = None


class CredentialCreate(BaseModel):
    access_key_id: str = Field(min_length=16, max_length=128)
    secret_access_key: str = Field(min_length=16, max_length=128)
    account_label: str | None = None
    is_default: bool = False


class CredentialUpdate(BaseModel):
    access_key_id: str | None = Field(default=None, min_length=16, max_length=128)
    secret_access_key: str | None = Field(default=None, min_length=16, max_length=128)
    account_label: str | None = None
    is_default: bool | None = None


class CredentialItem(BaseModel):
    id: int
    access_key_masked: str
    account_label: str | None = None
    is_default: bool = False
    last_validated_at: datetime | None = None
    created_at: datetime | None = None
    # Lightsail 配额（vCPU/Region，如 5V/8V/32V）
    vcpu_quota: float | None = None
    vcpu_tier: str | None = None
    static_ip_quota: float | None = None
    used_vcpu: float | None = None
    used_instance_count: int | None = None
    remaining_vcpu: float | None = None
    quota_region: str | None = None
    quota_message: str | None = None
    quota_checked_at: datetime | None = None


class CredentialListOut(BaseModel):
    items: list[CredentialItem]
    has_credentials: bool


# 兼容旧前端字段（单对象），同时提供列表
class CredentialOut(BaseModel):
    has_credentials: bool
    items: list[CredentialItem] = []
    # 默认凭证摘要
    id: int | None = None
    access_key_masked: str | None = None
    account_label: str | None = None
    last_validated_at: datetime | None = None


class InstanceCreate(BaseModel):
    region: str
    instance_name: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9\-]*$")
    blueprint_id: str
    bundle_id: str
    allocate_static_ip: bool = True
    availability_zone: str | None = None
    credential_id: int | None = None
    # 自定义登录密码（通过 userData 注入；不落库）
    # 不设 min_length：长度与复杂度统一由 services.lightsail._validate_instance_password
    # 校验并以 LightsailError 返回 400；留空则不启用密码登录，走密钥登录。
    password: str | None = Field(default=None, max_length=64)
    # 可选：LINUX_UNIX / WINDOWS，用于选择注入脚本
    platform: str | None = None
    # 创建后是否开放全部防火墙端口（0-65535 / all / 任意 IPv4+IPv6）
    # 默认关闭（H-3 修复）：需用户明确勾选才放行。
    open_all_ports: bool = False


class InstanceSettingsUpdate(BaseModel):
    monthly_limit_gb: float | None = None
    auto_stop_on_limit: bool | None = None
    note: str | None = None


class InstanceTraffic(BaseModel):
    in_gb: float = 0
    out_gb: float = 0
    total_gb: float = 0
    limit_gb: float | None = None
    over_limit: bool = False
    year_month: str | None = None


class InstanceOut(BaseModel):
    name: str
    region: str
    availability_zone: str | None = None
    state: str
    public_ip: str | None = None
    private_ip: str | None = None
    blueprint_id: str | None = None
    blueprint_name: str | None = None
    bundle_id: str | None = None
    is_static_ip: bool = False
    static_ip_name: str | None = None
    created_at: datetime | None = None
    traffic: InstanceTraffic | None = None
    monthly_limit_gb: float | None = None
    auto_stop_on_limit: bool = False
    note: str | None = None
    credential_id: int | None = None
    account_label: str | None = None


class CreateInstanceResponse(BaseModel):
    name: str
    region: str
    credential_id: int | None = None
    static_ip_name: str | None = None
    message: str


class ChangeIpResponse(BaseModel):
    instance_name: str
    region: str
    old_ip: str | None = None
    new_ip: str | None = None
    static_ip_name: str
    message: str


class TrafficInstanceRow(BaseModel):
    region: str
    name: str
    credential_id: int | None = None
    account_label: str | None = None
    in_gb: float
    out_gb: float
    total_gb: float
    limit_gb: float | None = None
    over_limit: bool = False
    auto_stop_on_limit: bool = False
    year_month: str


class TrafficRegionRow(BaseModel):
    region: str
    total_gb: float
    instance_count: int


class TrafficSummary(BaseModel):
    year_month: str
    instances: list[TrafficInstanceRow]
    by_region: list[TrafficRegionRow]
    note: str = "流量基于 Lightsail NetworkIn/NetworkOut 指标估算，与账单可能存在差异"


class MetricPoint(BaseModel):
    timestamp: datetime
    network_in_bytes: float
    network_out_bytes: float


class MetricsSeries(BaseModel):
    region: str
    name: str
    period: str
    points: list[MetricPoint]


class RegionOut(BaseModel):
    name: str
    display_name: str
    continent_code: str | None = None


class BundleOut(BaseModel):
    bundle_id: str
    name: str
    price: float | None = None
    cpu_count: int | None = None
    ram_size_in_gb: float | None = None
    disk_size_in_gb: int | None = None
    transfer_per_month_in_gb: int | None = None
    power: int | None = None
    is_active: bool = True
    supported_platforms: list[str] = []


class BlueprintOut(BaseModel):
    blueprint_id: str
    name: str
    group: str | None = None
    type: str | None = None
    platform: str | None = None
    version: str | None = None
    is_active: bool = True


class MessageOut(BaseModel):
    message: str


class OperationResult(BaseModel):
    message: str
    region: str
    name: str


class CreditCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    credit_id: str | None = Field(default=None, max_length=128)
    total_amount: float = Field(gt=0)
    currency: str = Field(default="USD", max_length=8)
    start_date: date
    expires_at: date | None = None
    note: str | None = None
    credential_id: int | None = None


class CreditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    credit_id: str | None = None
    total_amount: float
    currency: str = "USD"
    start_date: date
    expires_at: date | None = None
    note: str | None = None
    credential_id: int | None = None
    created_at: datetime | None = None
    # 估算值：仅在 /billing/summary 里由 Cost Explorer 算出；
    # CE 不可用时为 None。/billing/credits 列表里同样可能为 None。
    remaining_estimate: float | None = None
    credits_applied_since_start: float | None = None


class BillingServiceCost(BaseModel):
    service: str
    total: float
    net: float
    credits_applied: float
    currency: str = "USD"


class AwsCredit(BaseModel):
    """AWS Billing GetCredits 返回的一条官方赠金（真实余额，非估算）。

    remaining_amount：截至上个账单周期的余额；
    estimated_amount：含未出账单的估算剩余。
    """

    credit_id: str | None = None
    description: str | None = None
    credit_type: str | None = None
    status: str | None = None
    initial_amount: float | None = None
    remaining_amount: float | None = None
    estimated_amount: float | None = None
    currency: str = "USD"
    start_date: str | None = None
    end_date: str | None = None
    exhaust_date: str | None = None
    applicable_products: list[str] = []


class BillingSummary(BaseModel):
    # ok / unavailable（CE 未启用或暂无数据）/ no_permission（缺 IAM 权限）
    status: str = "ok"
    message: str | None = None
    year_month: str
    currency: str = "USD"
    total_cost: float | None = None
    net_cost: float | None = None
    credits_applied: float | None = None
    lightsail_cost: float | None = None
    forecast_cost: float | None = None
    by_service: list[BillingServiceCost] = []
    # 手动录入的赠金记录（补充，剩余是估算）；与下方官方赠金分开。
    credits: list[CreditOut] = []
    # AWS Billing GetCredits 返回的官方赠金；status 为
    # ok / unavailable / no_permission / invalid（参数无效，400 级）。
    aws_credits: list[AwsCredit] = []
    aws_credits_status: str = "unavailable"
    aws_credits_error: str | None = None
    aws_account_id: str | None = None
    required_permissions: list[str] = []


TokenResponse.model_rebuild()
