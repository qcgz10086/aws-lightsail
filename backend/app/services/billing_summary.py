"""Compose account-scoped billing; no manual grant consumption allocation."""
import asyncio
from collections import defaultdict
from decimal import Decimal, localcontext

from app.models import CreditRecord
from app.proxy_config import sanitize_error_message
from app.services import billing as service
from app.services.aws_clients import select_scan_credentials
from app.services.traffic import get_credential_keys, get_credential_proxy_url


def status_error(exc):
    status = ("no_permission" if isinstance(exc, service.BillingNoPermissionError)
              else "invalid" if isinstance(exc, service.BillingValidationError) else "unavailable")
    return {"status": status, "error": sanitize_error_message(str(exc))}


def manual_credit(record):
    return {"id": record.id, "name": record.name, "credit_id": record.credit_id,
            "total_amount": service.decimal_string(record.total_amount_decimal or str(record.total_amount)),
            "amount_precision": record.amount_precision,
            "precision_note": ("历史 REAL 浮点近似值，无法恢复最初输入精度" if record.amount_precision == "legacy_float"
                               else "保留新建记录的十进制金额"),
            "currency": record.currency, "account_id": record.account_id,
            "credential_id": record.credential_id, "start_date": record.start_date.isoformat(),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            "note": record.note, "created_at": record.created_at.isoformat() if record.created_at else None,
            "remaining_estimate": None, "credits_applied_since_start": None,
            "status": "unmatched" if record.account_id else "pending_assignment",
            "balance_included": False}


async def account_summary(credential):
    account_id = credential.account_id
    out = {"account_id": account_id, "credential_id": credential.id,
           "account_label": credential.account_label,
           "costs": {"status": "unknown_identity", "error": "请先校验凭证账号身份"},
           "forecast": {"status": "unknown_identity", "error": "请先校验凭证账号身份"},
           "credits": {"status": "unknown_identity", "error": "请先校验凭证账号身份"}}
    if not account_id:
        return out
    try:
        ak, sk = get_credential_keys(credential)
        proxy = get_credential_proxy_url(credential)
        identity = await asyncio.to_thread(service.get_sts_identity, ak, sk, proxy)
        if identity != account_id:
            raise service.BillingValidationError("STS 身份未知或已变化；未将结果计入原账号，请重新校验凭证")
    except Exception as exc:
        out.update({key: status_error(exc) for key in ("costs", "forecast", "credits")})
        return out

    async def costs():
        try:
            client = service.make_ce_client(ak, sk, proxy)
            return {"status": "ok", **await asyncio.to_thread(service.get_billing_summary, client, account_id=account_id)}
        except Exception as exc:
            return status_error(exc)

    async def forecast():
        try:
            client = service.make_ce_client(ak, sk, proxy)
            return {"status": "ok", **await asyncio.to_thread(service.get_forecast_result, client, account_id=account_id),
                    "basis": "remaining_month", "start_date": service._today_utc().isoformat()}
        except Exception as exc:
            return status_error(exc)

    async def credits():
        try:
            result = await asyncio.to_thread(service.get_aws_credits, ak, sk, proxy_url=proxy,
                                             expected_account_id=account_id)
            return {**result, "status": "ok"}
        except Exception as exc:
            return status_error(exc)

    out["costs"], out["forecast"], out["credits"] = await asyncio.gather(costs(), forecast(), credits())
    return out


def aggregate(accounts):
    """Return per-currency known subtotals, explicitly incomplete on partial errors."""
    buckets = defaultdict(dict)
    statuses = {key: bool(accounts) and all(account[key]["status"] == "ok" for account in accounts)
                for key in ("costs", "forecast", "credits")}
    with localcontext() as context:
        context.prec = 80
        for account in accounts:
            if account["costs"]["status"] == "ok":
                for row in account["costs"]["currencies"]:
                    target = buckets[row["currency"]]
                    for key in ("total_cost", "net_cost", "credits_applied", "refunds", "discounts", "lightsail_cost"):
                        target[key] = target.get(key, Decimal(0)) + service.decimal_amount(row[key])
            if account["forecast"]["status"] == "ok":
                row = account["forecast"]
                target = buckets[row["currency"]]
                target["forecast_remaining"] = target.get("forecast_remaining", Decimal(0)) + service.decimal_amount(row["amount"])
            if account["credits"]["status"] == "ok":
                for credit in account["credits"]["credits"]:
                    target = buckets[credit["currency"]]
                    for field, key in (("remaining_amount", "official_remaining"), ("estimated_amount", "official_estimated")):
                        value = credit[field]
                        if value is None:
                            target[key + "_missing"] = True
                        else:
                            target[key] = target.get(key, Decimal(0)) + service.decimal_amount(value)
        result = []
        for currency, values in sorted(buckets.items()):
            row = {"currency": currency, "costs_complete": statuses["costs"],
                   "forecast_complete": statuses["forecast"], "credits_complete": statuses["credits"]
                   and not values.get("official_remaining_missing") and not values.get("official_estimated_missing")}
            for key in ("total_cost", "net_cost", "credits_applied", "refunds", "discounts", "lightsail_cost",
                        "forecast_remaining", "official_remaining", "official_estimated"):
                row[key] = (service.decimal_string(values[key])
                            if key in values and not values.get(key + "_missing") else None)
            row["forecast_month_total"] = (service.decimal_string(
                values["net_cost"] + values["forecast_remaining"])
                if statuses["costs"] and statuses["forecast"] and "net_cost" in values
                and "forecast_remaining" in values else None)
            result.append(row)
    return result


async def summary(db, user, credential_id=None):
    credentials = list(user.credentials)
    if not credentials:
        raise service.BillingValidationError("请先绑定 AWS 凭证")
    if credential_id is not None:
        credentials = [credential for credential in credentials if credential.id == credential_id]
        if not credentials:
            raise service.BillingValidationError("凭证不存在")
    else:
        credentials = select_scan_credentials(credentials)
    # ORM data and secrets remain in the route's session thread; each account worker
    # only invokes AWS clients in threads, and never uses the database there.
    accounts = []
    for credential in credentials:
        accounts.append(await account_summary(credential))
    rows = db.query(CreditRecord).filter(CreditRecord.user_id == user.id).order_by(
        CreditRecord.start_date.desc(), CreditRecord.id.desc()).all()
    selected_accounts = {account["account_id"] for account in accounts if account["account_id"]}
    pending, manual = [], []
    official = {}
    for account in accounts:
        if account["credits"]["status"] == "ok":
            for credit in account["credits"]["credits"]:
                if credit["credit_id"]:
                    official[(account["account_id"], credit["currency"], credit["credit_id"])] = credit
    for record in rows:
        row = manual_credit(record)
        if not record.account_id:
            pending.append(row)
        elif credential_id is None or record.account_id in selected_accounts:
            match = official.get((record.account_id, record.currency, record.credit_id))
            if match:
                row.update(status="matched_official", official_remaining=match["remaining_amount"],
                           official_estimated=match["estimated_amount"])
            if record.account_id not in selected_accounts:
                row["status"] = "historical_account"
            manual.append(row)
    sections = [account[key]["status"] for account in accounts for key in ("costs", "forecast", "credits")]
    return {"year_month": service._month_bounds()[2], "status": "ok" if sections and all(
        status == "ok" for status in sections) else "partial" if "ok" in sections else "unavailable",
        "accounts": accounts, "currencies": aggregate(accounts), "manual_credits": manual,
        "pending_assignment": pending, "forecast_basis": "remaining_month",
        "note": "金额按币种分别汇总；手动记录不叠加官方余额，无逐笔可靠匹配不估算剩余。"}
