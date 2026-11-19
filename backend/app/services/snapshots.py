"""Bounded SQLite snapshots with conditional refresh leases and stale values."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from threading import Event, Lock, Thread
import time
import uuid

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, User
from app.proxy_config import sanitize_error_message

MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
LEASE_SECONDS = 300
RETRY_SECONDS = 15
_background_loop = None
_background_thread = None
_pending = set()
_guard = Lock()
logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    def __init__(self, message, invalidate=False, status="unavailable"):
        super().__init__(message)
        self.invalidate = invalidate
        self.status = status


def version_for(user, credential):
    raw = [user.id, user.token_version, credential.id, credential.account_id,
           credential.access_key_id_enc, credential.secret_access_key_enc, credential.proxy_url_enc]
    return hashlib.sha256(json.dumps(raw, separators=(",", ":")).encode()).hexdigest()


def scope_for(user, credential, kind, scope):
    version = version_for(user, credential)
    values = {"user_id": user.id, "credential_id": credential.id, "account_id": credential.account_id,
              "kind": kind, "scope": scope, "version": version}
    values["key"] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return values


def _current(db, scope):
    row = db.execute(select(User.id, User.token_version, AwsCredential.id, AwsCredential.account_id,
        AwsCredential.access_key_id_enc, AwsCredential.secret_access_key_enc, AwsCredential.proxy_url_enc).join(
        AwsCredential, AwsCredential.user_id == User.id).where(User.id == scope["user_id"],
        AwsCredential.id == scope["credential_id"])).first()
    return bool(row and hashlib.sha256(json.dumps(list(row), separators=(",", ":")).encode()).hexdigest() == scope["version"])


def _row_current(engine, scope):
    # One joined query verifies the current credential version and obtains the
    # payload, avoiding a separate session/query per cache hit.
    with Session(engine) as db:
        result = db.execute(select(User, AwsCredential, DataSnapshot).join(
            AwsCredential, AwsCredential.user_id == User.id).outerjoin(DataSnapshot, DataSnapshot.key == scope["key"]).where(
            User.id == scope["user_id"], AwsCredential.id == scope["credential_id"])).first()
        if not result or version_for(result[0], result[1]) != scope["version"]:
            return None, False
        row = result[2]
        if row is not None:
            db.expunge(row)
        return row, True


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


def _view(row, now, invalidated=False):
    if invalidated:
        return {"data": None, "status": "invalidated", "stale": False, "refreshing": False,
                "last_success_at": None, "error": "凭证、代理或身份版本已变化"}
    refreshing = bool(row.lease_token and row.lease_until > now)
    stale = row.payload is not None and (row.expires_at <= now or bool(row.error))
    return {"data": json.loads(row.payload) if row.payload is not None else None,
            "status": "stale" if stale else "fresh" if row.payload is not None else "refreshing" if refreshing else "error",
            "stale": stale, "refreshing": refreshing, "last_success_at": _iso(row.last_success),
            "expires_at": _iso(row.expires_at), "error": row.error, "error_status": row.error_status}


def permission_failure(exc):
    from botocore.exceptions import ClientError
    from app.services.billing import BillingNoPermissionError, BillingValidationError
    code = (exc.response.get("Error", {}).get("Code", "")
            if isinstance(exc, ClientError) else getattr(exc, "code", ""))
    return (isinstance(exc, (BillingNoPermissionError, BillingValidationError))
            or code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException",
                "AuthFailure", "InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch",
                "ExpiredToken", "ExpiredTokenException", "InvalidAccessKeyId", "InvalidSignatureException"})


def _read_or_claim(engine, scope, force):
    now = time.time()
    row, current = _row_current(engine, scope)
    if not current:
        return _view(None, now, True), None
    if row is not None and row.lease_token and row.lease_until > now:
        return _view(row, now), None
    if row is not None and not force and ((row.payload is not None and row.expires_at > now)
                                          or row.next_retry_at > now):
        return _view(row, now), None
    candidate = uuid.uuid4().hex
    values = {**scope, "payload_bytes": 0, "expires_at": 0, "next_retry_at": 0,
              "lease_token": candidate, "lease_until": now + LEASE_SECONDS, "updated_at": now}
    statement = insert(DataSnapshot).values(**values)
    condition = DataSnapshot.lease_until <= now
    if not force:
        condition = condition & or_(DataSnapshot.payload.is_(None), DataSnapshot.expires_at <= now) & (DataSnapshot.next_retry_at <= now)
    statement = statement.on_conflict_do_update(index_elements=["key"],
        set_={"lease_token": candidate, "lease_until": now + LEASE_SECONDS}, where=condition).returning(DataSnapshot)
    with Session(engine) as db:
        claimed = db.scalars(statement).first()
        view = _view(claimed, now) if claimed else None
        db.commit()
    if view is not None:
        return view, candidate
    row, current = _row_current(engine, scope)
    return (_view(row, time.time()) if row and current else _view(None, now, True)), None


def _publish(engine, scope, token, ttl, **result):
    return _publish_batch(engine, [(scope, token, ttl, result)])[0]


def _publish_batch(engine, entries):
    prepared = []
    for scope, token, ttl, result in entries:
        data, error = result.get("data"), result.get("error")
        payload = None
        if error is not None:
            error = error or "刷新超时或未提供错误详情"
        else:
            try:
                payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_json_default)
                if len(payload.encode()) > MAX_PAYLOAD_BYTES:
                    error = "刷新结果超过64 MiB有效载荷上限"
            except (TypeError, ValueError) as exc:
                error = str(exc)
        prepared.append((scope, token, ttl, result, data, error, payload))
    outputs = []
    # Publish only results already ready in this event-loop turn. Never wait
    # for another network result while holding the SQLite writer reservation.
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        for scope, token, ttl, result, data, error, payload in prepared:
            now = time.time()
            row = connection.exec_driver_sql("""SELECT u.id, u.token_version, c.id, c.account_id,
                c.access_key_id_enc, c.secret_access_key_enc, c.proxy_url_enc, s.payload_bytes
                FROM users AS u JOIN aws_credentials AS c ON c.user_id=u.id
                JOIN data_snapshots AS s ON s.key=:key
                WHERE u.id=:user_id AND c.id=:credential_id AND s.lease_token=:token""",
                {"key": scope["key"], "user_id": scope["user_id"], "credential_id": scope["credential_id"], "token": token}).first()
            if row is None:
                outputs.append(None); continue
            current_version = hashlib.sha256(json.dumps(list(row[:7]), separators=(",", ":")).encode()).hexdigest()
            if current_version != scope["version"]:
                connection.exec_driver_sql("DELETE FROM data_snapshots WHERE key=? AND lease_token=?", (scope["key"], token))
                outputs.append(None); continue
            values = {"lease_token": None, "lease_until": 0, "updated_at": now,
                      "error": sanitize_error_message(error) if error else None,
                      "error_status": result.get("error_status", "unavailable") if error else None}
            if error:
                values["next_retry_at"] = now + RETRY_SECONDS
                if result.get("invalidate", False):
                    where = "credential_id=:credential_id"
                    parameters = {**values, "credential_id": scope["credential_id"]}
                    if scope["kind"] != "billing-identity":
                        where += " AND kind=:kind"
                        parameters["kind"] = scope["kind"]
                    connection.exec_driver_sql("""UPDATE data_snapshots SET payload=NULL,payload_bytes=0,
                        last_success=NULL,expires_at=0,lease_token=NULL,lease_until=0,updated_at=:updated_at,
                        error=:error,error_status=:error_status,next_retry_at=:next_retry_at WHERE """ + where, parameters)
                    values.update(payload=None, payload_bytes=0, last_success=None, expires_at=0)
            else:
                needed = len(payload.encode())
                total = connection.exec_driver_sql("SELECT COALESCE(SUM(payload_bytes),0) FROM data_snapshots").scalar_one() - row[7]
                if total + needed > MAX_PAYLOAD_BYTES:
                    victims = connection.exec_driver_sql("""SELECT key,payload_bytes FROM data_snapshots
                        WHERE key<>? AND payload_bytes>0 ORDER BY updated_at""", (scope["key"],)).all()
                    for victim_key, victim_bytes in victims:
                        if total + needed <= MAX_PAYLOAD_BYTES:
                            break
                        total -= victim_bytes
                        connection.exec_driver_sql("""UPDATE data_snapshots SET payload=NULL,payload_bytes=0,
                            last_success=NULL,expires_at=0,error=? WHERE key=?""", ("缓存容量回收，请重新刷新", victim_key))
                values.update(payload=payload, payload_bytes=needed, last_success=now, expires_at=now + ttl, next_retry_at=0)
            # Column names are fixed internal keys; all runtime data is bound.
            assignments = ",".join(name + "=:" + name for name in values)
            connection.exec_driver_sql("UPDATE data_snapshots SET " + assignments + " WHERE key=:key AND lease_token=:expected_token",
                {**values, "key": scope["key"], "expected_token": token})
            outputs.append({"data": data, "status": "fresh", "stale": False, "refreshing": False,
                "last_success_at": _iso(now), "expires_at": _iso(now + ttl), "error": None,
                "error_status": None} if error is None else None)
    return outputs


async def _publish_ready(engine, scope, token, ttl, **result):
    loop = asyncio.get_running_loop()
    batches = getattr(loop, "_lightsail_snapshot_batches", None)
    if batches is None:
        batches = loop._lightsail_snapshot_batches = {}
    future = loop.create_future()
    if engine not in batches:
        batches[engine] = []
        def flush():
            ready = batches.pop(engine)
            try:
                outputs = _publish_batch(engine, [entry for entry, _future in ready])
            except Exception as exc:
                for _entry, waiter in ready:
                    if not waiter.done():
                        waiter.set_exception(exc)
            else:
                for (_entry, waiter), output in zip(ready, outputs):
                    if not waiter.done():
                        waiter.set_result(output)
        loop.call_soon(flush)
    batches[engine].append(((scope, token, ttl, result), future))
    return await future


def _loop():
    global _background_loop, _background_thread
    with _guard:
        if _background_loop is None:
            ready = Event()
            def run():
                global _background_loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                _background_loop = loop
                ready.set()
                loop.run_forever()
                loop.run_until_complete(loop.shutdown_default_executor())
                loop.close()
            _background_thread = Thread(target=run, name="snapshot-refresh", daemon=True)
            _background_thread.start()
            ready.wait()
        return _background_loop


async def _work(engine, scope, token, ttl, loader):
    try:
        data = await asyncio.wait_for(loader(), timeout=25)
        await _publish_ready(engine, scope, token, ttl, data=data)
    except Exception as exc:
        try:
            _publish(engine, scope, token, ttl, error=str(exc), invalidate=getattr(exc, "invalidate", False),
                     error_status=getattr(exc, "status", "unavailable"))
        except Exception:
            logger.exception("Snapshot refresh failure could not be recorded")
    finally:
        with _guard:
            _pending.discard(scope["key"])


async def read_snapshot(engine, scope, ttl, loader, wait=False, force=False):
    return await finish_snapshot(engine, scope, ttl, loader, wait, *_read_or_claim(engine, scope, force))


async def finish_snapshot(engine, scope, ttl, loader, wait, view, token):
    if token is None and not view["refreshing"]:
        return view
    run_here = bool(token and wait)
    if token:
        if run_here:
            try:
                data = await asyncio.wait_for(loader(), timeout=25)
                published = await _publish_ready(engine, scope, token, ttl, data=data)
                if published is not None:
                    return published
            except Exception as exc:
                _publish(engine, scope, token, ttl, error=str(exc), invalidate=getattr(exc, "invalidate", False),
                         error_status=getattr(exc, "status", "unavailable"))
        else:
            with _guard:
                queued = len(_pending) >= 128
                if not queued:
                    _pending.add(scope["key"])
            if queued:
                _publish(engine, scope, token, ttl, error="刷新队列已满，请稍后重试")
            else:
                asyncio.run_coroutine_threadsafe(_work(engine, dict(scope), token, ttl, loader), _loop())
    if wait and token is not None:
        row, current = _row_current(engine, scope)
        return _view(row, time.time()) if row and current else _view(None, time.time(), True)
    if wait and view["status"] != "invalidated":
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            with Session(engine) as db:
                if not _current(db, scope):
                    return _view(None, time.time(), True)
                row = db.get(DataSnapshot, scope["key"])
                if row is None:
                    return _view(None, time.time(), True)
                view = _view(row, time.time())
            if not view["refreshing"]:
                return view
            await asyncio.sleep(0.01)
    return view


async def read_many(engine, requests, wait=False, force=False):
    """Batch scope/credential reads and lease reservations; never batch AWS failures."""
    if not requests:
        return []
    now = time.time()
    with Session(engine) as db:
        credentials = {credential.id: (user, credential) for user, credential in db.execute(
            select(User, AwsCredential).join(AwsCredential, AwsCredential.user_id == User.id).where(
                AwsCredential.id.in_({scope["credential_id"] for scope, _ttl, _loader in requests})))}
        rows = {row.key: row for row in db.scalars(select(DataSnapshot).where(
            DataSnapshot.key.in_([scope["key"] for scope, _ttl, _loader in requests])))}
        views = []
        for scope, _ttl, _loader in requests:
            pair = credentials.get(scope["credential_id"])
            if not pair or pair[0].id != scope["user_id"] or version_for(*pair) != scope["version"]:
                views.append((_view(None, now, True), None)); continue
            row = rows.get(scope["key"])
            if row and (row.lease_token and row.lease_until > now or not force and (
                    row.payload is not None and row.expires_at > now or row.next_retry_at > now)):
                views.append((_view(row, now), None)); continue
            candidate = uuid.uuid4().hex
            values = {**scope, "payload_bytes": 0, "expires_at": 0, "next_retry_at": 0,
                      "lease_token": candidate, "lease_until": now + LEASE_SECONDS, "updated_at": now}
            condition = DataSnapshot.lease_until <= now
            if not force:
                condition = condition & or_(DataSnapshot.payload.is_(None), DataSnapshot.expires_at <= now) & (DataSnapshot.next_retry_at <= now)
            statement = insert(DataSnapshot).values(**values).on_conflict_do_update(index_elements=["key"],
                set_={"lease_token": candidate, "lease_until": now + LEASE_SECONDS}, where=condition).returning(DataSnapshot)
            claimed = db.scalars(statement.execution_options(populate_existing=True)).first()
            if claimed:
                views.append((_view(claimed, now), candidate))
            else:
                db.expire_all()
                views.append((_view(db.get(DataSnapshot, scope["key"]), now), None))
        db.commit()
    return await asyncio.gather(*(finish_snapshot(engine, scope, ttl, loader, wait, view, token)
        for (scope, ttl, loader), (view, token) in zip(requests, views)))


def invalidate_credential(db, credential_id):
    db.execute(delete(DataSnapshot).where(DataSnapshot.credential_id == credential_id))


def shutdown_refreshes():
    global _background_loop, _background_thread
    with _guard:
        loop, thread = _background_loop, _background_thread
    if loop is not None:
        async def finish():
            pending = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(finish(), loop).result(timeout=60)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=60)
        _background_loop, _background_thread = None, None


def wait_for_refresh(key, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _guard:
            if key not in _pending:
                return
        time.sleep(0.01)
    raise TimeoutError("Snapshot worker did not finish")
