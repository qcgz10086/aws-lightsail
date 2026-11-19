"""账单服务：AWS Billing GetCredits（官方赠金）+ Cost Explorer（本月费用）。

官方赠金（Billing API `GetCredits`，固定 us-east-1）：
- 需要 boto3 >= 1.44.3：旧版 boto3 1.35.86 的 billing 客户端只有
  CreateBillingView / GetBillingView 等操作，没有 GetCredits。
- accountId 不让用户手填：先用 STS GetCallerIdentity 从当前凭证自动取
  12 位账号 ID（需 sts:GetCallerIdentity），再调 billing:GetCredits。
- GetCredits 的 startDate 必须是过去 1 年内的日期，本实现取今天往前
  364 天，endDate 取今天；返回的每条 credit 含 initialAmount /
  remainingAmount / estimatedAmount。
- remainingAmount 是截至上个账单周期的余额，estimatedAmount 是含未出
  账单的估算剩余，两者都由 AWS 官方返回，不是本应用推算的。
- 手动记录不能从账号级 Credit 总额分摊出逐笔剩余。只有可靠的官方
  Credit ID 匹配才引用官方余额，且不重复计入汇总。

Cost Explorer 本身：
- 固定走 us-east-1（CE 只有该区域）。
- 需先在 AWS 控制台启用 Cost Explorer，启用后数据通常 24-48 小时才有。
- 按次计费约 $0.01/次（GetCostAndUsage / GetCostForecast 各算一次）。
- IAM 需要 ce:GetCostAndUsage、ce:GetCostForecast。
"""
from __future__ import annotations

import logging
import boto3
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from collections import defaultdict

from botocore.exceptions import BotoCoreError, ClientError

from app.services.aws_clients import make_client as make_aws_client
from app.proxy_config import sanitize_error_message

logger = logging.getLogger(__name__)

CE_REGION = "us-east-1"
BILLING_REGION = "us-east-1"
REQUIRED_PERMISSIONS = ["ce:GetCostAndUsage", "ce:GetCostForecast"]
BILLING_REQUIRED_PERMISSIONS = [
    "billing:GetCredits",
    "sts:GetCallerIdentity",
]
# GetCredits 的 startDate 必须在过去 1 年内；取 364 天留出余量。
CREDITS_LOOKBACK_DAYS = 364

CE_NOT_ENABLED_HINT = (
    "Cost Explorer 未启用或暂无数据：请先在 AWS 控制台启用 Cost Explorer"
    "（控制台 → Cost Explorer → 启用），启用后数据通常需要 24-48 小时才会出现。"
)
CE_NO_PERMISSION_HINT = (
    "没有 Cost Explorer 查询权限：请给该 IAM 用户/角色添加权限 "
    "ce:GetCostAndUsage、ce:GetCostForecast（建议只读、仅 Cost Explorer）。"
)


