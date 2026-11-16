import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.deps import get_current_user
from app.models import User
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
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/regions", response_model=list[RegionOut])
async def list_regions(
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
) -> list[RegionOut]:
    ak, sk, proxy_url = _keys(user, credential_id)
    try:
        regions = await asyncio.to_thread(ls.get_regions, ak, sk, proxy_url=proxy_url)
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
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
    region: str = Query(...),
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
) -> list[BundleOut]:
    ak, sk, proxy_url = _keys(user, credential_id)
    try:
        items = await asyncio.to_thread(ls.get_bundles, ak, sk, region, proxy_url=proxy_url)
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return [BundleOut(**b) for b in items if b.get("is_active", True)]


@router.get("/blueprints", response_model=list[BlueprintOut])
async def list_blueprints(
    region: str = Query(...),
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
) -> list[BlueprintOut]:
    ak, sk, proxy_url = _keys(user, credential_id)
    try:
        items = await asyncio.to_thread(ls.get_blueprints, ak, sk, region, proxy_url=proxy_url)
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    active = [b for b in items if b.get("is_active", True)]
    return [BlueprintOut(**b) for b in active]
