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
- 手动录入的赠金记录仍保留（见 routers/billing.py），其剩余是估算：
  remaining_estimate = total_amount - 自 start_date 起 Cost Explorer
  累计已抵扣额（UnblendedCost - NetUnblendedCost），仅作补充。

Cost Explorer 本身：
- 固定走 us-east-1（CE 只有该区域）。
- 需先在 AWS 控制台启用 Cost Explorer，启用后数据通常 24-48 小时才有。
- 按次计费约 $0.01/次（GetCostAndUsage / GetCostForecast 各算一次）。
- IAM 需要 ce:GetCostAndUsage、ce:GetCostForecast。
"""
from __future__ import annotations

import logging
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.proxy_config import proxy_config

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


class BillingNoPermissionError(Exception):
    """IAM 缺少 CE / Billing 权限。"""


class BillingValidationError(Exception):
    """GetCredits 请求参数无效（400 级，中文说明由调用方展示）。

    正常流程下不应触发：accountId 来自 STS，startDate 已按 1 年限制钳制。
    """


def make_sts_client(access_key_id: str, secret_access_key: str, proxy_url: str | None = None):
    """STS 客户端（固定 us-east-1，仅用于 GetCallerIdentity 取账号 ID）。"""
    return boto3.client(
        "sts",
        region_name=BILLING_REGION,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=proxy_config(proxy_url),
    )


def make_billing_client(access_key_id: str, secret_access_key: str, proxy_url: str | None = None):
    """AWS Billing 客户端（固定 us-east-1；需 boto3 >= 1.44.3 才有 GetCredits）。"""
    return boto3.client(
        "billing",
        region_name=BILLING_REGION,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=proxy_config(proxy_url),
    )


def make_ce_client(access_key_id: str, secret_access_key: str, proxy_url: str | None = None):
    """Cost Explorer 客户端（固定 us-east-1）。"""
    return boto3.client(
        "ce",
        region_name=CE_REGION,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=proxy_config(proxy_url),
    )


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


def _amount(metrics: dict | None, key: str) -> float:
    if not metrics:
        return 0.0
    try:
        return float((metrics.get(key) or {}).get("Amount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _currency(metrics: dict | None) -> str:
    if not metrics:
        return "USD"
    for value in metrics.values():
        if isinstance(value, dict) and value.get("Unit"):
            return str(value["Unit"])
    return "USD"


def _round(value: float) -> float:
    return round(float(value), 2)


def _classify_client_error(exc: ClientError) -> Exception:
    err = (exc.response or {}).get("Error") or {}
    code = str(err.get("Code") or "")
    message = str(err.get("Message") or "") or str(exc)
    lowered = message.lower()
    if code in {
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedException",
        "NotAuthorized",
        "AuthFailure",
        "SignatureDoesNotMatch",
    } or "not authorized" in lowered or "access denied" in lowered:
        return BillingNoPermissionError(f"{CE_NO_PERMISSION_HINT}（{code}：{message}）")
    if (
        code in {"DataUnavailableException", "ValidationException"}
        or "dataunavailable" in code.lower()
        or "not enabled" in lowered
        or "cost explorer" in lowered
    ):
        return BillingUnavailableError(f"{CE_NOT_ENABLED_HINT}（{code}：{message}）")
    return BillingUnavailableError(f"Cost Explorer 查询失败：{message}")


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
    message = str(err.get("Message") or "") or str(exc)
    lowered = message.lower()
    if code in {
        "AccessDeniedException",
        "AccessDenied",
        "UnauthorizedException",
        "NotAuthorized",
        "AuthFailure",
        "SignatureDoesNotMatch",
    } or "not authorized" in lowered or "access denied" in lowered:
        return BillingNoPermissionError(f"{BILLING_NO_PERMISSION_HINT}（{code}：{message}）")
    if code in {"ValidationException", "ValidationError", "InvalidParameterValue"} or (
        "validation" in code.lower() or "invalid" in lowered
    ):
        return BillingValidationError(f"{BILLING_VALIDATION_HINT}（{code}：{message}）")
    return BillingUnavailableError(f"查询官方赠金失败：{message}")


def _money(amount: dict | None) -> tuple[float | None, str]:
    """AWS 金额结构 {currencyAmount, currencyCode} -> (float 金额, 币种)。"""
    if not isinstance(amount, dict):
        return None, "USD"
    try:
        value = float(amount.get("currencyAmount"))
    except (TypeError, ValueError):
        return None, str(amount.get("currencyCode") or "USD")
    return value, str(amount.get("currencyCode") or "USD")


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
    remaining, _ = _money(raw.get("remainingAmount"))
    estimated, _ = _money(raw.get("estimatedAmount"))
    products = raw.get("applicableProductNames") or []
    return {
        "credit_id": raw.get("creditId"),
        "description": raw.get("description"),
        "credit_type": raw.get("creditType"),
        "status": raw.get("creditStatus"),
        "initial_amount": _round(initial) if initial is not None else None,
        "remaining_amount": _round(remaining) if remaining is not None else None,
        "estimated_amount": _round(estimated) if estimated is not None else None,
        "currency": currency or "USD",
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
        raise BillingUnavailableError(f"STS 查询账号失败：{exc}") from exc
    account_id = (identity or {}).get("Account") or ""
    if not account_id.isdigit() or len(account_id) != 12:
        raise BillingUnavailableError(
            "STS 返回的账号 ID 无效（应为 12 位数字），无法查询官方赠金。"
        )

    start = today - timedelta(days=CREDITS_LOOKBACK_DAYS)
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    billing = billing_client or make_billing_client(access_key_id, secret_access_key, proxy_url)
    try:
        resp = billing.get_credits(
            accountId=account_id,
            startDate=start_dt,
            endDate=end_dt,
        )
    except ClientError as exc:
        raise _classify_billing_error(exc) from exc
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        raise BillingUnavailableError(f"查询官方赠金失败：{exc}") from exc

    credits = [_parse_aws_credit(c) for c in (resp or {}).get("credits") or []]
    credits.sort(key=lambda c: (c.get("end_date") or "", c.get("credit_id") or ""))
    return {
        "status": "ok",
        "account_id": account_id,
        "credits": credits,
        "error": None,
    }


def get_billing_summary(client, now: datetime | None = None) -> dict:
    """本月费用汇总（MONTHLY，按 SERVICE 分组）。

    返回：year_month / currency / total_cost / net_cost /
    credits_applied / lightsail_cost / by_service。
    CE 不可用或无权限时抛 BillingUnavailableError / BillingNoPermissionError，
    调用方映射为 status，不抛 500。
    """
    first, tomorrow, year_month = _month_bounds(now)
    try:
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": first.isoformat(), "End": tomorrow.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "NetUnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )
    except ClientError as exc:
        raise _classify_client_error(exc) from exc
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        raise BillingUnavailableError(f"Cost Explorer 查询失败：{exc}") from exc

    results = resp.get("ResultsByTime") or []
    if not results:
        raise BillingUnavailableError(
            "Cost Explorer 暂无本月费用数据：数据通常在启用后 24-48 小时才出现，"
            "请稍后再刷新；若刚启用 Cost Explorer，请先确认已在控制台完成启用。"
        )
    period = results[0]
    groups = period.get("Groups") or []
    currency = "USD"
    by_service: list[dict] = []
    total = 0.0
    net = 0.0
    if groups:
        for group in groups:
            service = (group.get("Keys") or ["未知服务"])[0]
            metrics = group.get("Metrics") or {}
            currency = _currency(metrics) or currency
            g_total = _amount(metrics, "UnblendedCost")
            g_net = _amount(metrics, "NetUnblendedCost")
            total += g_total
            net += g_net
            by_service.append(
                {
                    "service": service,
                    "total": _round(g_total),
                    "net": _round(g_net),
                    "credits_applied": _round(g_total - g_net),
                    "currency": currency,
                }
            )
    else:  # 未分组时用 Total 兜底
        metrics = period.get("Total") or {}
        currency = _currency(metrics) or currency
        total = _amount(metrics, "UnblendedCost")
        net = _amount(metrics, "NetUnblendedCost")

    by_service.sort(key=lambda x: x["total"], reverse=True)
    lightsail_cost = sum(
        s["total"] for s in by_service if "lightsail" in s["service"].lower()
    )
    return {
        "year_month": year_month,
        "currency": currency,
        "total_cost": _round(total),
        "net_cost": _round(net),
        "credits_applied": _round(total - net),
        "lightsail_cost": _round(lightsail_cost),
        "by_service": by_service,
    }


def get_cost_forecast(client, now: datetime | None = None) -> float | None:
    """预测本月总费用（UNBLENDED_COST）。失败返回 None，不整体失败。"""
    first, _tomorrow, _ym = _month_bounds(now)
    end = _next_month_first(first)
    try:
        resp = client.get_cost_forecast(
            TimePeriod={"Start": first.isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY",
            Metric="UNBLENDED_COST",
        )
    except Exception as exc:  # noqa: BLE001 - 预测失败不影响主汇总
        logger.warning("Cost Explorer 预测失败: %s", exc)
        return None
    total = (resp or {}).get("Total") or {}
    try:
        amount = float(total.get("Amount"))
    except (TypeError, ValueError):
        return None
    return _round(amount)


def get_credits_applied_since(client, start_date, now: datetime | None = None) -> float:
    """自 start_date 起累计已抵扣赠金 = sum(UnblendedCost - NetUnblendedCost)。

    start_date 可为 date 或 datetime；开始日在未来时返回 0。
    """
    if isinstance(start_date, datetime):
        start = start_date.date()
    else:
        start = start_date
    today = _today_utc(now)
    if start > today:
        return 0.0
    _first, tomorrow, _ym = _month_bounds(now)
    try:
        resp = client.get_cost_and_usage(
            TimePeriod={"Start": start.isoformat(), "End": tomorrow.isoformat()},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost", "NetUnblendedCost"],
        )
    except ClientError as exc:
        raise _classify_client_error(exc) from exc
    except (BotoCoreError, Exception) as exc:  # noqa: BLE001
        raise BillingUnavailableError(f"Cost Explorer 查询失败：{exc}") from exc

    applied = 0.0
    for period in resp.get("ResultsByTime") or []:
        metrics = period.get("Total") or {}
        applied += _amount(metrics, "UnblendedCost") - _amount(metrics, "NetUnblendedCost")
    return _round(applied)
