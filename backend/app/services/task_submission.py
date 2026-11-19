"""Shared task submission for v2, legacy routes, and the scheduler."""
import asyncio
import time
import uuid

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.services.task_queue import TaskConflict, queue_for
from app.services.traffic import get_credential, NoCredentialsError

LEGACY_WAIT_SECONDS = 1.0


def submit(db, user, kind, credential_id, *, region=None, resource_name=None,
           payload=None, idempotency_key=None):
    from app.routers.tasks import _request
    try:
        credential = get_credential(user, credential_id)
    except NoCredentialsError:
        raise HTTPException(status_code=404, detail="凭证不存在") from None
    try:
        kind, region, resource_name, normalized, account_lock = _request(
            kind, credential.id, region, resource_name, payload or {})
    except HTTPException as exc:
        if kind == "create" and exc.detail == "实例密码格式不符合要求":
            raise HTTPException(status_code=400, detail=exc.detail) from None
        raise
    try:
        return queue_for(db.get_bind()).accept(db, user, credential, kind, normalized,
            idempotency_key or str(uuid.uuid4()), region=region, resource_name=resource_name,
            account_lock=account_lock, wait=False)
    except TaskConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="任务请求或幂等键无效") from None


async def legacy_result(db, user, state):
    engine, user_id, task_id = db.get_bind(), user.id, state["task_id"]
    db.commit()
    deadline = time.monotonic() + LEGACY_WAIT_SECONDS
    while state["status"] in {"queued", "running"} and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        with Session(engine) as reader:
            state = queue_for(engine).get(reader, user_id, task_id)
    if state["status"] == "succeeded":
        return {**(state["result"] or {}), "task_id": task_id, "status": state["status"],
                "credential_id": state["credential_id"], "message": "操作已确认完成"}
    if state["status"] == "failed":
        raise HTTPException(status_code=400, detail={"message": state["error"] or "任务明确失败",
            "task_id": task_id, "status": state["status"]})
    message = "任务已持久接受，等待完成" if state["status"] in {"queued", "running"} else "任务部分完成或待核对，请查看步骤"
    return JSONResponse(status_code=202, content={**state, "message": message,
        "status_url": f"/api/v2/tasks/{task_id}"}, headers={"Location": f"/api/v2/tasks/{task_id}", "Retry-After": "1"})
