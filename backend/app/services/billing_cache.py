"""Independent billing section snapshots with immutable refresh inputs."""
import asyncio
import time
from dataclasses import dataclass
from datetime import timedelta

from app.services import billing as service
from app.services.snapshots import (SnapshotError, _row_current, _view, credential_failure,
                                    permission_failure, read_snapshot, scope_for)
from app.services.traffic import get_credential_keys, get_credential_proxy_url

TTL = 1800


@dataclass(frozen=True)
class BillingInput:
    account_id: str | None
    access_key: str
    secret_key: str
    proxy: str | None


def _section(view):
    metadata = {key: value for key, value in view.items() if key != "data"}
    if view["data"] is None:
        return {"status": "refreshing" if view["refreshing"] else view.get("error_status") or "unavailable",
                "error": view["error"], "cache": metadata}
    return {"status": "ok", **view["data"], "cache": metadata}


async def cached_account(engine, user, credential, wait=False, force=False):
    account_id = credential.account_id
    output = {"account_id": account_id, "credential_id": credential.id, "account_label": credential.account_label}
    if not account_id:
        return {**output, **{key: {"status": "unknown_identity", "error": "请先校验凭证账号身份"}
                              for key in ("costs", "forecast", "credits")}}
    try:
        ak, sk = get_credential_keys(credential)
        proxy = get_credential_proxy_url(credential)
        inputs = BillingInput(account_id, ak, sk, proxy)
    except Exception as exc:
        return {**output, **{key: {"status": "unavailable", "error": str(exc)}
                              for key in ("costs", "forecast", "credits")}}
    # Cost End and forecast Start change at UTC midnight. Keep those ranges
    # separate even when the previous day's 30-minute TTL has not elapsed.
    as_of = service._today_utc().isoformat()
    identity_scope = scope_for(user, credential, "billing-identity", as_of)

    async def identity_loader():
        try:
            identity = await asyncio.to_thread(service.get_sts_identity, inputs.access_key, inputs.secret_key, inputs.proxy)
            if identity != inputs.account_id:
                raise SnapshotError("STS 身份未知或变化，请重新校验凭证", invalidate=True,
                                    invalidate_credential=True)
            return {"account_id": identity}
        except Exception as exc:
            invalid = getattr(exc, "invalidate", False) or permission_failure(exc)
            credential_invalid = (getattr(exc, "invalidate_credential", False)
                                  or credential_failure(exc))
            status = ("no_permission" if permission_failure(exc) and not credential_invalid
                      else getattr(exc, "status", "unavailable"))
            raise SnapshotError(str(exc), invalidate=invalid,
                                status=status, invalidate_credential=credential_invalid) from exc

    identity = await read_snapshot(engine, identity_scope, TTL, identity_loader, wait=wait, force=force)
    if identity["data"] is None:
        metadata = {key: value for key, value in identity.items() if key != "data"}
        output.update({name: {"status": "refreshing" if identity["refreshing"] else "unavailable",
                              "error": identity["error"], "cache": metadata}
                       for name in ("costs", "forecast", "credits")})
        if identity.get("error_status") == "no_permission":
            # STS action-level denial does not prove the credential or the
            # already-confirmed CE/Billing snapshots are invalid. Show prior
            # values as stale without launching dependent AWS refreshes.
            for name in ("costs", "forecast", "credits"):
                scope = scope_for(user, credential, "billing-" + name, as_of)
                row, current = _row_current(engine, scope)
                if row is None or not current or row.payload is None:
                    continue
                view = _view(row, time.time())
                view.update(stale=True, error=identity["error"], error_status="no_permission")
                output[name] = _section(view)
        return output

    async def load(name):
        # A stale identity may be used only after its coalesced refresh completed.
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot
        if identity["status"] == "stale" and not identity["refreshing"]:
            raise SnapshotError("身份服务暂不可用，保留最近成功快照并暂停刷新")
        if identity["refreshing"]:
            deadline = asyncio.get_running_loop().time() + 25
            while asyncio.get_running_loop().time() < deadline:
                with Session(engine) as local:
                    row = local.get(DataSnapshot, identity_scope["key"])
                    ready = row and row.payload and not row.error and not row.lease_token and row.expires_at > time.time()
                    running = row and row.lease_token and row.lease_until > time.time()
                    # Confirmed identity/auth failure clears its payload. A
                    # temporary STS outage keeps it and must keep section values.
                    invalidated = row is None or row.error_status == "credential_invalid"
                if ready:
                    break
                if not running:
                    raise SnapshotError("身份校验失败，停止本次刷新", invalidate=invalidated,
                                        invalidate_credential=invalidated)
                await asyncio.sleep(0.01)
            else:
                raise SnapshotError("身份校验等待超时，保留最近成功快照")
        try:
            if name == "credits":
                return await asyncio.to_thread(service.get_aws_credits, inputs.access_key, inputs.secret_key,
                    proxy_url=inputs.proxy, expected_account_id=inputs.account_id)
            client = service.make_ce_client(inputs.access_key, inputs.secret_key, inputs.proxy)
            if name == "costs":
                return await asyncio.to_thread(service.get_billing_summary, client, account_id=inputs.account_id)
            return {**await asyncio.to_thread(service.get_forecast_result, client, account_id=inputs.account_id),
                    "basis": "remaining_month", "start_date": (service._today_utc() + timedelta(days=1)).isoformat()}
        except Exception as exc:
            status = ("no_permission" if isinstance(exc, service.BillingNoPermissionError)
                      else "invalid" if isinstance(exc, service.BillingValidationError) else "unavailable")
            invalid = permission_failure(exc)
            raise SnapshotError(str(exc), invalidate=invalid, status=status,
                                invalidate_credential=invalid and credential_failure(exc)) from exc

    async def section(name):
        view = await read_snapshot(engine, scope_for(user, credential, "billing-" + name, as_of), TTL,
                                   lambda: load(name), wait=wait, force=force)
        return _section(view)
    views = await asyncio.gather(*(section(name) for name in ("costs", "forecast", "credits")))
    output["costs"], output["forecast"], output["credits"] = views
    return output
