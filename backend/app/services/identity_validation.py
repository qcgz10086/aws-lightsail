"""Coalesced STS confirmation shared by regional and billing refreshes."""
import asyncio
import time

from app.services import billing
from app.services.snapshots import (SnapshotError, SnapshotReadStopped, _row_current, credential_failure,
                                    permission_failure, read_snapshot, revalidate_snapshot_views, scope_for)


def identity_scope_for(user, credential):
    return scope_for(user, credential, "billing-identity", billing._today_utc().isoformat())


async def verify_identity(engine, scope, access_key, secret_key, proxy, expected_account_id,
                          *, checked_since=None, wait=True, force=False, check=None):
    """Reuse a check completed during this refresh, while honoring failure cooldown."""
    from app.services.read_throttle import _current
    inherited_checks = _current.get().checks

    def guard():
        # An identity follower has no snapshot lease of its own yet. Keep the
        # originating refresh/round's authorization and subscription fence while
        # waiting, then run it again immediately before protected re-claim.
        for inherited in inherited_checks:
            inherited()
        if check is not None:
            check()

    if checked_since is not None:
        row, current = _row_current(engine, scope)
        force = bool(current and (row is None or row.updated_at < checked_since))

    async def loader():
        try:
            identity = await asyncio.to_thread(billing.get_sts_identity, access_key, secret_key, proxy)
            if not identity:
                raise SnapshotError("STS 暂未返回可确认的账号身份")
            if expected_account_id and identity != expected_account_id:
                raise SnapshotError("STS 确认账号身份已变化，请重新校验凭证", invalidate=True,
                                    invalidate_credential=True)
            return {"account_id": identity}
        except SnapshotReadStopped:
            raise
        except Exception as exc:
            invalid = getattr(exc, "invalidate_credential", False) or credential_failure(exc)
            denied = permission_failure(exc) and not invalid
            raise SnapshotError(str(exc), invalidate=invalid or denied,
                                status="no_permission" if denied else "unavailable",
                                invalidate_credential=invalid) from exc

    view = await read_snapshot(engine, scope, 1800, loader, wait=wait, force=force, check=guard)
    if not wait:
        return view
    while view.get("refreshing") and view.get("data") is None:
        guard()
        await asyncio.sleep(.01)
        view = revalidate_snapshot_views(engine, [(scope, view)])[0]
    if view.get("data") is not None or view.get("error") or view.get("status") == "invalidated":
        return view
    row, current = _row_current(engine, scope)
    if (not current or row is None or row.payload is not None or row.error
            or row.error_status == "credential_invalid" or row.next_retry_at > time.time()):
        return revalidate_snapshot_views(engine, [(scope, view)])[0]
    guard()
    # This is recovery of an empty prerequisite, not a second force request.
    # The non-forced claim checks the latest payload/expiry inside SQLite's
    # conditional update, so a slower follower reuses a replacement already
    # published even if its earlier orphan observation is now out of date.
    return await read_snapshot(engine, scope, 1800, loader, wait=True, force=False, check=guard)