class BillingUnavailableError(Exception):
    """CE 未启用 / 暂无数据 / 其他可恢复的查询失败（前端以 status 展示，不 500）。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class BillingNoPermissionError(Exception):
    """IAM 缺少 CE / Billing 权限。"""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class BillingValidationError(Exception):
    """GetCredits 请求参数无效（400 级，中文说明由调用方展示）。

    正常流程下不应触发：accountId 来自 STS，startDate 已按 1 年限制钳制。
    """

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class BillingIdentityError(BillingValidationError):
    """A successful STS response proved that the credential changed accounts."""


def make_sts_client(access_key_id: str, secret_access_key: str, proxy_url: str | None = None):
    """STS 客户端（固定 us-east-1，仅用于 GetCallerIdentity 取账号 ID）。"""
    return make_aws_client("sts", access_key_id, secret_access_key, BILLING_REGION, proxy_url)


def get_sts_identity(access_key_id: str, secret_access_key: str, proxy_url: str | None = None) -> str | None:
    response = make_sts_client(access_key_id, secret_access_key, proxy_url).get_caller_identity()
    account_id = (response or {}).get("Account") or ""
    return account_id if isinstance(account_id, str) and len(account_id) == 12 and account_id.isascii() and account_id.isdigit() else None


def make_billing_client(access_key_id: str, secret_access_key: str, proxy_url: str | None = None):
    """AWS Billing 客户端（固定 us-east-1；需 boto3 >= 1.44.3 才有 GetCredits）。"""
    return make_aws_client("billing", access_key_id, secret_access_key, BILLING_REGION, proxy_url)


def make_ce_client(access_key_id: str, secret_access_key: str, proxy_url: str | None = None):
    """Cost Explorer 客户端（固定 us-east-1）。"""
    return make_aws_client("ce", access_key_id, secret_access_key, CE_REGION, proxy_url)


def _today_utc(now: datetime | None = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.date()


def _month_bounds(now: datetime | None = None) -> tuple[date, date, str]:
    """返回 (本月 1 日, 明天, YYYY-MM)。CE 的 End 是左闭右开，所以用明天。"""
    today = _today_utc(now)
    first = date(today.year, today.month, 1)
    tomorrow = today + timedelta(days=1)
    return first, tomorrow, first.strftime("%Y-%m")


def _next_month_first(first: date) -> date:
    days = monthrange(first.year, first.month)[1]
    nxt = first + timedelta(days=days)
    return date(nxt.year, nxt.month, 1)


def decimal_amount(value) -> Decimal:
    try:
        if value is None or isinstance(value, bool) or len(str(value)) > 128:
            raise ValueError("missing or invalid amount")
        result = Decimal(str(value))
        if not result.is_finite() or abs(result.adjusted()) > 24 or len(result.as_tuple().digits) > 50:
            raise ValueError("non-finite or oversized amount")
        return result
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BillingUnavailableError("金额缺失或格式无效，不能作为零展示") from exc


def decimal_string(value) -> str:
    result = format(decimal_amount(value), "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if result in ("-0", "") else result


def account_filter(account_id):
    if not isinstance(account_id, str) or len(account_id) != 12 or not account_id.isascii() or not account_id.isdigit():
        raise BillingValidationError("须先确认 AWS 账号身份；禁止无账号范围的组织查询")
    return {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account_id]}}


def _classify_client_error(exc: ClientError) -> Exception:
    err = (exc.response or {}).get("Error") or {}
    code = str(err.get("Code") or "")
    message = sanitize_error_message(str(err.get("Message") or "") or str(exc))
    lowered = message.lower()
    if code in {
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedException",
        "NotAuthorized",
        "AuthFailure",
        "SignatureDoesNotMatch",
    } or "not authorized" in lowered or "access denied" in lowered:
        return BillingNoPermissionError(f"{CE_NO_PERMISSION_HINT}（{code}：{message}）", code=code)
    if (
        code in {"DataUnavailableException", "ValidationException"}
        or "dataunavailable" in code.lower()
        or "not enabled" in lowered
        or "cost explorer" in lowered
    ):
        return BillingUnavailableError(f"{CE_NOT_ENABLED_HINT}（{code}：{message}）", code=code)
    return BillingUnavailableError(f"Cost Explorer 查询失败：{message}", code=code)


BILLING_NO_PERMISSION_HINT = (
    "没有查询官方赠金的权限：请给该 IAM 用户/角色添加权限 "
    "billing:GetCredits、sts:GetCallerIdentity（建议只读）。不要用根账号 Access Key。"
)
BILLING_VALIDATION_HINT = (
    "查询官方赠金的参数无效（400）：请检查账号 ID 是否为 12 位数字、"
    "查询起始日期是否在过去 1 年内；若持续出现请稍后再试。"
)


def _classify_billing_error(exc: ClientError) -> Exception:
    """GetCredits / STS 的错误映射：无权限 / 参数无效 / 其他不可用。"""
    err = (exc.response or {}).get("Error") or {}
    code = str(err.get("Code") or "")
    message = sanitize_error_message(str(err.get("Message") or "") or str(exc))
    lowered = message.lower()
    if code in {
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedException",
        "NotAuthorized",
        "AuthFailure",
        "SignatureDoesNotMatch",
    } or "not authorized" in lowered or "access denied" in lowered:
        return BillingNoPermissionError(f"{BILLING_NO_PERMISSION_HINT}（{code}：{message}）", code=code)
    if code in {"ValidationException", "ValidationError", "InvalidParameterValue"} or (
        "validation" in code.lower() or "invalid" in lowered
    ):
        return BillingValidationError(f"{BILLING_VALIDATION_HINT}（{code}：{message}）", code=code)
    return BillingUnavailableError(f"查询官方赠金失败：{message}", code=code)


def _money(amount: dict | None) -> tuple[str | None, str | None]:
    if not isinstance(amount, dict):
        return None, None
    currency = amount.get("currencyCode")
    if not isinstance(currency, str) or not currency.strip():
        raise BillingUnavailableError("官方赠金币种缺失")
    return decimal_string(amount.get("currencyAmount")), currency


def _date_str(value) -> str | None:
    """GetCredits 返回的时间戳（datetime / Unix 秒 / ISO 字符串）-> YYYY-MM-DD。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    return text[:10] if text else None


