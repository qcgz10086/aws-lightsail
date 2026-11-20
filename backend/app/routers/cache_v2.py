"""Native snapshot reads and read-only refresh requests; not resource mutation tasks."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse

from app.database import get_db
from app.deps import get_current_user
from app.models import User
from app import query_models  # Register read-query tables before test/new-install create_all.
from app.services.aws_clients import select_scan_credentials
from app.services.instance_cache import instance_scopes
from app.services.billing_summary import summary

router = APIRouter(prefix="/v2", tags=["snapshots"])


def selected(user, credential_id):
    credentials = list(user.credentials)
    if credential_id is not None:
        credentials = [credential for credential in credentials if credential.id == credential_id]
        if not credentials:
            raise HTTPException(status_code=404, detail="凭证不存在")
        return credentials
    return select_scan_credentials(credentials)


@router.get("/instances")
async def instances(credential_id: int | None = None, region: str | None = Query(None, min_length=1, max_length=32, pattern=r"^[a-z0-9-]+$"), wait: bool = False,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    credentials = selected(user, credential_id)
    db.commit()  # Release the authenticated read transaction before refresh work.
    try:
        scopes = await instance_scopes(db.get_bind(), user, credentials, wait=wait, region=region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Raw AWS snapshots do not cache user-editable policy settings; resolve once
    # using the current session so saving a policy takes effect without an AWS call.
    from app.routers.instances import outputs_for_raw
    labels = {credential.id: credential.account_label for credential in credentials}
    raw = [(scope["credential_id"], labels.get(scope["credential_id"]), item)
           for scope in scopes for item in scope["data"] or []]
    return JSONResponse({"instances": [item.model_dump(mode="json") for item in outputs_for_raw(db, user, raw)],
            "scopes": [{key: value for key, value in scope.items() if key != "data"} for scope in scopes],
            "refreshing": any(scope["refreshing"] for scope in scopes)})


class RefreshRequest(BaseModel):
    kind: str = Field(pattern="^(billing|instances)$")
    credential_id: int | None = None
    region: str | None = Field(None, min_length=1, max_length=32, pattern=r"^[a-z0-9-]+$")


@router.post("/refresh", status_code=202)
async def refresh(body: RefreshRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    credentials = selected(user, body.credential_id)
    db.commit()
    if body.kind == "billing":
        result = await summary(db, user, body.credential_id, cached=True, force=True)
        return {"kind": body.kind, "accounts": result["accounts"], "accepted": True}
    try:
        result = await instance_scopes(db.get_bind(), user, credentials, force=True, region=body.region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"kind": body.kind, "scopes": [{key: value for key, value in row.items() if key != "data"}
                                           for row in result], "accepted": True}


class QueryRequest(BaseModel):
    mode: str = Field("refresh", pattern="^(refresh|global)$")
    credential_id: int | None = None
    region: str | None = Field(None, min_length=1, max_length=32, pattern=r"^[a-z0-9-]+$")


@router.post("/queries", status_code=202)
def create_query(body: QueryRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.query_rounds import submit_query
    credentials = selected(user, body.credential_id)
    user_id, credential_ids = user.id, [credential.id for credential in credentials]
    db.commit()
    try:
        ids = [submit_query(db.get_bind(), user_id, credential_id, mode=body.mode,
            region=body.region, force=True) for credential_id in credential_ids]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"accepted": True, "round_ids": ids}


@router.get("/queries/{round_id}")
def query_status(round_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.query_rounds import QueryNotFound, query_view
    try:
        return query_view(db.get_bind(), user.id, round_id)
    except QueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/queries/{round_id}/cancel")
def cancel_query(round_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.services.query_rounds import QueryNotFound, cancel_query as cancel
    user_id = user.id
    db.commit()
    try:
        return cancel(db.get_bind(), user_id, round_id)
    except QueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/query-instances")
def query_instances(credential_id: int | None = None,
        region: str | None = Query(None, min_length=1, max_length=32, pattern=r"^[a-z0-9-]+$"),
        round_ids: str | None = Query(None, max_length=512),
        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from app.routers.instances import outputs_for_raw
    from app.services.query_rounds import (QueryNotFound, current_query, query_view, region_plan)
    credentials = selected(user, credential_id)
    try:
        from app.services.instance_cache import validate_region
        validate_region(region)
        user_id = user.id
        labels = {credential.id: credential.account_label for credential in credentials}
        ids = [value for value in (round_ids or "").split(",") if value]
        if len(ids) > 10 or any(len(value) != 32 for value in ids):
            raise ValueError("查询轮次参数无效")
        db.commit()
        if round_ids is None:
            ids = [current_query(db.get_bind(), user_id, credential.id, region=region)
                   for credential in credentials]
        rounds = [query_view(db.get_bind(), user_id, value) for value in ids]
        if any(item["credential_id"] not in labels for item in rounds):
            raise QueryNotFound("查询轮次不属于所选凭证范围")
        raw, scopes = [], []
        for item in rounds:
            for target in item["targets"]:
                if region and target["region"] != region:
                    continue
                payload = target.get("data")
                raw.extend((item["credential_id"], labels.get(item["credential_id"]), inst)
                    for inst in (payload or {}).get("instances", []))
                scopes.append({"credential_id": item["credential_id"], "account_id": item["account_id"],
                    "region": target["region"], "status": target["status"],
                    "refreshing": target["status"] in {"queued", "running"},
                    "stale": bool((payload or {}).get("stale")),
                    "last_success_at": (payload or {}).get("last_success_at") or (target["finished_at"] if payload else None),
                    "error": target["error"] or target["auxiliary_error"],
                    "error_status": target["error_code"], "next_retry_at": target["next_retry_at"],
                    "opt_status": target["opt_status"]})
        discovery = [{"credential_id": credential.id, "account_id": credential.account_id,
            **region_plan(db, user, credential)} for credential in credentials]
        return JSONResponse({"instances": [item.model_dump(mode="json") for item in outputs_for_raw(db, user, raw)],
            "scopes": scopes, "rounds": [{key: value for key, value in item.items() if key != "targets"}
                                        for item in rounds], "discovery": discovery,
            "refreshing": any(item["status"] in {"queued", "discovering", "running", "cancelling"}
                              for item in rounds)})
    except QueryNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
