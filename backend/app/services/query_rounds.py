"""Durable account-scoped discovery, fixed read rounds and subscription cancellation."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import logging
import re
from threading import Lock
import time
import uuid
import weakref

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, OperationTask, User
from app.settings_models import TrafficTaskCancellation
from app.query_models import QueryAccount, QueryRegion, QueryRound, QueryTarget
from app.proxy_config import sanitize_error_message
from app.services import lightsail as ls
from app.services.identity_validation import identity_scope_for, verify_identity
from app.services.instance_cache import supported_regions, validate_region
from app.services.snapshots import (SnapshotError, SnapshotReadStopped, _current, _error_code,
    _iso, permission_failure, credential_failure, read_snapshot, revalidate_snapshot_views,
    scope_for, version_for, retry_seconds, retry_deadline)
from app.services.traffic import get_credential_keys, get_credential_proxy_url

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "discovering", "running", "cancelling"}
TERMINAL = {"succeeded", "partial", "failed", "cancelled", "invalidated"}
TARGET_DONE = {"succeeded", "failed", "cooldown", "cancelled", "disabled", "invalidated"}
LEASE_SECONDS = 300
SUBSCRIPTION_KIND = "query-subscription"
DISCOVERY_PARENT_KIND = "query-discovery-parent"


class QueryNotFound(LookupError):
    pass


class QueryStopped(SnapshotReadStopped):
    pass


class QueryPaused(QueryStopped):
    pass


def _pause_if_closed(engine):
    with _workers_lock:
        worker = _workers.get(engine)
        if worker is not None and worker.closed:
            raise QueryPaused("服务正在停止，剩余读取将在重启后继续")


def account_key(credential):
    value = credential.account_id
    return value if value and len(value) == 12 and value.isascii() and value.isdigit() else f"credential:{credential.id}"


@contextmanager
def _transaction(engine):
    with Session(engine, expire_on_commit=False) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        yield db
        db.commit()


def _account(db, user_id, key):
    row = db.scalar(select(QueryAccount).where(QueryAccount.user_id == user_id,
                                              QueryAccount.account_key == key))
    if row is None:
        row = QueryAccount(user_id=user_id, account_key=key)
        db.add(row)
        db.flush()
    return row


def _region(db, user_id, key, name):
    row = db.scalar(select(QueryRegion).where(QueryRegion.user_id == user_id,
        QueryRegion.account_key == key, QueryRegion.region == name))
    if row is None:
        row = QueryRegion(user_id=user_id, account_key=key, region=name)
        db.add(row)
        db.flush()
    return row


def region_plan(db, user, credential):
    """Collector contract: pending -> submit discovery; ready/empty never broadens."""
    key = account_key(credential)
    account = db.scalar(select(QueryAccount).where(QueryAccount.user_id == user.id,
                                                   QueryAccount.account_key == key))
    rows = db.scalars(select(QueryRegion).where(QueryRegion.user_id == user.id,
        QueryRegion.account_key == key).order_by(QueryRegion.region)).all()
    pending = account is None or account.discovery_state == "pending"
    active = [row.region for row in rows if row.observed_state == "active"]
    return {"state": "pending" if pending else "ready" if active else "empty",
        "regions": active, "discovery_state": account.discovery_state if account else "pending",
        "last_global_started_at": _iso(account.last_global_started_at) if account else None,
        "last_global_finished_at": _iso(account.last_global_finished_at) if account else None,
        "undiscovered": sum(row.observed_state == "unknown" for row in rows)}


def _add_targets(db, row, user, credential, names):
    for name in sorted(set(names)):
        db.add(QueryTarget(round_id=row.id, region=name,
            snapshot_key=scope_for(user, credential, "query-instances", name)["key"]))
    row.targets_fixed = True


def _subscribe(db, row, subscriber):
    """Durable demand metadata has no payload, cache TTL or refresh lease.

    Keep it in the existing FK/version-bound snapshot registry so v6 databases
    need no table change. Payload eviction cannot erase subscription state.
    A page demand lasts through completion; a task demand is revocable alone.
    """
    name = f"{row.id}/{subscriber}"
    key = hashlib.sha256(f"{SUBSCRIPTION_KIND}/{row.user_id}/{name}".encode()).hexdigest()
    marker = db.get(DataSnapshot, key)
    if marker is None:
        marker = DataSnapshot(key=key, user_id=row.user_id, credential_id=row.credential_id,
            account_id=row.account_id, kind=SUBSCRIPTION_KIND, scope=name,
            version=row.credential_version, payload_bytes=0, expires_at=0,
            next_retry_at=0, lease_until=0, updated_at=time.time())
        db.add(marker)
    else:
        marker.error_status, marker.error, marker.updated_at = None, None, time.time()


def _discovery_parent(db, row):
    marker = db.get(DataSnapshot, hashlib.sha256(
        f"{DISCOVERY_PARENT_KIND}/{row.id}".encode()).hexdigest())
    return marker.error if marker is not None and marker.version == row.credential_version else None


def _share_discovery(db, row, parent):
    db.add(DataSnapshot(key=hashlib.sha256(f"{DISCOVERY_PARENT_KIND}/{row.id}".encode()).hexdigest(),
        user_id=row.user_id, credential_id=row.credential_id, account_id=row.account_id,
        kind=DISCOVERY_PARENT_KIND, scope=row.id, version=row.credential_version,
        error=parent.id, payload_bytes=0, expires_at=0, next_retry_at=0, lease_until=0,
        updated_at=time.time()))
    _subscribe(db, parent, f"round:{row.id}")


def _detach_discovery(db, row):
    parent_id = _discovery_parent(db, row)
    if parent_id is not None:
        db.execute(update(DataSnapshot).where(DataSnapshot.kind == SUBSCRIPTION_KIND,
            DataSnapshot.user_id == row.user_id,
            DataSnapshot.scope == f"{parent_id}/round:{row.id}").values(
                error_status="cancelled", updated_at=time.time()))
        parent = db.get(QueryRound, parent_id)
        if parent is not None:
            _cancel_no_demand(db, parent)


def _has_subscribers(db, row, visited=None, *, include_followers=True):
    visited = set() if visited is None else visited
    if row.id in visited:
        return False
    visited.add(row.id)
    markers = db.scalars(select(DataSnapshot).where(DataSnapshot.kind == SUBSCRIPTION_KIND,
        DataSnapshot.user_id == row.user_id, DataSnapshot.version == row.credential_version,
        DataSnapshot.scope.startswith(row.id + "/"), DataSnapshot.error_status.is_(None))).all()
    for marker in markers:
        subscriber = marker.scope.split("/", 1)[1]
        if subscriber == "page":
            return True
        if include_followers and subscriber.startswith("round:"):
            child = db.get(QueryRound, subscriber[6:])
            if (child is not None and child.user_id == row.user_id
                    and child.account_key == row.account_key
                    and child.status in ACTIVE - {"cancelling"}
                    and _discovery_parent(db, child) == row.id):
                try:
                    _inputs(db, child)
                except QueryStopped:
                    continue
                if _has_subscribers(db, child, visited):
                    return True
        if subscriber.startswith("task:"):
            task_id = subscriber[5:]
            task = db.get(OperationTask, task_id)
            if (task is not None and task.user_id == row.user_id
                    and task.status in {"queued", "running", "reconciliation_required"}
                    and db.get(TrafficTaskCancellation, task_id) is None):
                return True
    return False


def _cancel_no_demand(db, row):
    # Followers authorize the shared catalog only. Once it is fixed, they
    # read their own credential scopes and cannot keep this key's reads alive.
    if row.status in ACTIVE and not _has_subscribers(db, row, include_followers=not row.targets_fixed):
        row.status, row.active_key = "cancelling", None
        db.execute(update(QueryTarget).where(QueryTarget.round_id == row.id,
            QueryTarget.status == "queued").values(status="cancelled", finished_at=time.time()))
        if row.worker_token is None or row.targets_fixed:
            _finish_round(db, row)
        _detach_discovery(db, row)


def release_task_queries(db, user_id, task_id):
    """Detach only this parent, inside the same transaction as its cancellation."""
    markers = db.scalars(select(DataSnapshot).where(DataSnapshot.kind == SUBSCRIPTION_KIND,
        DataSnapshot.user_id == user_id, DataSnapshot.scope.endswith(f"/task:{task_id}"),
        DataSnapshot.error_status.is_(None))).all()
    round_ids = set()
    for marker in markers:
        marker.error_status, marker.updated_at = "cancelled", time.time()
        round_ids.add(marker.scope.split("/", 1)[0])
    db.flush()
    for round_id in round_ids:
        row = db.get(QueryRound, round_id)
        if row is not None:
            _cancel_no_demand(db, row)


def submit_query(engine, user_id, credential_id, *, mode="refresh", region=None, force=False,
                 start=True, subscriber="page"):
    validate_region(region)
    if mode not in {"refresh", "global"}:
        raise ValueError("不支持的查询方式")
    with _transaction(engine) as db:
        user, credential = db.get(User, user_id), db.get(AwsCredential, credential_id)
        if user is None or credential is None or credential.user_id != user_id:
            raise QueryNotFound("凭证不存在")
        key, now = account_key(credential), time.time()
        account = _account(db, user_id, key)
        version = version_for(user, credential)
        global_key = f"{user_id}/{key}/global/*"
        # The account owns one catalog discovery. Each credential keeps its
        # own round, access checks and query-instances payloads.
        parent = db.scalar(select(QueryRound).where(QueryRound.active_key == global_key,
            QueryRound.status.in_(ACTIVE - {"cancelling"})))
        if parent is not None:
            try:
                _inputs(db, parent)
            except QueryStopped as exc:
                _invalidate(db, parent, str(exc))
                parent = None
        existing = db.scalar(select(QueryRound).where(QueryRound.user_id == user_id,
            QueryRound.credential_id == credential_id, QueryRound.credential_version == version,
            QueryRound.account_key == key, QueryRound.status.in_(ACTIVE - {"cancelling"}),
            QueryRound.mode == "global").order_by(QueryRound.created_at))
        if existing is not None:
            round_id = existing.id
        else:
            effective_mode = "global" if parent is not None or mode == "global" or account.discovery_state == "pending" else "refresh"
            selected_region = None if effective_mode == "global" else region
            active_key = f"{user_id}/{key}/{effective_mode}/{selected_region or '*'}"
            if effective_mode != "global" or parent is not None:
                active_key += f"/credential:{credential_id}"
            existing = db.scalar(select(QueryRound).where(QueryRound.active_key == active_key))
            if existing is not None and existing.credential_version == version:
                round_id = existing.id
            else:
                if existing is not None:
                    _invalidate(db, existing, "凭证版本已变化")
                row = QueryRound(id=uuid.uuid4().hex, user_id=user_id, credential_id=credential_id,
                    account_key=key, account_id=credential.account_id, credential_version=version,
                    mode=effective_mode, region_filter=selected_region, active_key=active_key,
                    force_refresh=force, created_at=now, updated_at=now)
                db.add(row)
                db.flush()
                round_id = row.id
                if parent is not None:
                    _share_discovery(db, row, parent)
                elif effective_mode == "global":
                    account.last_global_round_id = row.id
                    account.last_global_started_at = now
                else:
                    plan = region_plan(db, user, credential)
                    _add_targets(db, row, user, credential,
                        [name for name in plan["regions"] if region is None or name == region])
                    if not plan["regions"] or region and region not in plan["regions"]:
                        row.status, row.active_key, row.finished_at = "succeeded", None, now
        _subscribe(db, db.get(QueryRound, round_id), subscriber)
        parent_id = _discovery_parent(db, db.get(QueryRound, round_id))
    if start:
        if parent_id is not None:
            worker_for(engine).start(parent_id)
        worker_for(engine).start(round_id)
    return round_id


def _invalidate(db, row, reason):
    row.status, row.active_key, row.error = "invalidated", None, reason
    row.worker_token, row.lease_until, row.finished_at = None, 0, time.time()
    db.execute(update(QueryTarget).where(QueryTarget.round_id == row.id,
        QueryTarget.status.not_in(TARGET_DONE)).values(status="invalidated", result_json=None,
        error=reason, finished_at=time.time()))
    _detach_discovery(db, row)


def _inputs(db, row):
    user = db.get(User, row.user_id)
    credential = db.get(AwsCredential, row.credential_id) if row.credential_id is not None else None
    if (user is None or credential is None or credential.user_id != row.user_id
            or version_for(user, credential) != row.credential_version):
        raise QueryStopped("凭证、代理或身份版本已变化", status="invalidated")
    if db.scalar(select(DataSnapshot.key).where(DataSnapshot.credential_id == credential.id,
            DataSnapshot.version == row.credential_version, DataSnapshot.kind == "billing-identity",
            DataSnapshot.error_status == "credential_invalid")):
        raise QueryStopped("凭证身份已确认失效", status="credential_invalid")
    scope = scope_for(user, credential, "query-instances", "identity")
    return user, credential, scope


def _owned(db, round_id, token):
    row = db.get(QueryRound, round_id)
    if row is None or row.worker_token != token or row.status not in ACTIVE:
        raise QueryStopped("查询执行所有权已变化", status="invalidated")
    _inputs(db, row)
    return row


def _interest(engine, snapshot_scope):
    """Cancellation removes one subscriber; another live round can finish the read."""
    _pause_if_closed(engine)
    with _transaction(engine) as db:
        if not _current(db, snapshot_scope):
            raise QueryStopped("凭证、代理或身份版本已变化", status="invalidated")
        rows = db.scalars(select(QueryRound).join(QueryTarget, QueryTarget.round_id == QueryRound.id).where(
            QueryTarget.snapshot_key == snapshot_scope["key"], QueryTarget.status.in_({"queued", "running"}),
            QueryRound.status.in_(ACTIVE - {"cancelling"}),
            QueryRound.credential_version == snapshot_scope["version"])).all()
        rows = [row for row in rows if _has_subscribers(db, row, include_followers=False)]
        if not rows:
            raise QueryStopped("本轮后续读取已取消", status="cancelled")
        for row in rows:
            row.lease_until = time.time() + LEASE_SECONDS
            row.updated_at = time.time()


def fetch_region_opt_status(access_key, secret_key, proxy, region):
    from app.services.aws_clients import make_client
    client = make_client("account", access_key, secret_key, "us-east-1", proxy)
    return client.get_region_opt_status(RegionName=region).get("RegionOptStatus") or "UNKNOWN"


def _region_status(engine, scope, key, region, access_key, secret_key, proxy, force):
    user_id = scope["user_id"]
    with Session(engine) as db:
        row = db.scalar(select(QueryRegion).where(QueryRegion.user_id == user_id,
            QueryRegion.account_key == key, QueryRegion.region == region))
        if row and row.opt_checked_at and time.time() - row.opt_checked_at < (60 if force else 900):
            return row.opt_status
    try:
        status = fetch_region_opt_status(access_key, secret_key, proxy, region)
    except SnapshotReadStopped:
        raise
    except Exception:
        # A status API denial/outage is UNKNOWN, not proof the region is disabled.
        status = "UNKNOWN"
    with _transaction(engine) as db:
        if not _current(db, scope):
            raise QueryStopped("凭证、代理或身份版本已变化", status="invalidated")
        row = _region(db, user_id, key, region)
        row.opt_status, row.opt_checked_at = status, time.time()
    return status


def _load_region(engine, round_id, token, name):
    with Session(engine) as db:
        row = _owned(db, round_id, token)
        user, credential, _unused = _inputs(db, row)
        scope = scope_for(user, credential, "query-instances", name)
        identity_scope = identity_scope_for(user, credential)
        access_key, secret_key = get_credential_keys(credential)
        proxy, expected_account, key, force = get_credential_proxy_url(credential), credential.account_id, row.account_key, row.force_refresh
        user_id = row.user_id
    checked_since = time.time()
    async def load():
        from app.services.read_throttle import read_context, read_scope
        from app.services.read_cooldowns import ReadCooldownStore, cooldown_context
        # The outer snapshot context also fences its own lease. Nested checks
        # are accumulated by read_context and inherited by asyncio.to_thread.
        with read_context(read_scope(user_id, expected_account, scope["credential_id"]),
                          lambda: _interest(engine, scope)), cooldown_context(ReadCooldownStore(engine, scope)):
            status = await asyncio.to_thread(_region_status, engine, scope, key, name,
                                           access_key, secret_key, proxy, force)
            if status == "DISABLED":
                return {"instances": [], "instances_complete": False, "opt_status": status,
                        "auxiliary_error": None}
            try:
                result = await asyncio.to_thread(ls.discover_instances_in_region, access_key, secret_key,
                                                name, proxy_url=proxy)
                return {**result, "opt_status": status}
            except SnapshotReadStopped:
                raise
            except Exception as exc:
                if credential_failure(exc):
                    await verify_identity(engine, identity_scope, access_key, secret_key, proxy,
                                          expected_account, checked_since=checked_since,
                                          check=lambda: _interest(engine, scope))
                from botocore.exceptions import ClientError
                code = _error_code(exc, ClientError) or type(exc).__name__
                with _transaction(engine) as db:
                    if _current(db, scope):
                        failure = _region(db, user_id, key, name)
                        failure.error_code = re.sub(r"[^A-Za-z0-9_.-]", "", code)[:64] or "unavailable"
                        failure.error = sanitize_error_message(str(exc))
                        failure.last_failure_at = time.time()
                        deadline = retry_deadline(exc)
                        failure.next_retry_at = deadline if deadline is not None else time.time() + retry_seconds(exc)
                raise SnapshotError(sanitize_error_message(str(exc)), invalidate=permission_failure(exc),
                    status="no_permission" if permission_failure(exc) else "unavailable") from exc
    view = asyncio.run(read_snapshot(engine, scope, 60, load, wait=True, force=force,
                                     check=lambda: _interest(engine, scope)))
    # A concurrent reader owns the shared snapshot lease. Follow only this fixed
    # scope, without claiming new work after its successful result expires.
    while view.get("refreshing"):
        _pause_if_closed(engine)
        with Session(engine) as db:
            _owned(db, round_id, token)
        time.sleep(.05)
        view = revalidate_snapshot_views(engine, [(scope, view)])[0]
    _pause_if_closed(engine)
    return view


def _discover(engine, round_id, token):
    if _follow_discovery(engine, round_id, token):
        return
    with Session(engine) as db:
        row = _owned(db, round_id, token)
        user, credential, _unused = _inputs(db, row)
        access_key, secret_key = get_credential_keys(credential)
        proxy = get_credential_proxy_url(credential)
        scope = scope_for(user, credential, "catalog", "regions")
        force = row.force_refresh
    def check():
        _pause_if_closed(engine)
        with _transaction(engine) as db:
            row = _owned(db, round_id, token)
            if row.status == "cancelling":
                raise QueryStopped("本轮区域发现已取消", status="cancelled")
            if not _has_subscribers(db, row):
                raise QueryStopped("区域发现已没有订阅需求", status="cancelled")
            row.lease_until = time.time() + LEASE_SECONDS
    async def load():
        return await asyncio.to_thread(ls.get_regions, access_key, secret_key, proxy_url=proxy)
    view = asyncio.run(read_snapshot(engine, scope, 86400, load, wait=True, force=force, check=check))
    while view.get("refreshing") or (view.get("data") is None and not view.get("error")
                                     and view.get("status") != "invalidated"):
        check()
        if view.get("refreshing"):
            time.sleep(.05)
            view = revalidate_snapshot_views(engine, [(scope, view)])[0]
        else:
            # A followed lease may expire without a published result. Claim
            # again through the version/revocation fence instead of failing.
            view = asyncio.run(read_snapshot(engine, scope, 86400, load,
                                             wait=True, force=force, check=check))
    _pause_if_closed(engine)
    if view.get("error") or view.get("data") is None:
        raise SnapshotError(view.get("error") or "区域目录尚不可用")
    names = sorted({item["name"] for item in view["data"] if item.get("name") in supported_regions()})
    if not names:
        raise SnapshotError("区域目录未返回可确认的 Lightsail 区域")
    with _transaction(engine) as db:
        row = _owned(db, round_id, token)
        if row.status == "cancelling":
            raise QueryStopped("本轮区域发现已取消", status="cancelled")
        user, credential, _unused = _inputs(db, row)
        _add_targets(db, row, user, credential, names)
        for name in names:
            _region(db, row.user_id, row.account_key, name)
        row.status = "running"
        _cancel_no_demand(db, row)


def _follow_discovery(engine, round_id, token):
    """Share only the fixed account catalog, never another key's read result."""
    with Session(engine) as db:
        parent_id = _discovery_parent(db, _owned(db, round_id, token))
    if parent_id is None:
        return False
    while True:
        _pause_if_closed(engine)
        with _transaction(engine) as db:
            row = _owned(db, round_id, token)
            if row.status == "cancelling" or not _has_subscribers(db, row):
                raise QueryStopped("区域发现已没有订阅需求", status="cancelled")
            row.lease_until, row.updated_at = time.time() + LEASE_SECONDS, time.time()
            parent = db.get(QueryRound, parent_id)
            if parent is None or parent.user_id != row.user_id or parent.account_key != row.account_key:
                raise SnapshotError("共享区域发现已不可用")
            if parent.targets_fixed:
                names = db.scalars(select(QueryTarget.region).where(QueryTarget.round_id == parent.id)).all()
                if not names:
                    raise SnapshotError("共享区域发现未返回可确认的区域")
                user, credential, _unused = _inputs(db, row)
                _add_targets(db, row, user, credential, names)
                row.status = "running"
                _detach_discovery(db, row)
                return True
            if parent.status in TERMINAL | {"cancelling"}:
                raise SnapshotError("共享区域发现未完成：" + (parent.error or parent.status))
        time.sleep(.05)