def _parse_aws_credit(raw: dict) -> dict:
    initial, currency = _money(raw.get("initialAmount"))
    remaining, remaining_currency = _money(raw.get("remainingAmount"))
    estimated, estimated_currency = _money(raw.get("estimatedAmount"))
    currencies = {value for value in (currency, remaining_currency, estimated_currency) if value}
    if len(currencies) != 1:
        raise BillingUnavailableError("官方赠金币种缺失或同笔金额币种不一致")
    products = raw.get("applicableProductNames") or []
    return {
        "credit_id": raw.get("creditId"),
        "description": raw.get("description"),
        "credit_type": raw.get("creditType"),
        "status": raw.get("creditStatus"),
        "initial_amount": initial,
        "remaining_amount": remaining,
        "estimated_amount": estimated,
        "currency": next(iter(currencies)),
        "start_date": _date_str(raw.get("startDate")),
        "end_date": _date_str(raw.get("endDate")),
        "exhaust_date": _date_str(raw.get("exhaustDate")),
        "applicable_products": [str(p) for p in products if p],
    }


def get_aws_credits(
    access_key_id: str,
    secret_access_key: str,
    now: datetime | None = None,
    sts_client=None,
    billing_client=None,
    proxy_url: str | None = None,
    expected_account_id: str | None = None,
) -> dict:
    """用 AWS Billing GetCredits 查官方赠金（真实余额，非估算）。

    流程：STS GetCallerIdentity 取 accountId -> billing.get_credits。
    startDate 取今天往前 364 天（API 要求在过去 1 年内），endDate 取今天。
    返回 {"status": "ok", "account_id": ..., "credits": [...], "error": None}。
    无权限 / 参数无效 / 其他失败时抛 BillingNoPermissionError /
    BillingValidationError / BillingUnavailableError，调用方映射为状态。
    """
    today = _today_utc(now)
    sts = sts_client or make_sts_client(access_key_id, secret_access_key, proxy_url)
    try:
        identity = sts.get_caller_identity()
    except ClientError as exc:
        raise _classify_billing_error(exc) from exc
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        raise BillingUnavailableError(sanitize_error_message(f"STS 查询账号失败：{exc}"),
                                      code=getattr(exc, "code", None)) from exc
    account_id = (identity or {}).get("Account") or ""
    if not isinstance(account_id, str) or not account_id.isascii() or not account_id.isdigit() or len(account_id) != 12:
        raise BillingUnavailableError(
            "STS 返回的账号 ID 无效（应为 12 位数字），无法查询官方赠金。"
        )
    if expected_account_id is not None and account_id != expected_account_id:
        raise BillingIdentityError("当前 STS 身份与已确认账号不同，官方赠金查询已停止")

    start = today - timedelta(days=CREDITS_LOOKBACK_DAYS)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    billing = billing_client or make_billing_client(access_key_id, secret_access_key, proxy_url)
    try:
        resp = billing.get_credits(
            accountId=account_id,
            startDate=start_dt,
            endDate=end_dt,
            payerAccountFlag=False,
        )
    except ClientError as exc:
        raise _classify_billing_error(exc) from exc
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        raise BillingUnavailableError(sanitize_error_message(f"查询官方赠金失败：{exc}"),
                                      code=getattr(exc, "code", None)) from exc

    raw_credits = (resp or {}).get("credits")
    if not isinstance(raw_credits, list):
        raise BillingUnavailableError("官方赠金响应缺少记录列表")
    credits, seen = [], {}
    for raw in raw_credits:
        if raw.get("accountId") and raw["accountId"] != account_id:
            raise BillingValidationError("官方赠金响应含其他账号，拒绝混合汇总")
        credit = _parse_aws_credit(raw)
        identity = credit["credit_id"]
        if identity and identity in seen:
            if seen[identity] != credit:
                raise BillingUnavailableError("相同官方 Credit ID 返回不一致记录")
            continue
        if identity:
            seen[identity] = credit
        credits.append(credit)
    credits.sort(key=lambda c: (c.get("end_date") or "", c.get("credit_id") or ""))
    return {
        "status": "ok",
        "account_id": account_id,
        "credits": credits,
        "error": None,
    }


