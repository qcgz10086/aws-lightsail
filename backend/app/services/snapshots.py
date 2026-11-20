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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.models import AwsCredential, DataSnapshot, User
from app.proxy_config import sanitize_error_message

MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
LEASE_SECONDS = 300
RETRY_SECONDS = 60
_background_loop = None
_background_thread = None
_pending = set()
_guard = Lock()
logger = logging.getLogger(__name__)


class SnapshotError(Exception):
    def __init__(self, message, invalidate=False, status="unavailable", invalidate_credential=False):
        super().__init__(message)
        self.invalidate = invalidate
        self.status = status
        self.invalidate_credential = invalidate_credential


class SnapshotReadStopped(SnapshotError):
    """A revoked credential or refresh lease cannot send another read."""


def check_snapshot_read(engine, scope, token):
    with Session(engine) as db:
        if not _current(db, scope):
            raise SnapshotReadStopped("凭证、代理或身份版本已变化", status="invalidated")
        row = db.get(DataSnapshot, scope["key"])
        if row is None or row.lease_token != token:
            raise SnapshotReadStopped("查询执行所有权已变化", status="invalidated")
        blocked = db.scalar(select(DataSnapshot.key).where(
            DataSnapshot.credential_id == scope["credential_id"],
            DataSnapshot.version == scope["version"], DataSnapshot.kind == "billing-identity",
            DataSnapshot.error_status == "credential_invalid"))
        if blocked:
            raise SnapshotReadStopped("凭证身份已确认失效", status="credential_invalid")
        # A long throttle queue must not expire an otherwise live refresh. The
        # conditional update also rejects workers superseded by a new owner.
        updated = db.execute(update(DataSnapshot).where(DataSnapshot.key == scope["key"],
            DataSnapshot.lease_token == token).values(lease_until=time.time() + LEASE_SECONDS)).rowcount
        if not updated:
            raise SnapshotReadStopped("查询执行所有权已变化", status="invalidated")
        db.commit()


async def _load_guarded(engine, scope, token, loader, check=None):
    from app.services.read_throttle import read_context, read_scope
    def gate():
        check_snapshot_read(engine, scope, token)
        if check is not None:
            check()
    gate()
    with read_context(read_scope(scope["user_id"], scope["account_id"], scope["credential_id"]), gate):
        return await loader()


def _release_read(engine, scope, token):
    with engine.begin() as connection:
        connection.execute(update(DataSnapshot).where(DataSnapshot.key == scope["key"],
            DataSnapshot.lease_token == token).values(lease_token=None, lease_until=0))


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


def _scope_states(db, scopes):
    """Read payloads, current identities and revocation markers in one statement."""
    blocked = aliased(DataSnapshot)
    versions, rows, invalid = {}, {}, {}
    for user, credential, snapshot, marker in db.execute(select(
            User, AwsCredential, DataSnapshot, blocked).join(
                AwsCredential, AwsCredential.user_id == User.id).outerjoin(
                DataSnapshot, (DataSnapshot.credential_id == AwsCredential.id)
                & DataSnapshot.key.in_([scope["key"] for scope in scopes])).outerjoin(
                blocked, (blocked.credential_id == AwsCredential.id)
                & (blocked.kind == "billing-identity")
                & (blocked.error_status == "credential_invalid")).where(
                AwsCredential.id.in_({scope["credential_id"] for scope in scopes}))):
        versions[(user.id, credential.id)] = version_for(user, credential)
        if snapshot is not None:
            rows[snapshot.key] = snapshot
        if marker is not None:
            invalid[(credential.id, marker.version)] = marker
    return versions, rows, invalid


def _state_view(scope, now, versions, rows, invalid):
    if versions.get((scope["user_id"], scope["credential_id"])) != scope["version"]:
        return _view(None, now, True), None
    marker = invalid.get((scope["credential_id"], scope["version"]))
    if marker is not None:
        return _view(marker, now), marker
    row = rows.get(scope["key"])
    return (_view(row, now) if row is not None else None), row


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
            "expires_at": _iso(row.expires_at), "error": row.error, "error_status": row.error_status,
            "next_retry_at": _iso(row.next_retry_at) if row.next_retry_at else None}


def permission_failure(exc):
    from botocore.exceptions import ClientError
    from app.services.billing import BillingNoPermissionError, BillingValidationError
    code = _error_code(exc, ClientError)
    return (isinstance(exc, (BillingNoPermissionError, BillingValidationError))
            or code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException",
                "AuthFailure", "InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch",
                "ExpiredToken", "ExpiredTokenException", "InvalidAccessKeyId", "InvalidSignatureException"})