def _save_target(engine, round_id, token, target_id, view):
    with _transaction(engine) as db:
        row = _owned(db, round_id, token)
        target = db.get(QueryTarget, target_id)
        if target is None or target.status != "running":
            return
        now = time.time()
        target.finished_at = now
        data = view.get("data")
        discovery = _region(db, row.user_id, row.account_key, target.region)
        if view.get("status") == "invalidated" or view.get("error_status") == "credential_invalid":
            _invalidate(db, row, "凭证身份或版本已变化")
            return
        if view.get("error") or data is None:
            target.status = "cancelled" if row.status == "cancelling" else "cooldown" if view.get("next_retry_at") else "failed"
            target.error = view.get("error") or "暂无成功数据"
            target.error_code = (discovery.error_code if discovery.last_failure_at and
                discovery.last_failure_at >= (target.started_at or 0) else None) or view.get("error_status") or "unavailable"
            if data is not None:
                target.result_json = json.dumps({**data, "stale": True,
                    "last_success_at": view.get("last_success_at")}, ensure_ascii=False, default=str)
            if view.get("next_retry_at"):
                from datetime import datetime
                target.next_retry_at = datetime.fromisoformat(view["next_retry_at"]).timestamp()
            if row.status != "cancelling":
                discovery.last_failure_at, discovery.error = now, target.error
                discovery.error_code, discovery.next_retry_at = target.error_code, target.next_retry_at
            return
        target.result_json = json.dumps({**data, "stale": False, "last_success_at": view.get("last_success_at")},
                                       ensure_ascii=False, separators=(",", ":"), default=str)
        target.auxiliary_error = data.get("auxiliary_error")
        auxiliary = data.get("auxiliary_failure")
        if auxiliary:
            target.error_code = auxiliary.get("code")
            target.next_retry_at = auxiliary.get("retry_at") or 0
        if data.get("opt_status") == "DISABLED":
            target.status = "disabled"
            return
        target.status = "succeeded"
        instances = data.get("instances") or []
        if data.get("instances_complete") and (instances or not data.get("auxiliary_error")):
            discovery.observed_state = "active" if instances else "empty"
            discovery.last_success_at = now
            discovery.error, discovery.error_code, discovery.next_retry_at = None, None, 0
        if data.get("auxiliary_error"):
            discovery.last_failure_at, discovery.error = now, data["auxiliary_error"]


