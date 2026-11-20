"""Persist read-scope cooldowns without treating another key as new AWS capacity."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot
from app.proxy_config import sanitize_error_message
from app.services.snapshots import (SnapshotReadStopped, _current, _error_code,
    permission_failure, retry_seconds)

_THROTTLED = {"Throttling", "ThrottlingException", "ThrottledException", "TooManyRequestsException",
              "RequestLimitExceeded", "RequestThrottled", "RequestThrottledException", "SlowDown"}
_store = ContextVar("read_cooldown_store", default=None)


class ReadCooldownActive(RuntimeError):
    def __init__(self, failure):
        super().__init__(failure["reason"])
        self.failure = failure


def current_cooldown_store():
    return _store.get()


@contextmanager
def cooldown_context(store):
    token = _store.set(store)
    try:
        yield store
    finally:
        _store.reset(token)


def _http_status(exc):
    current, seen = exc, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")
        if status is not None:
            try:
                return int(status)
            except (TypeError, ValueError):
                pass
        current = getattr(current, "__cause__", None)
    return None


def failure_details(exc, region, service, operation, resource="", *, now=None):
    """Keep machine-readable retry information even for an optional read."""
    moment = time.time() if now is None else now
    code = _error_code(exc, ClientError) or ("HTTP429" if _http_status(exc) == 429 else type(exc).__name__)
    code = re.sub(r"[^A-Za-z0-9_.-]", "", str(code))[:64] or "unavailable"
    # Explicit authorization/authentication failures stay on the captured key,
    # even if a malformed service response also happens to contain HTTP 429.
    permission = permission_failure(exc)
    shared = not permission and (code in _THROTTLED or _http_status(exc) == 429)
    reason = ("当前读取权限或身份认证未通过" if permission else "AWS 请求频率受限" if shared
              else "网络或 AWS 服务暂不可用" if code in {"TimeoutError", "ConnectTimeoutError", "ReadTimeoutError",
                  "EndpointConnectionError", "ConnectionError", "ServiceUnavailable", "ServiceUnavailableException",
                  "InternalFailure"} else "当前读取未完成")
    retry_at = moment + retry_seconds(exc)
    return {"region": region, "service": service, "operation": operation, "resource": resource,
            "code": code, "reason": reason,
            "retry_at": retry_at, "next_retry_at": datetime.fromtimestamp(retry_at, timezone.utc).isoformat(),
            "shared": shared, "scope": f"{service}:{operation}"}


def _known_account(value):
    return isinstance(value, str) and len(value) == 12 and value.isascii() and value.isdigit()


class ReadCooldownStore:
    """Account throttle markers and credential-specific failures use distinct keys.

    Markers live in DataSnapshot with no payload or refresh lease. Account
    throttles are copied to the currently configured keys for that account so
    deleting or updating the key that observed 429 cannot erase its siblings'
    retry deadline. Permission, identity and temporary network failures retain
    the caller's captured version and never spread to another credential.
    """
    def __init__(self, engine, scope):
        self.engine = engine
        self.scope = dict(scope)

    @staticmethod
    def _range(region, service, operation, resource):
        return json.dumps([region, service, operation, resource], separators=(",", ":"))

    def active(self, region, service, operation, resource=""):
        now = time.time()
        name = self._range(region, service, operation, resource)
        with Session(self.engine) as db:
            if not _current(db, self.scope):
                raise SnapshotReadStopped("凭证、代理或身份版本已变化", status="invalidated")
            rows = db.scalars(select(DataSnapshot).where(DataSnapshot.user_id == self.scope["user_id"],
                DataSnapshot.scope == name, DataSnapshot.next_retry_at > now,
                DataSnapshot.error_status.in_(("throttled", "unavailable")),
                DataSnapshot.kind.in_(("read-cooldown-account", "read-cooldown-key")))).all()
            usable = [row for row in rows if (
                row.kind == "read-cooldown-account" and _known_account(self.scope.get("account_id"))
                and row.account_id == self.scope["account_id"]
            ) or (row.kind == "read-cooldown-key" and row.credential_id == self.scope["credential_id"]
                  and row.version == self.scope["version"])]
            if not usable:
                return None
            row = max(usable, key=lambda value: value.next_retry_at)
            details = json.loads(row.error)
            details["retry_at"] = row.next_retry_at
            details["next_retry_at"] = datetime.fromtimestamp(row.next_retry_at, timezone.utc).isoformat()
            return details

    def record(self, exc, region, service, operation, resource="", details=None):
        if isinstance(exc, ReadCooldownActive):
            return exc.failure
        if isinstance(exc, SnapshotReadStopped) or type(exc).__name__ in {
                "TrafficStopped", "TaskLeaseLost", "TaskCredentialChanged", "TaskPaused"}:
            return None
        now = time.time()
        failure = failure_details(exc, region, service, operation, resource, now=now)
        if details:
            for field in ("step", "name", "retry_condition"):
                if details.get(field) is not None:
                    failure[field] = sanitize_error_message(str(details[field]))
        shared = failure["shared"] and _known_account(self.scope.get("account_id"))
        failure["shared"] = shared
        name = self._range(region, service, operation, resource)
        with Session(self.engine) as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            if not _current(db, self.scope):
                raise SnapshotReadStopped("凭证、代理或身份版本已变化", status="invalidated")
            anchors = db.scalars(select(AwsCredential.id).where(AwsCredential.user_id == self.scope["user_id"],
                AwsCredential.account_id == self.scope["account_id"])).all() if shared else [self.scope["credential_id"]]
            for credential_id in anchors:
                identity = [self.scope["user_id"], "account", self.scope["account_id"]] if shared else [
                    self.scope["user_id"], "credential", credential_id, self.scope["version"]]
                version = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest() if shared else self.scope["version"]
                key = hashlib.sha256(json.dumps([identity, credential_id, name], separators=(",", ":")).encode()).hexdigest()
                values = {"key": key, "user_id": self.scope["user_id"], "credential_id": credential_id,
                    "account_id": self.scope.get("account_id"), "kind": "read-cooldown-account" if shared else "read-cooldown-key",
                    "scope": name, "version": version, "payload": None, "payload_bytes": 0,
                    "last_success": None, "expires_at": 0, "next_retry_at": failure["retry_at"],
                    "error": json.dumps(failure, ensure_ascii=False, separators=(",", ":")),
                    "error_status": "throttled" if shared else "unavailable", "lease_token": None,
                    "lease_until": 0, "updated_at": now}
                statement = insert(DataSnapshot).values(**values)
                db.execute(statement.on_conflict_do_update(index_elements=["key"],
                    set_={"next_retry_at": statement.excluded.next_retry_at, "error": statement.excluded.error,
                          "updated_at": statement.excluded.updated_at},
                    where=DataSnapshot.next_retry_at <= statement.excluded.next_retry_at))
            db.commit()
        return self.active(region, service, operation, resource)

    def require_available(self, region, service, operation, resource=""):
        """Use as an actual-send context check after waiting for a network slot."""
        failure = self.active(region, service, operation, resource)
        if failure is not None:
            raise ReadCooldownActive(failure)
