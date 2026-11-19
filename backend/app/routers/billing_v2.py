"""Decimal-string billing endpoints and explicit manual grant ownership."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import CreditRecord, User
from app.schemas import CreditCreate
from app.services import billing as service
from app.services.billing_summary import manual_credit, summary
from app.services.traffic import get_credential

router = APIRouter(prefix="/v2/billing", tags=["billing-v2"])


@router.get("/summary")
async def billing_summary(credential_id: int | None = None, wait: bool = False, user: User = Depends(get_current_user),
                          db: Session = Depends(get_db)):
    try:
        return await summary(db, user, credential_id, cached=True, wait=wait)
    except service.BillingValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_record(body: CreditCreate, user: User, db: Session):
    if body.expires_at is not None and body.expires_at < body.start_date:
        raise HTTPException(status_code=400, detail="到期日不能早于开始日期")
    account_id = None
    if body.credential_id is not None:
        try:
            credential = get_credential(user, body.credential_id)
        except Exception as exc:
            raise HTTPException(status_code=400, detail="凭证不存在") from exc
        if not credential.account_id:
            raise HTTPException(status_code=409, detail="请先校验所选凭证的 AWS 账号身份")
        account_id = credential.account_id
    name = body.name.strip()
    currency = body.currency.strip().upper()
    if not name or not (3 <= len(currency) <= 8 and currency.isascii() and currency.isalpha()):
        raise HTTPException(status_code=400, detail="名称和币种格式无效")
    record = CreditRecord(user_id=user.id, credential_id=body.credential_id, account_id=account_id,
        name=name, credit_id=(body.credit_id or "").strip() or None,
        total_amount=float(body.total_amount), total_amount_decimal=service.decimal_string(body.total_amount),
        amount_precision="exact_decimal",
        currency=currency, start_date=body.start_date, expires_at=body.expires_at,
        note=(body.note or "").strip() or None)
    db.add(record); db.commit(); db.refresh(record)
    return record


@router.post("/credits", status_code=201)
def create_credit(body: CreditCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return manual_credit(create_record(body, user, db))


class Assignment(BaseModel):
    credential_id: int


@router.patch("/credits/{record_id}/assign")
def assign_credit(record_id: int, body: Assignment, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    record = db.query(CreditRecord).filter(CreditRecord.id == record_id,
                                           CreditRecord.user_id == user.id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="赠金记录不存在")
    try:
        credential = get_credential(user, body.credential_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="凭证不存在") from exc
    if not credential.account_id:
        raise HTTPException(status_code=409, detail="请先确认所选凭证的 AWS 账号身份")
    if record.account_id and record.account_id != credential.account_id:
        raise HTTPException(status_code=409, detail="已归属记录不能因换凭证迁移到其他账号；请新建记录")
    record.credential_id = credential.id
    record.account_id = credential.account_id
    db.add(record); db.commit(); db.refresh(record)
    return manual_credit(record)