def _finish_round(db, row):
    targets = db.scalars(select(QueryTarget).where(QueryTarget.round_id == row.id)).all()
    if any(target.status not in TARGET_DONE for target in targets):
        return
    cancelled = row.status == "cancelling" or any(target.status == "cancelled" for target in targets)
    failed = any(target.status in {"failed", "cooldown", "invalidated"} or target.auxiliary_error for target in targets)
    row.status = "cancelled" if cancelled else "failed" if not row.targets_fixed else "partial" if failed and any(
        target.status in {"succeeded", "disabled"} for target in targets) else "failed" if failed else "succeeded"
    row.active_key, row.worker_token, row.lease_until, row.finished_at = None, None, 0, time.time()
    _detach_discovery(db, row)
    if row.mode == "global":
        account = _account(db, row.user_id, row.account_key)
        if account.last_global_round_id == row.id:
            account.discovery_state = "complete" if row.status == "succeeded" else "partial"
            account.last_global_finished_at = row.finished_at


def cancel_query(engine, user_id, round_id):
    with _transaction(engine) as db:
        row = db.get(QueryRound, round_id)
        if row is None or row.user_id != user_id:
            raise QueryNotFound("查询轮次不存在")
        if row.status in ACTIVE:
            db.execute(update(DataSnapshot).where(DataSnapshot.kind == SUBSCRIPTION_KIND,
                DataSnapshot.user_id == user_id, DataSnapshot.scope.startswith(row.id + "/"),
                ~DataSnapshot.scope.startswith(row.id + "/round:")).values(
                    error_status="cancelled", updated_at=time.time()))
            _cancel_no_demand(db, row)
    return query_view(engine, user_id, round_id)