DISCOUNT_RECORD_TYPES = {"Discount", "BundledDiscount", "EdpDiscount", "PrivateRateDiscount", "RiVolumeDiscount"}


def _cost_pages(client, kwargs):
    seen = set()
    for _ in range(1000):
        try:
            response = client.get_cost_and_usage(**kwargs)
        except ClientError as exc:
            raise _classify_client_error(exc) from exc
        except Exception as exc:
            raise BillingUnavailableError(sanitize_error_message(f"Cost Explorer 查询失败：{exc}"),
                                          code=getattr(exc, "code", None)) from exc
        if not isinstance(response, dict) or not isinstance(response.get("ResultsByTime"), list):
            raise BillingUnavailableError("Cost Explorer 响应缺少期间列表")
        yield response["ResultsByTime"]
        token = response.get("NextPageToken")
        if not token:
            return
        if not isinstance(token, str) or token in seen:
            raise BillingUnavailableError("Cost Explorer 分页标识重复或无效")
        seen.add(token)
        kwargs = {**kwargs, "NextPageToken": token}
    raise BillingUnavailableError("Cost Explorer 分页超过安全上限，拒绝不完整汇总")


def get_billing_summary(client, now: datetime | None = None, account_id: str | None = None) -> dict:
    """Account-scoped signed line items, separated by currency and record type."""
    first, tomorrow, year_month = _month_bounds(now)
    kwargs = {"TimePeriod": {"Start": first.isoformat(), "End": tomorrow.isoformat()},
              "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
              "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"},
                          {"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
              "Filter": account_filter(account_id)}
    totals = defaultdict(lambda: defaultdict(Decimal))
    services = defaultdict(lambda: defaultdict(Decimal))
    found = False
    estimated = False
    with localcontext() as context:
        context.prec = 80
        for periods in _cost_pages(client, kwargs):
            for period in periods:
                groups = period.get("Groups")
                if not isinstance(groups, list):
                    raise BillingUnavailableError("费用缺少记录类型分组，拒绝推算 Credit")
                estimated = estimated or bool(period.get("Estimated"))
                for group in groups:
                    keys = group.get("Keys") or []
                    metric = (group.get("Metrics") or {}).get("UnblendedCost") or {}
                    currency = metric.get("Unit")
                    if len(keys) != 2 or not all(isinstance(key, str) and key for key in keys) or not currency:
                        raise BillingUnavailableError("费用缺少服务、记录类型或币种")
                    amount = decimal_amount(metric.get("Amount"))
                    service, record_type = keys
                    totals[currency][record_type] += amount
                    services[(currency, service)][record_type] += amount
                    found = True
        if not found:
            raise BillingUnavailableError("Cost Explorer 暂无可确认费用记录；缺失数据不能显示为零")

        def amounts(lines):
            credit = lines.get("Credit", Decimal(0))
            refund = lines.get("Refund", Decimal(0))
            discount = sum((value for key, value in lines.items() if key in DISCOUNT_RECORD_TYPES), Decimal(0))
            net = sum(lines.values(), Decimal(0))
            return {"total_cost": decimal_string(net - credit - refund - discount),
                    "net_cost": decimal_string(net), "credits_applied": decimal_string(-credit),
                    "refunds": decimal_string(-refund), "discounts": decimal_string(-discount),
                    "record_types": {key: decimal_string(value) for key, value in sorted(lines.items())}}

        currencies = [{"currency": currency, **amounts(lines)} for currency, lines in sorted(totals.items())]
        by_service = [{"currency": currency, "service": service, **amounts(lines)}
                      for (currency, service), lines in sorted(services.items())]
        for row in currencies:
            row["lightsail_cost"] = decimal_string(sum((decimal_amount(service["total_cost"])
                for service in by_service if service["currency"] == row["currency"]
                and "lightsail" in service["service"].lower()), Decimal(0)))
    single = currencies[0] if len(currencies) == 1 else {}
    return {"year_month": year_month, "currency": single.get("currency", "MULTI"),
            **{key: single.get(key) for key in ("total_cost", "net_cost", "credits_applied", "lightsail_cost")},
            "currencies": currencies, "by_service": by_service, "estimated": estimated,
            "cost_basis": "UnblendedCost line items; Credit/Refund/discount record types separated"}


