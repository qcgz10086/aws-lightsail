"""Native snapshot reads and read-only refresh requests; not resource mutation tasks."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from fastapi.responses import JSONResponse

from app.database import get_db
from app.deps import get_current_user
from app.models import User
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