def current_query(engine, user_id, credential_id, *, region=None):
    """A page visit starts at most one round; progress reads use its explicit id."""
    with Session(engine) as db:
        user, credential = db.get(User, user_id), db.get(AwsCredential, credential_id)
        if user is None or credential is None or credential.user_id != user_id:
            raise QueryNotFound("凭证不存在")
        key, version = account_key(credential), version_for(user, credential)
        row = db.scalar(select(QueryRound).where(QueryRound.user_id == user_id,
            QueryRound.account_key == key, QueryRound.credential_id == credential_id,
            QueryRound.credential_version == version,
            (QueryRound.region_filter == region) | (QueryRound.mode == "global")).order_by(
                QueryRound.created_at.desc()))
        reusable = row is not None and (row.status == "cancelled" or time.time() - (row.finished_at or row.created_at) < 60)
        if reusable and row.status not in ACTIVE | {"cancelled"}:
            expired = db.scalar(select(DataSnapshot.key).join(QueryTarget,
                QueryTarget.snapshot_key == DataSnapshot.key).where(QueryTarget.round_id == row.id,
                    DataSnapshot.expires_at <= time.time(), DataSnapshot.error.is_(None)).limit(1))
            reusable = expired is None
        if row is not None and (row.status in ACTIVE or reusable):
            round_id = row.id
        else:
            round_id = None
    if round_id is None:
        round_id = submit_query(engine, user_id, credential_id, region=region)
    elif row.status in ACTIVE:
        with _transaction(engine) as db:
            live = db.get(QueryRound, round_id)
            if live is not None and live.status in ACTIVE - {"cancelling"}:
                _subscribe(db, live, "page")
        worker_for(engine).start(round_id)
    return round_id