def get_forecast_result(client, now: datetime | None = None, account_id: str | None = None) -> dict:
    first, _tomorrow, _ym = _month_bounds(now)
    today = _today_utc(now)
    end = _next_month_first(first)
    kwargs = {"TimePeriod": {"Start": today.isoformat(), "End": end.isoformat()},
              "Granularity": "DAILY", "Metric": "UNBLENDED_COST", "Filter": account_filter(account_id)}
    try:
        response = client.get_cost_forecast(**kwargs)
    except ClientError as exc:
        raise _classify_client_error(exc) from exc
    except Exception as exc:
        raise BillingUnavailableError(sanitize_error_message(f"费用预测不可用：{exc}"),
                                      code=getattr(exc, "code", None)) from exc
    total = response.get("Total") or {}
    if not total.get("Unit"):
        raise BillingUnavailableError("预测缺少币种")
    periods = response.get("ForecastResultsByTime")
    if not isinstance(periods, list):
        raise BillingUnavailableError("预测缺少逐日覆盖，无法排除与已发生费用重叠的今天")
    daily = {}
    with localcontext() as context:
        context.prec = 80
        for period in periods:
            try:
                start = date.fromisoformat(period["TimePeriod"]["Start"])
                stop = date.fromisoformat(period["TimePeriod"]["End"])
            except (KeyError, TypeError, ValueError) as exc:
                raise BillingUnavailableError("预测日期格式无效") from exc
            if start < today or stop != start + timedelta(days=1) or stop > end or start in daily:
                raise BillingUnavailableError("预测日期重复、重叠或超出范围")
            daily[start] = decimal_amount(period.get("MeanValue"))
        expected = {today + timedelta(days=offset) for offset in range((end - today).days)}
        if set(daily) != expected:
            raise BillingUnavailableError("逐日预测未覆盖全部剩余日期，拒绝不完整完整月预测")
        # The actual-cost query already includes today; discard its forecast.
        remaining = sum((value for day, value in daily.items() if day > today), Decimal(0))
        return {"amount": decimal_string(remaining), "currency": total["Unit"]}


def get_cost_forecast(client, now: datetime | None = None, account_id: str | None = None):
    try:
        return get_forecast_result(client, now, account_id)["amount"]
    except (BillingUnavailableError, BillingNoPermissionError, BillingValidationError):
        return None


def get_credits_applied_since(client, start_date, now: datetime | None = None, account_id: str | None = None):
    """Compatibility helper for account totals only; never allocates to manual grants."""
    scope = account_filter(account_id)
    start = start_date.date() if isinstance(start_date, datetime) else start_date
    if start > _today_utc(now):
        return "0"
    _first, tomorrow, _ym = _month_bounds(now)
    kwargs = {"TimePeriod": {"Start": start.isoformat(), "End": tomorrow.isoformat()},
              "Granularity": "MONTHLY", "Metrics": ["UnblendedCost"],
              "Filter": {"And": [scope, {"Dimensions": {"Key": "RECORD_TYPE", "Values": ["Credit"]}}]}}
    totals = defaultdict(Decimal)
    with localcontext() as context:
        context.prec = 80
        for periods in _cost_pages(client, kwargs):
            for period in periods:
                metric = (period.get("Total") or {}).get("UnblendedCost") or {}
                if not metric.get("Unit"):
                    raise BillingUnavailableError("Credit 数据缺少币种")
                totals[metric["Unit"]] -= decimal_amount(metric.get("Amount"))
        if len(totals) != 1:
            raise BillingUnavailableError("Credit 数据缺失或包含多币种，不能合并")
        return decimal_string(next(iter(totals.values())))
