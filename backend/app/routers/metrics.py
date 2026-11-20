from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import AwsCredential, OperationTask, User
from app.proxy_config import ProxyConfigError
from app.schemas import MetricPoint, MetricsSeries, TrafficSummary
from app.services import lightsail as ls
from app.services.collector import sync_user_traffic
from app.services.credential_reads import CredentialReadGuard
from app.services.traffic import (
    NoCredentialsError,
    build_traffic_summary,
    get_credential_proxy_url,
    get_user_aws_keys_and_cred,
)

router = APIRouter(tags=["metrics"])


def _keys(user: User, credential_id: int | None = None):
    """返回 (ak, sk, cred, proxy_url)；代理解密失败同样按 400 处理。"""
    try:
        ak, sk, cred = get_user_aws_keys_and_cred(user, credential_id)
        return ak, sk, cred, get_credential_proxy_url(cred)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/traffic/summary", response_model=TrafficSummary)
def traffic_summary(
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TrafficSummary:
    data = build_traffic_summary(db, user, credential_id)
    from app.services.task_queue import credential_version
    credentials = db.query(AwsCredential).filter(AwsCredential.user_id == user.id)
    if credential_id is not None:
        credentials = credentials.filter(AwsCredential.id == credential_id)
    latest = []
    for credential in credentials.all():
        task = db.query(OperationTask).filter(OperationTask.user_id == user.id,
            OperationTask.credential_id == credential.id, OperationTask.kind == "traffic_sync",
            OperationTask.credential_version == credential_version(credential)).order_by(
                OperationTask.created_at.desc(), OperationTask.id.desc()).first()
        if task:
            latest.append({"task_id": task.id, "credential_id": credential.id,
                "account_id": task.account_id, "status": task.status,
                "created_at": task.created_at.isoformat(), "error": task.error_message,
                "result": json.loads(task.result_json) if task.result_json else None})
    data["latest_syncs"] = latest
    return TrafficSummary(**data)


@router.post("/traffic/sync")
async def traffic_sync(
    credential_id: int | None = Query(None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        states = sync_user_traffic(db, user, credential_id, idempotency_key)
    except NoCredentialsError:
        raise HTTPException(status_code=404, detail="凭证不存在") from None
    return JSONResponse(status_code=202, content={"message": "流量同步任务已接受",
        "tasks": states, "task_id": states[0]["task_id"] if len(states) == 1 else None})


@router.get("/metrics/{region}/{name}", response_model=MetricsSeries)
async def metrics(
    region: str,
    name: str,
    period: str = Query("day", pattern="^(day|week|month)$"),
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MetricsSeries:
    ak, sk, cred, proxy_url = _keys(user, credential_id)
    guard = CredentialReadGuard.capture(db.get_bind(), user, cred)
    now = datetime.now(timezone.utc)
    if period == "day":
        start = now - timedelta(hours=24)
        p = 3600
    elif period == "week":
        start = now - timedelta(days=7)
        p = 3600 * 6
    else:
        start = now - timedelta(days=30)
        p = 86400
    try:
        with guard.context():
            points = await asyncio.to_thread(
                ls.get_metric_series, ak, sk, region, name, start, now, p, proxy_url=proxy_url
            )
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return MetricsSeries(
        region=region,
        name=name,
        period=period,
        points=[MetricPoint(**pt) for pt in points],
    )
