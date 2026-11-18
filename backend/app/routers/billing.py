"""旧账单结构兼容适配；金额计算与账号隔离使用 v2 共用核心。

旧响应字段结构保留；共用 Decimal 和按账号隔离的 v2 核心。
手动记录不从账号级 Credit 推算逐笔剩余；多币种合计字段为空。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import CreditRecord, User
from app.schemas import BillingSummary, CreditCreate, CreditOut
from app.services import billing as billing_svc

router = APIRouter(prefix="/billing", tags=["billing"])


def _credit_out(rec: CreditRecord) -> CreditOut:
    return CreditOut.model_validate(rec)


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
    from app.services.billing_summary import summary as precise_summary
    try:
        result = await precise_summary(db, user, credential_id)
    except billing_svc.BillingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    account = result["accounts"][0] if len(result["accounts"]) == 1 else None
    currencies = result["currencies"]
    currency = currencies[0] if len(currencies) == 1 else {}
    costs_ok = bool(result["accounts"]) and all(row["costs"]["status"] == "ok" for row in result["accounts"])
    credits_ok = bool(result["accounts"]) and all(row["credits"]["status"] == "ok" for row in result["accounts"])
    def number(key):
        return float(currency[key]) if costs_ok and currency.get(key) is not None else None
    official = []
    services = []
    for row in result["accounts"]:
        if row["credits"]["status"] == "ok":
            official.extend(row["credits"]["credits"])
        if row["costs"]["status"] == "ok":
            for item in row["costs"]["by_service"]:
                services.append({"service": item["service"], "total": item["total_cost"],
                    "net": item["net_cost"], "credits_applied": item["credits_applied"], "currency": item["currency"]})
    records = db.query(CreditRecord).filter(CreditRecord.user_id == user.id).all()
    selected = {row["id"] for row in result["manual_credits"] + result["pending_assignment"]}
    return BillingSummary(status="ok" if costs_ok else "unavailable",
        message="多币种和各账号独立状态请使用 v2；手动记录无逐笔可靠匹配不估算剩余。",
        year_month=result["year_month"], currency=currency.get("currency", "MULTI"),
        total_cost=number("total_cost"), net_cost=number("net_cost"),
        credits_applied=number("credits_applied"), lightsail_cost=number("lightsail_cost"),
        forecast_cost=(float(currency["forecast_month_total"])
                       if currency.get("forecast_month_total") is not None else None),
        by_service=services, credits=[_credit_out(row) for row in records if row.id in selected],
        aws_credits=official, aws_credits_status="ok" if credits_ok else "unavailable",
        aws_credits_error=None if credits_ok else "部分账号官方赠金不可用；详见 v2 独立状态",
        aws_account_id=account["account_id"] if account else None)


@router.get("/credits", response_model=list[CreditOut])
def list_credits(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[CreditOut]:
    # 列表本身不调 AWS；无逐笔可靠匹配时兼容估算字段为空。
    return _plain_credits(db, user)


@router.post("/credits", response_model=CreditOut, status_code=201)
def create_credit(
    body: CreditCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CreditOut:
    from app.routers.billing_v2 import create_record
    return _credit_out(create_record(body, user, db))


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
