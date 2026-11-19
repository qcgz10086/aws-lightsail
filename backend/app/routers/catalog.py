from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.traffic import get_credential
from app.services.instance_cache import cached_call, validate_region

from app.deps import get_current_user
from app.models import User
from app.proxy_config import ProxyConfigError
from app.schemas import BlueprintOut, BundleOut, RegionOut
from app.services import lightsail as ls
from app.services.traffic import (
    NoCredentialsError,
    get_credential_proxy_url,
    get_user_aws_keys_and_cred,
)

router = APIRouter(prefix="/catalog", tags=["catalog"])


def _keys(user: User, credential_id: int | None = None):
    """返回 (ak, sk, proxy_url)；代理解密失败同样按 400 处理。"""
    try:
        ak, sk, cred = get_user_aws_keys_and_cred(user, credential_id)
        return ak, sk, get_credential_proxy_url(cred)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/regions", response_model=list[RegionOut])
async def list_regions(
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[RegionOut]:
    _keys(user, credential_id)
    view = await cached_call(db.get_bind(), user, get_credential(user, credential_id), "catalog", "regions", 86400,
                             ls.get_regions, wait=True)
    if view["data"] is None:
        raise HTTPException(status_code=400, detail=view["error"] or "目录刷新未完成")
    regions = view["data"]
    return [
        RegionOut(
            name=r["name"],
            display_name=r.get("display_name") or r["name"],
            continent_code=r.get("continent_code"),
        )
        for r in regions
    ]


@router.get("/bundles", response_model=list[BundleOut])
async def list_bundles(
    region: str = Query(..., min_length=1, max_length=32),
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BundleOut]:
    try:
        validate_region(region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _keys(user, credential_id)
    view = await cached_call(db.get_bind(), user, get_credential(user, credential_id), "catalog", "bundles:"+region,
                             86400, ls.get_bundles, region, wait=True)
    if view["data"] is None:
        raise HTTPException(status_code=400, detail=view["error"] or "目录刷新未完成")
    items = view["data"]
    return [BundleOut(**b) for b in items if b.get("is_active", True)]


@router.get("/blueprints", response_model=list[BlueprintOut])
async def list_blueprints(
    region: str = Query(..., min_length=1, max_length=32),
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BlueprintOut]:
    try:
        validate_region(region)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _keys(user, credential_id)
    view = await cached_call(db.get_bind(), user, get_credential(user, credential_id), "catalog", "blueprints:"+region,
                             86400, ls.get_blueprints, region, wait=True)
    if view["data"] is None:
        raise HTTPException(status_code=400, detail=view["error"] or "目录刷新未完成")
    items = view["data"]
    active = [b for b in items if b.get("is_active", True)]
    return [BlueprintOut(**b) for b in active]
