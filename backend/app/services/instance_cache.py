"""Region-granular instance snapshots; each successful scope is independently usable."""
import asyncio
from dataclasses import dataclass
from functools import lru_cache
import botocore.session

from app.services import lightsail as ls
from app.services.snapshots import SnapshotError, credential_failure, permission_failure, read_many, read_snapshot, scope_for
from app.services.traffic import get_credential_keys, get_credential_proxy_url


@dataclass(frozen=True)
class InstanceInput:
    access_key: str
    secret_key: str
    proxy: str | None


@lru_cache(maxsize=1)
def supported_regions():
    # Endpoint metadata is local to the installed botocore model; no AWS request.
    return frozenset(botocore.session.get_session().get_available_regions("lightsail"))


def validate_region(region):
    if region is not None and region not in supported_regions():
        raise ValueError("不支持的 Lightsail 区域")


async def cached_call(engine, user, credential, kind, scope, ttl, method, *args, wait=False, force=False):
    try:
        ak, sk = get_credential_keys(credential)
        inputs = InstanceInput(ak, sk, get_credential_proxy_url(credential))
    except Exception as exc:
        return {"data": None, "status": "error", "error": str(exc), "refreshing": False,
                "stale": False, "last_success_at": None}
    async def loader():
        try:
            data = await asyncio.to_thread(method, inputs.access_key, inputs.secret_key, *args, proxy_url=inputs.proxy)
            return data
        except Exception as exc:
            raise SnapshotError(str(exc), invalidate=permission_failure(exc),
                                invalidate_credential=credential_failure(exc)) from exc
    return await read_snapshot(engine, scope_for(user, credential, kind, scope), ttl, loader, wait, force)


async def instance_scopes(engine, user, credentials, wait=False, force=False, region=None):
    validate_region(region)
    async def one_credential(credential):
        catalog = await cached_call(engine, user, credential, "catalog", "regions", 86400,
                                    ls.get_regions, wait=wait, force=False)
        if catalog["data"] is None:
            return [{"credential_id": credential.id, "account_id": credential.account_id,
                     "region": None, **catalog}]
        regions = catalog["data"]
        if region:
            regions = [item for item in regions if item.get("name") == region]
            if not regions:
                raise ValueError("该凭证区域目录不包含请求的 Lightsail 区域")
        requests, names = [], []
        ak, sk = get_credential_keys(credential)
        inputs = InstanceInput(ak, sk, get_credential_proxy_url(credential))
        for item in regions:
            name = item["name"]
            if name not in supported_regions():
                continue
            async def loader(name=name):
                try:
                    return await asyncio.to_thread(ls.list_instances_in_region, inputs.access_key, inputs.secret_key,
                                                   name, proxy_url=inputs.proxy)
                except Exception as exc:
                    raise SnapshotError(str(exc), invalidate=permission_failure(exc),
                                        invalidate_credential=credential_failure(exc)) from exc
            requests.append((scope_for(user, credential, "instances", name), 60, loader))
            names.append(name)
        views = await read_many(engine, requests, wait=wait, force=force)
        return [{"credential_id": credential.id, "account_id": credential.account_id, "region": name, **view}
                for name, view in zip(names, views)]
    gathered = await asyncio.gather(*(one_credential(credential) for credential in credentials))
    return [row for group in gathered for row in group]