def credential_failure(exc):
    from botocore.exceptions import ClientError
    from app.services.billing import BillingIdentityError
    code = _error_code(exc, ClientError)
    return isinstance(exc, BillingIdentityError) or code in {
        "AuthFailure", "InvalidClientTokenId", "UnrecognizedClientException", "SignatureDoesNotMatch",
        "ExpiredToken", "ExpiredTokenException", "InvalidAccessKeyId", "InvalidSignatureException"}


def _error_code(exc, client_error_type):
    if isinstance(exc, client_error_type):
        return exc.response.get("Error", {}).get("Code", "")
    code = getattr(exc, "code", "")
    if code:
        return code
    failure = getattr(exc, "failure", None) or getattr(exc, "read_failure", None)
    if failure and failure.get("code"):
        return failure["code"]
    # Wrapped AWS failures keep their structured code. Do not infer identity
    # failure from a localized/free-form message.
    cause = getattr(exc, "__cause__", None)
    return _error_code(cause, client_error_type) if cause is not None and cause is not exc else ""


def retry_seconds(exc):
    """Keep a longer AWS Retry-After even when a service wraps ClientError."""
    from email.utils import parsedate_to_datetime
    current, seen, delay = exc, set(), RETRY_SECONDS
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        headers = getattr(current, "response", {}).get("ResponseMetadata", {}).get("HTTPHeaders", {})
        value = headers.get("retry-after") or headers.get("Retry-After")
        if value:
            try:
                delay = max(delay, float(value))
            except (ValueError, TypeError):
                try:
                    delay = max(delay, parsedate_to_datetime(value).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        current = getattr(current, "__cause__", None)
    return delay


def retry_deadline(exc):
    """Keep the absolute deadline of an already recorded read failure."""
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        failure = getattr(current, "failure", None) or getattr(current, "read_failure", None)
        if failure and failure.get("retry_at") is not None:
            return failure["retry_at"]
        current = getattr(current, "__cause__", None)
    return None


def _read_or_claim(engine, scope, force):
    now = time.time()
    with Session(engine) as db:
        view, row = _state_view(scope, now, *_scope_states(db, [scope]))
        if view and (view["status"] == "invalidated" or view.get("error_status") == "credential_invalid"):
            return view, None
    if row is not None and row.lease_token and row.lease_until > now:
        return _view(row, now), None
    if row is not None and (row.error_status == "credential_invalid" or row.next_retry_at > now
                            or not force and row.payload is not None and row.expires_at > now):
        return _view(row, now), None
    candidate = uuid.uuid4().hex
    values = {**scope, "payload_bytes": 0, "expires_at": 0, "next_retry_at": 0,
              "lease_token": candidate, "lease_until": now + LEASE_SECONDS, "updated_at": now}
    statement = insert(DataSnapshot).values(**values)
    condition = ((DataSnapshot.lease_until <= now) & (DataSnapshot.next_retry_at <= now)
                 & or_(DataSnapshot.error_status.is_(None), DataSnapshot.error_status != "credential_invalid"))
    if not force:
        condition = condition & or_(DataSnapshot.payload.is_(None), DataSnapshot.expires_at <= now) & (DataSnapshot.next_retry_at <= now)
    statement = statement.on_conflict_do_update(index_elements=["key"],
        set_={"lease_token": candidate, "lease_until": now + LEASE_SECONDS}, where=condition).returning(DataSnapshot)
    with Session(engine) as db:
        try:
            # Acquire SQLite's writer reservation before checking the version.
            # Parent deletion/key rotation cannot commit between this check and
            # lease insertion. The hot path above remains a single read.
            db.execute(update(AwsCredential).where(AwsCredential.id == scope["credential_id"])
                       .values(id=AwsCredential.id))
            if not _current(db, scope):
                return _view(None, time.time(), True), None
            invalid_identity = db.scalar(select(DataSnapshot).where(
                DataSnapshot.credential_id == scope["credential_id"],
                DataSnapshot.version == scope["version"], DataSnapshot.kind == "billing-identity",
                DataSnapshot.error_status == "credential_invalid"))
            if invalid_identity is not None:
                return _view(invalid_identity, time.time()), None
            claimed = db.scalars(statement).first()
        except IntegrityError:
            db.rollback()
            if not _current(db, scope):
                return _view(None, time.time(), True), None
            raise
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
        # An auth failure may suppress sibling results only if this batch entry
        # still owns its lease and the captured credential version is current.
        # A superseded worker must not invalidate a newer lease or its results.
        credential_failures = set()
        for scope, token, _ttl, result, _data, error, _payload in prepared:
            if not (error and result.get("invalidate", False)
                    and result.get("invalidate_credential", False)):
                continue
            current = connection.exec_driver_sql("""SELECT u.id,u.token_version,c.id,c.account_id,
                c.access_key_id_enc,c.secret_access_key_enc,c.proxy_url_enc
                FROM users AS u JOIN aws_credentials AS c ON c.user_id=u.id
                JOIN data_snapshots AS s ON s.key=?
                WHERE u.id=? AND c.id=? AND s.lease_token=?""",
                (scope["key"], scope["user_id"], scope["credential_id"], token)).first()
            if current and hashlib.sha256(json.dumps(list(current), separators=(",", ":")).encode()).hexdigest() == scope["version"]:
                credential_failures.add(scope["credential_id"])
        for scope, token, ttl, result, data, error, payload in prepared:
            now = time.time()
            if scope["credential_id"] in credential_failures and not (
                    error and result.get("invalidate", False)
                    and result.get("invalidate_credential", False)):
                connection.exec_driver_sql("""UPDATE data_snapshots SET lease_token=NULL,lease_until=0,
                    updated_at=?,error=?,error_status=?,next_retry_at=?
                    WHERE key=? AND lease_token=?""",
                    (now, "同批凭证认证已失效，结果已丢弃", "credential_invalid", now + RETRY_SECONDS,
                     scope["key"], token))
                outputs.append(None)
                continue
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
            blocked = connection.exec_driver_sql("""SELECT key FROM data_snapshots
                WHERE credential_id=? AND version=? AND kind='billing-identity'
                AND error_status='credential_invalid' LIMIT 1""",
                (scope["credential_id"], scope["version"])).first()
            if blocked and not result.get("invalidate_credential", False):
                connection.exec_driver_sql("UPDATE data_snapshots SET lease_token=NULL,lease_until=0 WHERE key=? AND lease_token=?",
                                          (scope["key"], token))
                outputs.append(None); continue
            values = {"lease_token": None, "lease_until": 0, "updated_at": now,
                      "error": sanitize_error_message(error) if error else None,
                      "error_status": ("credential_invalid" if result.get("invalidate_credential", False)
                                       else result.get("error_status", "unavailable")) if error else None}
            if error:
                deadline = result.get("retry_at")
                values["next_retry_at"] = (deadline if deadline is not None else
                    now + max(RETRY_SECONDS, result.get("retry_after", RETRY_SECONDS)))
                if result.get("invalidate", False):
                    if result.get("invalidate_credential", False):
                        connection.exec_driver_sql("""UPDATE data_snapshots SET payload=NULL,payload_bytes=0,
                            last_success=NULL,expires_at=0,lease_token=NULL,lease_until=0,updated_at=:updated_at,
                            error=:error,error_status=:error_status,next_retry_at=:next_retry_at
                            WHERE credential_id=:credential_id""",
                            {**values, "credential_id": scope["credential_id"]})
                    else:
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


async def _work(engine, scope, token, ttl, loader, check=None):
    try:
        data = await _load_guarded(engine, scope, token, loader, check)
        await _publish_ready(engine, scope, token, ttl, data=data)
    except SnapshotReadStopped:
        _release_read(engine, scope, token)
    except Exception as exc:
        try:
            _publish(engine, scope, token, ttl, error=str(exc), invalidate=getattr(exc, "invalidate", False),
                     invalidate_credential=getattr(exc, "invalidate_credential", False),
                     error_status=getattr(exc, "status", "unavailable"), retry_after=retry_seconds(exc),
                     retry_at=retry_deadline(exc))
        except Exception:
            logger.exception("Snapshot refresh failure could not be recorded")
    finally:
        with _guard:
            _pending.discard(scope["key"])


async def read_snapshot(engine, scope, ttl, loader, wait=False, force=False, *, check=None):
    result = await finish_snapshot(engine, scope, ttl, loader, wait, *_read_or_claim(engine, scope, force), check=check)
    return revalidate_snapshot_views(engine, [(scope, result)])[0]


async def initial_dependency(engine, scope, view, budget=0.15):
    """Briefly follow a cold prerequisite so its children can start this request.

    Existing values return immediately. A slow or failed prerequisite still
    returns its normal snapshot state within the bounded initial wait.
    """
    deadline = time.monotonic() + budget
    while view['data'] is None and view['refreshing'] and time.monotonic() < deadline:
        await asyncio.sleep(min(0.01, max(0, deadline - time.monotonic())))
        row, current = _row_current(engine, scope)
        view = _view(row, time.time()) if row and current else _view(None, time.time(), True)
    return view


async def finish_snapshot(engine, scope, ttl, loader, wait, view, token, *, check=None):
    if token is None and not view["refreshing"]:
        return view
    run_here = bool(token and wait)
    if token:
        if run_here:
            try:
                data = await _load_guarded(engine, scope, token, loader, check)
                published = await _publish_ready(engine, scope, token, ttl, data=data)
                if published is not None:
                    return published
            except SnapshotReadStopped:
                _release_read(engine, scope, token)
            except Exception as exc:
                _publish(engine, scope, token, ttl, error=str(exc), invalidate=getattr(exc, "invalidate", False),
                         invalidate_credential=getattr(exc, "invalidate_credential", False),
                         error_status=getattr(exc, "status", "unavailable"), retry_after=retry_seconds(exc),
                         retry_at=retry_deadline(exc))
        else:
            with _guard:
                queued = len(_pending) >= 128
                if not queued:
                    _pending.add(scope["key"])
            if queued:
                _publish(engine, scope, token, ttl, error="刷新队列已满，请稍后重试")
            else:
                asyncio.run_coroutine_threadsafe(_work(engine, dict(scope), token, ttl, loader, check), _loop())
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
        scopes = [scope for scope, _ttl, _loader in requests]
        states = _scope_states(db, scopes)
        reserved = False
        views = []
        for scope, _ttl, _loader in requests:
            view, row = _state_view(scope, now, *states)
            if view and (view["status"] == "invalidated" or view.get("error_status") == "credential_invalid"):
                views.append((view, None)); continue
            if row and (row.lease_token and row.lease_until > now or row.next_retry_at > now
                    or row.error_status == "credential_invalid"
                    or not force and row.payload is not None and row.expires_at > now):
                views.append((_view(row, now), None)); continue
            candidate = uuid.uuid4().hex
            values = {**scope, "payload_bytes": 0, "expires_at": 0, "next_retry_at": 0,
                      "lease_token": candidate, "lease_until": now + LEASE_SECONDS, "updated_at": now}
            condition = ((DataSnapshot.lease_until <= now) & (DataSnapshot.next_retry_at <= now)
                         & or_(DataSnapshot.error_status.is_(None), DataSnapshot.error_status != "credential_invalid"))
            if not force:
                condition = condition & or_(DataSnapshot.payload.is_(None), DataSnapshot.expires_at <= now) & (DataSnapshot.next_retry_at <= now)
            statement = insert(DataSnapshot).values(**values).on_conflict_do_update(index_elements=["key"],
                set_={"lease_token": candidate, "lease_until": now + LEASE_SECONDS}, where=condition).returning(DataSnapshot)
            if not reserved:
                db.execute(update(AwsCredential).where(
                    AwsCredential.id.in_({item["credential_id"] for item in scopes})).values(id=AwsCredential.id))
                db.expire_all()
                states = _scope_states(db, scopes)
                reserved = True
            view, row = _state_view(scope, now, *states)
            if view and (view["status"] == "invalidated" or view.get("error_status") == "credential_invalid"):
                views.append((view, None)); continue
            try:
                with db.begin_nested():
                    claimed = db.scalars(statement.execution_options(populate_existing=True)).first()
            except IntegrityError:
                if not _current(db, scope):
                    views.append((_view(None, time.time(), True), None))
                    continue
                raise
            if claimed:
                views.append((_view(claimed, now), candidate))
            else:
                db.expire_all()
                views.append((_view(db.get(DataSnapshot, scope["key"]), now), None))
        db.commit()
    results = await asyncio.gather(*(finish_snapshot(engine, scope, ttl, loader, wait, view, token)
        for (scope, ttl, loader), (view, token) in zip(requests, views)))
    # Another scope or concurrent request may discover invalid credentials
    # after an earlier scope completed. Re-read the whole response set so that
    # a previously captured successful view cannot escape after a credential-
    # wide invalidation committed in SQLite.
    return revalidate_snapshot_views(engine, [
        (scope, view) for (scope, _ttl, _loader), view in zip(requests, results)])


def revalidate_snapshot_views(engine, scoped_views):
    """Return committed values at the response boundary, not stale in-memory views."""
    if not scoped_views:
        return []
    with Session(engine) as db:
        versions, live, auth_rows = _scope_states(db, [scope for scope, _previous in scoped_views])
        db.expunge_all()
    refreshed = []
    now = time.time()
    for scope, previous in scoped_views:
        if versions.get((scope["user_id"], scope["credential_id"])) != scope["version"]:
            refreshed.append(_view(None, now, True)); continue
        if (scope["credential_id"], scope["version"]) in auth_rows:
            auth_row = auth_rows[(scope["credential_id"], scope["version"])]
            status = auth_row.error_status
            refreshed.append({"data": None, "status": status, "stale": False, "refreshing": False,
                              "last_success_at": None, "expires_at": None,
                              "error_status": status, "error": auth_row.error or "凭证认证已失效"})
            continue
        row = live.get(scope["key"])
        # A local pre-loader failure may have produced no row at all. Keep its
        # error-only result, never fall back to an in-memory success payload.
        if row is None and "data" in previous and previous.get("data") is None and previous.get("error"):
            refreshed.append(previous); continue
        refreshed.append(_view(row, now) if row is not None else _view(None, now, True))
    return refreshed


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