def query_view(engine, user_id, round_id):
    with Session(engine) as db:
        # pysqlite does not begin a read transaction for SELECT by default. A
        # real read transaction makes identity, revocation and saved results one
        # consistent response boundary, including after a concurrent deletion.
        connection = db.connection()
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
        row = db.get(QueryRound, round_id)
        if row is None or row.user_id != user_id:
            raise QueryNotFound("查询轮次不存在")
        try:
            _inputs(db, row)
            current = True
        except QueryStopped:
            current = False
        targets = db.scalars(select(QueryTarget).where(QueryTarget.round_id == row.id).order_by(QueryTarget.region)).all()
        region_states = {item.region: item.opt_status for item in db.scalars(select(QueryRegion).where(
            QueryRegion.user_id == user_id, QueryRegion.account_key == row.account_key))}
        output = []
        for target in targets:
            data = json.loads(target.result_json) if current and target.result_json else None
            if current:
                live = db.get(DataSnapshot, target.snapshot_key)
                if data is None and live is not None and live.payload is not None:
                    data = {**json.loads(live.payload), "stale": True, "last_success_at": _iso(live.last_success)}
                elif data is not None and live is not None and live.expires_at <= time.time():
                    data = {**data, "stale": True}
                if live is not None and live.payload is None and live.error_status in {
                        "no_permission", "credential_invalid", "invalid"}:
                    data = None
            output.append({"region": target.region, "status": target.status if current else "invalidated",
                "opt_status": region_states.get(target.region, "UNKNOWN"),
                "error": target.error if current else "凭证、代理或身份版本已变化",
                "error_code": target.error_code, "auxiliary_error": target.auxiliary_error,
                "next_retry_at": _iso(target.next_retry_at) if target.next_retry_at else None,
                "started_at": _iso(target.started_at), "finished_at": _iso(target.finished_at),
                "data": data})
        return {"id": row.id, "credential_id": row.credential_id, "account_id": row.account_id,
            "mode": row.mode, "region_filter": row.region_filter,
            "status": row.status if current else "invalidated", "targets_fixed": row.targets_fixed,
            "total": len(targets) if row.targets_fixed else None,
            "completed": sum(target.status in TARGET_DONE for target in targets),
            "succeeded": sum(target.status == "succeeded" for target in targets),
            "failed": sum(target.status in {"failed", "cooldown", "invalidated"} for target in targets),
            "cancelled": sum(target.status == "cancelled" for target in targets),
            "can_cancel": current and row.status in ACTIVE - {"cancelling"},
            "created_at": _iso(row.created_at), "finished_at": _iso(row.finished_at),
            "error": row.error if current else "凭证、代理或身份版本已变化", "targets": output}


