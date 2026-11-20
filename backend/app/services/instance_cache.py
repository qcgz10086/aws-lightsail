"""Region-granular instance snapshots; each successful scope is independently usable."""
import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
import botocore.session

from app.services import lightsail as ls
from app.services.identity_validation import identity_scope_for, verify_identity
from app.services.snapshots import (SnapshotError, SnapshotReadStopped, credential_failure, permission_failure,
                                    read_many, read_snapshot, revalidate_snapshot_views, scope_for, initial_dependency)
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
    checked_since = time.time()
    identity_scope = identity_scope_for(user, credential)
    account_id = credential.account_id
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
            invalid = permission_failure(exc)
            status = "no_permission" if invalid and not credential_failure(exc) else "unavailable"
            if credential_failure(exc):
                await verify_identity(engine, identity_scope, inputs.access_key, inputs.secret_key,
                                      inputs.proxy, account_id, checked_since=checked_since)
            raise SnapshotError(str(exc), invalidate=invalid, status=status) from exc
    return await read_snapshot(engine, scope_for(user, credential, kind, scope), ttl, loader, wait, force)


async def instance_scopes(engine, user, credentials, wait=False, force=False, region=None):
    validate_region(region)
    async def one_credential(credential):
        checked_since = time.time()
        identity_scope = identity_scope_for(user, credential)
        account_id = credential.account_id
        catalog = await cached_call(engine, user, credential, "catalog", "regions", 86400,
                                    ls.get_regions, wait=wait, force=False)
        if not wait:
            catalog = await initial_dependency(engine, scope_for(user, credential, "catalog", "regions"), catalog)
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
            snapshot_scope = scope_for(user, credential, "instances", name)
            async def loader(name=name, snapshot_scope=snapshot_scope):
                from app.services.read_cooldowns import ReadCooldownStore, cooldown_context
                cooldown = ReadCooldownStore(engine, snapshot_scope)
                try:
                    with cooldown_context(cooldown):
                        cooldown.require_available(name, "lightsail", "GetInstances")
                        # Discovery carries this store into every page and
                        # actual SDK send/retry, including after slot queueing.
                        return await asyncio.to_thread(ls.list_instances_in_region, inputs.access_key, inputs.secret_key,
                                                       name, proxy_url=inputs.proxy)
                except SnapshotReadStopped:
                    raise
                except Exception as exc:
                    invalid = permission_failure(exc)
                    status = "no_permission" if invalid and not credential_failure(exc) else "unavailable"
                    if credential_failure(exc):
                        await verify_identity(engine, identity_scope, inputs.access_key, inputs.secret_key,
                                              inputs.proxy, account_id, checked_since=checked_since)
                    raise SnapshotError(str(exc), invalidate=invalid, status=status) from exc
            requests.append((snapshot_scope, 60, loader))
            names.append(name)
        views = await read_many(engine, requests, wait=wait, force=force)
        return [{"credential_id": credential.id, "account_id": credential.account_id, "region": name, **view}
                for name, view in zip(names, views)]
    gathered = await asyncio.gather(*(one_credential(credential) for credential in credentials))
    output = [row for group in gathered for row in group]
    credential_by_id = {credential.id: credential for credential in credentials}
    scoped = []
    for row in output:
        credential = credential_by_id.get(row["credential_id"])
        if credential is None:
            scoped.append((None, row))
            continue
        kind, scope = (("catalog", "regions") if row["region"] is None
                       else ("instances", row["region"]))
        scoped.append((scope_for(user, credential, kind, scope), row))
    live = revalidate_snapshot_views(engine, [(scope, row) for scope, row in scoped if scope is not None])
    live_iter = iter(live)
    return [{**row, **next(live_iter)} if scope is not None else row for scope, row in scoped]
