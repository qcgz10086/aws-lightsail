"""账单路由：Cost Explorer 费用 + AWS Billing GetCredits 官方赠金 + 手动赠金记录。

GET /billing/summary 不抛 500：CE 与 GetCredits 各自独立查询、各自返回
状态（status / aws_credits_status），一方失败不影响另一方展示。
官方赠金来自 billing:GetCredits（remainingAmount / estimatedAmount 为
AWS 返回的真实余额）；手动录入的赠金记录是补充，其剩余是估算
（total - 自 start_date 起 CE 累计抵扣额），两者分开，不混成一条。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import CreditRecord, User
from app.schemas import AwsCredit, BillingSummary, CreditCreate, CreditOut
from app.services import billing as billing_svc
from app.services.traffic import (
    NoCredentialsError,
    get_credential_proxy_url,
    get_user_aws_keys_and_cred,
)

router = APIRouter(prefix="/billing", tags=["billing"])


def _credit_out(rec: CreditRecord, applied: float | None = None) -> CreditOut:
    out = CreditOut.model_validate(rec)
    if applied is not None:
        out.credits_applied_since_start = round(float(applied), 2)
        out.remaining_estimate = round(max(0.0, float(rec.total_amount) - float(applied)), 2)
    return out


def _plain_credits(db: Session, user: User) -> list[CreditOut]:
    recs = (
        db.query(CreditRecord)
        .filter(CreditRecord.user_id == user.id)
        .order_by(CreditRecord.start_date.desc(), CreditRecord.id.desc())
        .all()
    )
    return [_credit_out(r) for r in recs]


@router.get("/summary", response_model=BillingSummary)
async def billing_summary(
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BillingSummary:
    try:
        ak, sk, cred = get_user_aws_keys_and_cred(user, credential_id)
        proxy_url = get_credential_proxy_url(cred)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    client = billing_svc.make_ce_client(ak, sk, proxy_url)

    # Cost Explorer 与 GetCredits 各自独立查询，一方失败不影响另一方。
    data: dict | None = None
    forecast: float | None = None
    ce_status = "ok"
    ce_message: str | None = None
    ce_required: list[str] = []
    try:
        data = await asyncio.to_thread(billing_svc.get_billing_summary, client)
        forecast = await asyncio.to_thread(billing_svc.get_cost_forecast, client)
    except billing_svc.BillingNoPermissionError as exc:
        ce_status = "no_permission"
        ce_message = str(exc)
        ce_required = list(billing_svc.REQUIRED_PERMISSIONS)
    except billing_svc.BillingUnavailableError as exc:
        ce_status = "unavailable"
        ce_message = str(exc)

    # 官方赠金：STS 取账号 ID -> Billing GetCredits；失败只影响本区状态。
    aws_credits: list[AwsCredit] = []
    aws_status = "unavailable"
    aws_error: str | None = None
    aws_account_id: str | None = None
    try:
        aws_res = await asyncio.to_thread(billing_svc.get_aws_credits, ak, sk, proxy_url=proxy_url)
        aws_status = str(aws_res.get("status") or "ok")
        aws_account_id = aws_res.get("account_id")
        aws_credits = [AwsCredit(**c) for c in aws_res.get("credits") or []]
    except billing_svc.BillingNoPermissionError as exc:
        aws_status = "no_permission"
        aws_error = str(exc)
    except billing_svc.BillingValidationError as exc:
        # 参数无效（400 级）：只影响官方赠金区，不让整个账单页失败。
        aws_status = "invalid"
        aws_error = str(exc)
    except billing_svc.BillingUnavailableError as exc:
        aws_status = "unavailable"
        aws_error = str(exc)
    except Exception as exc:  # noqa: BLE001 - 官方赠金失败不影响费用汇总
        aws_status = "unavailable"
        aws_error = f"查询官方赠金失败：{exc}"

    # 手动赠金记录的估算：每条记录单独按 start_date 累计抵扣；某条失败
    # 不影响其他条；CE 不可用时估算为 None。
    credits: list[CreditOut] = []
    recs = (
        db.query(CreditRecord)
        .filter(CreditRecord.user_id == user.id)
        .order_by(CreditRecord.start_date.desc(), CreditRecord.id.desc())
        .all()
    )
    for rec in recs:
        applied = None
        if data is not None:
            try:
                applied = await asyncio.to_thread(
                    billing_svc.get_credits_applied_since, client, rec.start_date
                )
            except Exception:  # noqa: BLE001 - 单条估算失败时该条估算为 None
                applied = None
        credits.append(_credit_out(rec, applied))

    return BillingSummary(
        status=ce_status,
        message=ce_message,
        year_month=(data or {}).get("year_month") or billing_svc._month_bounds()[2],
        currency=(data or {}).get("currency") or "USD",
        total_cost=(data or {}).get("total_cost"),
        net_cost=(data or {}).get("net_cost"),
        credits_applied=(data or {}).get("credits_applied"),
        lightsail_cost=(data or {}).get("lightsail_cost"),
        forecast_cost=forecast,
        by_service=(data or {}).get("by_service") or [],
        credits=credits,
        aws_credits=aws_credits,
        aws_credits_status=aws_status,
        aws_credits_error=aws_error,
        aws_account_id=aws_account_id,
        required_permissions=ce_required,
    )


@router.get("/credits", response_model=list[CreditOut])
def list_credits(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CreditOut]:
    # 列表本身不调 CE（避免多次计费调用）；估算值见 /billing/summary。
    return _plain_credits(db, user)


@router.post("/credits", response_model=CreditOut, status_code=201)
def create_credit(
    body: CreditCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreditOut:
    if body.expires_at is not None and body.expires_at < body.start_date:
        raise HTTPException(status_code=400, detail="到期日不能早于开始日期")
    if body.credential_id is not None and not any(
        c.id == body.credential_id for c in user.credentials
    ):
        raise HTTPException(status_code=400, detail=f"凭证不存在: {body.credential_id}")
    rec = CreditRecord(
        user_id=user.id,
        credential_id=body.credential_id,
        name=body.name.strip(),
        credit_id=(body.credit_id or "").strip() or None,
        total_amount=float(body.total_amount),
        currency=(body.currency or "USD").strip().upper() or "USD",
        start_date=body.start_date,
        expires_at=body.expires_at,
        note=(body.note or "").strip() or None,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return _credit_out(rec)


@router.delete("/credits/{credit_id}", response_model=dict)
def delete_credit(
    credit_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    rec = (
        db.query(CreditRecord)
        .filter(CreditRecord.id == credit_id, CreditRecord.user_id == user.id)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="赠金记录不存在")
    db.delete(rec)
    db.commit()
    return {"message": "已删除赠金记录"}