def cleanup_credential_queries(db, user_id, credential):
    key = account_key(credential)
    for row in db.scalars(select(QueryRound).where(QueryRound.user_id == user_id,
            QueryRound.credential_id == credential.id, QueryRound.status.in_(ACTIVE))):
        _invalidate(db, row, "凭证已删除")
    others = db.scalars(select(AwsCredential).where(AwsCredential.user_id == user_id,
                                                  AwsCredential.id != credential.id)).all()
    if not any(account_key(other) == key for other in others):
        db.execute(delete(QueryAccount).where(QueryAccount.user_id == user_id, QueryAccount.account_key == key))
        db.execute(delete(QueryRegion).where(QueryRegion.user_id == user_id, QueryRegion.account_key == key))
        db.execute(update(QueryTarget).where(QueryTarget.round_id.in_(select(QueryRound.id).where(
            QueryRound.user_id == user_id, QueryRound.account_key == key))).values(result_json=None))


class QueryWorker:
    def __init__(self, engine):
        self.engine = engine
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="query-read")
        self.lock = Lock()
        self.pending = set()
        self.closed = False

    def start(self, round_id):
        with self.lock:
            if self.closed or round_id in self.pending:
                return
            self.pending.add(round_id)
        self.pool.submit(self.run, round_id)

    def run(self, round_id):
        token = uuid.uuid4().hex
        try:
            with _transaction(self.engine) as db:
                row = db.get(QueryRound, round_id)
                if row is None or row.status not in ACTIVE or row.worker_token and row.lease_until > time.time():
                    return
                try:
                    _inputs(db, row)
                except QueryStopped as exc:
                    _invalidate(db, row, str(exc))
                    return
                _cancel_no_demand(db, row)
                if row.status in TERMINAL:
                    return
                row.worker_token, row.lease_until = token, time.time() + LEASE_SECONDS
                row.status = "cancelling" if row.status == "cancelling" else "running" if row.targets_fixed else "discovering"
                discover = not row.targets_fixed and row.status != "cancelling"
            if discover:
                _discover(self.engine, round_id, token)
            while True:
                _pause_if_closed(self.engine)
                with _transaction(self.engine) as db:
                    row = _owned(db, round_id, token)
                    _cancel_no_demand(db, row)
                    if row.status in TERMINAL:
                        return
                    if row.status == "cancelling":
                        db.execute(update(QueryTarget).where(QueryTarget.round_id == round_id,
                            QueryTarget.status.not_in(TARGET_DONE)).values(status="cancelled", finished_at=time.time()))
                    target = db.scalar(select(QueryTarget).where(QueryTarget.round_id == round_id,
                        QueryTarget.status == "queued").order_by(QueryTarget.region))
                    if target is None:
                        _finish_round(db, row)
                        return
                    target.status, target.started_at = "running", time.time()
                    target_id, name = target.id, target.region
                view = _load_region(self.engine, round_id, token, name)
                _save_target(self.engine, round_id, token, target_id, view)
        except QueryPaused:
            with _transaction(self.engine) as db:
                row = db.get(QueryRound, round_id)
                if row is not None and row.worker_token == token:
                    row.worker_token, row.lease_until = None, 0
                    row.status = "cancelling" if row.status == "cancelling" else "queued"
                    db.execute(update(QueryTarget).where(QueryTarget.round_id == round_id,
                        QueryTarget.status == "running").values(status="queued"))
        except QueryStopped as exc:
            with _transaction(self.engine) as db:
                row = db.get(QueryRound, round_id)
                if row is not None and row.worker_token == token:
                    if row.status == "cancelling":
                        db.execute(update(QueryTarget).where(QueryTarget.round_id == row.id,
                            QueryTarget.status.not_in(TARGET_DONE)).values(status="cancelled", finished_at=time.time()))
                        _finish_round(db, row)
                    else:
                        if exc.status == "cancelled":
                            row.status = "cancelling"
                            db.execute(update(QueryTarget).where(QueryTarget.round_id == row.id,
                                QueryTarget.status.not_in(TARGET_DONE)).values(status="cancelled", finished_at=time.time()))
                            _finish_round(db, row)
                        else:
                            _invalidate(db, row, str(exc))
        except Exception as exc:
            logger.exception("Read-only query round failed")
            with _transaction(self.engine) as db:
                row = db.get(QueryRound, round_id)
                if row is not None and row.worker_token == token:
                    row.error = sanitize_error_message(str(exc))
                    db.execute(update(QueryTarget).where(QueryTarget.round_id == row.id,
                        QueryTarget.status.not_in(TARGET_DONE)).values(status="failed", error=row.error,
                        finished_at=time.time()))
                    _finish_round(db, row)
        finally:
            with self.lock:
                self.pending.discard(round_id)

    def close(self):
        with self.lock:
            self.closed = True
        self.pool.shutdown(wait=True, cancel_futures=False)


