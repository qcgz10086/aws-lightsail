from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.deps import get_current_user
from app.models import AwsCredential, OperationTask, User
from app.schemas import InstanceCreate
from app.services.task_queue import TaskConflict, TaskNotFound, queue_for, task_view

router = APIRouter(prefix="/v2/tasks", tags=["tasks"])


class TaskSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["create", "start", "stop", "reboot", "change_ip", "delete", "traffic_sync"]
    credential_id: int
    region: str | None = None
    resource_name: str | None = Field(default=None, max_length=128)
    payload: dict = Field(default_factory=dict)


class TrafficWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_start: datetime | None = None
    window_end: datetime | None = None


class TaskResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    acknowledge_task_id: str


def _credential(db: Session, user: User, credential_id: int) -> AwsCredential:
    credential = db.scalar(select(AwsCredential).where(
        AwsCredential.id == credential_id, AwsCredential.user_id == user.id))
    if credential is None:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return credential


def _request(kind: str, credential_id: int, region: str | None,
             resource_name: str | None, payload: dict) -> tuple[str, str | None, str | None, dict, bool]:
    if kind == "create":
        try:
            if set(payload) - set(InstanceCreate.model_fields):
                raise ValueError("unknown create members")
            body = InstanceCreate.model_validate({**payload, "credential_id": credential_id})
            if ((region is not None and region != body.region) or
                    (resource_name is not None and resource_name != body.instance_name)):
                raise ValueError("conflicting task envelope")
        except Exception as exc:
            raise HTTPException(status_code=422, detail="建机请求字段无效") from exc
        if body.password:
            from app.services.lightsail import build_password_user_data, LightsailError
            try:
                build_password_user_data(body.password, body.platform, body.blueprint_id)
            except LightsailError:
                raise HTTPException(status_code=422, detail="实例密码格式不符合要求") from None
        if not re.fullmatch(r"[a-z0-9-]{3,32}", body.region):
            raise HTTPException(status_code=422, detail="region 无效")
        normalized = body.model_dump(mode="json")
        return kind, body.region, body.instance_name, normalized, False
    if kind in {"start", "stop", "reboot", "change_ip", "delete"}:
        if (not region or not resource_name or payload or
                not re.fullmatch(r"[a-z0-9-]{3,32}", region) or
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", resource_name)):
            raise HTTPException(status_code=422, detail="region 和 resource_name 为必填项")
        normalized = {"kind": kind, "region": region, "resource_name": resource_name,
                     "credential_id": credential_id}
        return kind, region, resource_name, normalized, False
    if kind == "traffic_sync":
        try:
            if region is not None or resource_name is not None:
                raise ValueError("traffic task has account scope")
            window = TrafficWindow.model_validate(payload)
            start, end = window.window_start, window.window_end
            normalized = {"credential_id": credential_id}
            if start is not None or end is not None:
                if not start or not end or not start.tzinfo or not end.tzinfo:
                    raise ValueError("paired UTC window required")
                start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
                if (start >= end or start.day != 1 or any((start.hour, start.minute, start.second, start.microsecond))
                        or start.strftime("%Y-%m") != end.strftime("%Y-%m") or end > datetime.now(timezone.utc)):
                    raise ValueError("invalid traffic window")
                normalized.update(window_start=start.isoformat(), window_end=end.isoformat(),
                                  year_month=start.strftime("%Y-%m"))
        except Exception:
            raise HTTPException(status_code=422, detail="流量窗口必须为同月月初至结束时间，且有时区") from None
        return kind, None, None, normalized, True
    raise HTTPException(status_code=422, detail="unsupported task kind")


@router.post("", status_code=202)
def submit_task(body: TaskSubmit, response: Response,
                idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    credential = _credential(db, user, body.credential_id)
    kind, region, resource_name, payload, account_lock = _request(
        body.kind, body.credential_id, body.region, body.resource_name, body.payload)
    key = idempotency_key
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is required")
    try:
        result = queue_for(db.get_bind()).accept(db, user, credential, kind, payload, key,
                                         region=region, resource_name=resource_name,
                                         account_lock=account_lock, wait=False)
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError:
        raise HTTPException(status_code=422, detail="任务参数或幂等键无效") from None
    response.headers["Location"] = f"/api/v2/tasks/{result['task_id']}"
    response.headers["Retry-After"] = "1"
    return result


@router.get("")
def list_tasks(limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0), user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> list[dict]:
    return queue_for(db.get_bind()).list(db, user.id, limit, offset)


@router.get("/by-key")
def task_by_key(key: str = Query(..., min_length=1, max_length=128),
                user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    task = db.scalar(select(OperationTask).where(OperationTask.user_id == user.id,
                                               OperationTask.idempotency_key == key))
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task_view(db, task)


@router.get("/{task_id}")
def get_task(task_id: str, user: User = Depends(get_current_user),
             db: Session = Depends(get_db)) -> dict:
    try:
        return queue_for(db.get_bind()).get(db, user.id, task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str, user: User = Depends(get_current_user),
                db: Session = Depends(get_db)) -> dict:
    try:
        return queue_for(db.get_bind()).cancel(db, user.id, task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/reconcile")
def reconcile_task(task_id: str, user: User = Depends(get_current_user),
                   db: Session = Depends(get_db)) -> dict:
    try:
        return queue_for(db.get_bind()).reconcile(db, user.id, task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/retry", status_code=202)
def retry_task(task_id: str, user: User = Depends(get_current_user),
               db: Session = Depends(get_db)) -> dict:
    try:
        return queue_for(db.get_bind()).retry(db, user.id, task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{task_id}/resolve")
def resolve_task(task_id: str, body: TaskResolution, user: User = Depends(get_current_user),
                 db: Session = Depends(get_db)) -> dict:
    try:
        return queue_for(db.get_bind()).resolve(db, user.id, task_id, body.acknowledge_task_id)
    except TaskNotFound as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