_workers = weakref.WeakKeyDictionary()
_workers_lock = Lock()


def worker_for(engine):
    with _workers_lock:
        worker = _workers.get(engine)
        if worker is None:
            worker = QueryWorker(engine)
            _workers[engine] = worker
        return worker


def resume_queries(engine, *, recover=True):
    """Startup fence: old process tokens cannot publish target results after this."""
    with _transaction(engine) as db:
        rows = db.scalars(select(QueryRound).where(QueryRound.status.in_(ACTIVE))).all()
        for row in rows:
            if recover or row.lease_until <= time.time():
                try:
                    user, credential, _unused = _inputs(db, row)
                except QueryStopped as exc:
                    _invalidate(db, row, str(exc))
                    continue
                # Discovery has no QueryTarget yet. Fence the old process's
                # catalog and identity prerequisites together with target work.
                prerequisites = [identity_scope_for(user, credential)["key"]]
                if not row.targets_fixed and _discovery_parent(db, row) is None:
                    prerequisites.append(scope_for(user, credential, "catalog", "regions")["key"])
                db.execute(update(DataSnapshot).where(DataSnapshot.key.in_(prerequisites),
                    DataSnapshot.version == row.credential_version).values(lease_token=None, lease_until=0))
                running = db.scalars(select(QueryTarget).where(QueryTarget.round_id == row.id,
                                                               QueryTarget.status == "running")).all()
                for target in running:
                    db.execute(update(DataSnapshot).where(DataSnapshot.key == target.snapshot_key).values(
                        lease_token=None, lease_until=0))
                    target.status = "cancelled" if row.status == "cancelling" else "queued"
                row.worker_token, row.lease_until = None, 0
                if row.status != "cancelling":
                    row.status = "queued"
        ids = [row.id for row in sorted(rows, key=lambda item: _discovery_parent(db, item) is not None)]
    for round_id in ids:
        worker_for(engine).start(round_id)
    return ids


def shutdown_queries(engine):
    with _workers_lock:
        worker = _workers.get(engine)
    if worker is not None:
        worker.close()
        with _workers_lock:
            if _workers.get(engine) is worker:
                _workers.pop(engine, None)
