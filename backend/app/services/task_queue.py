"""Durable task acceptance, locking, step journaling, and worker recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import threading
import time
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, OperationLog, OperationTask, OperationTaskLock, OperationTaskStep, User
from app.proxy_config import sanitize_error_message
from app.security import decrypt_secret, encrypt_secret
from app.services.traffic import get_credential_keys, get_credential_proxy_url

logger = logging.getLogger(__name__)
TASK_KINDS = {"create", "start", "stop", "reboot", "change_ip", "delete", "traffic_sync"}
TERMINAL_STATES = {"succeeded", "failed", "partial", "cancelled", "resolved"}
PURGEABLE_STATES = TERMINAL_STATES - {"resolved"}
WORKER_COUNT = 2
LEASE_SECONDS = 3600
RETENTION_DAYS = 30
MAX_ACTIVE_PER_USER = 100
MAX_ACTIVE_GLOBAL = 1000
MAX_TASK_STEPS = 8192
MAX_TASK_PAYLOAD = 65536


class TaskConflict(ValueError):
    pass


class TaskNotFound(LookupError):
    pass


class TaskLeaseLost(RuntimeError):
    pass


class TaskCredentialChanged(RuntimeError):
    pass


class TaskUnknownOutcome(RuntimeError):
    pass


class TaskKnownFailure(RuntimeError):
    pass


class TaskContext:
    def __init__(self, engine: Engine, task_id: str, token: str, payload: dict):
        self.engine = engine
        self.task_id = task_id
        self.token = token
        self.payload = payload
        self.step_errors = []

    def _credential_is_current(self) -> bool:
        with Session(self.engine) as db:
            row = db.execute(select(User, AwsCredential).join(
                AwsCredential, AwsCredential.user_id == User.id).join(
                OperationTask, OperationTask.user_id == User.id).where(
                OperationTask.id == self.task_id,
                AwsCredential.id == OperationTask.credential_id,
            )).first()
            if not row:
                return False
            _user, credential = row
            expected = credential_version(credential)
            return db.scalar(select(OperationTask.credential_version).where(
                OperationTask.id == self.task_id)) == expected

    def check(self) -> None:
        # Renew only while this worker still owns the task and the captured
        # credential identity is unchanged. Every AWS subcall uses this gate.
        load_task_credentials(self.engine, self.task_id, self.token)

    def step(self, name: str) -> dict | None:
        with Session(self.engine) as db:
            row = db.scalar(select(OperationTaskStep).where(
                OperationTaskStep.task_id == self.task_id, OperationTaskStep.name == name
            ).order_by(OperationTaskStep.sequence.desc()))
            if row is None:
                return None
            return {"sequence": row.sequence, "name": row.name, "status": row.status,
                    "intent": json.loads(row.intent_json),
                    "result": json.loads(row.result_json) if row.result_json else {},
                    "operation_id": row.operation_id, "created_at": row.created_at.isoformat()}

    def plan(self, name: str, build) -> dict:
        previous = self.step(name)
        if previous:
            if previous["status"] == "succeeded":
                return previous["result"]
            if previous["intent"].get("mutation"):
                raise TaskUnknownOutcome("操作计划尚未完整持久化")
            # An interrupted read-only plan has no side effects to replay.
            if previous["status"] in {"intent", "unknown"}:
                self.resolve_step(previous["sequence"], "failed", {"read_interrupted": True})
        value = build()
        sequence = self.begin_step(name, {"mutation": False})
        self.finish_step(sequence, value)
        return value

    @contextmanager
    def transaction(self):
        """Fence local business writes in the same SQLite transaction as ownership."""
        with Session(self.engine) as db:
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                # A caller may already have flushed policy changes. A no-op
                # write obtains SQLite's writer lock without nesting BEGIN.
                connection.exec_driver_sql("UPDATE operation_tasks SET id=id WHERE 0")
            task = db.scalar(select(OperationTask).where(OperationTask.id == self.task_id,
                OperationTask.status == "running", OperationTask.worker_token == self.token,
                OperationTask.lease_until > time.time()))
            if task is None:
                raise TaskLeaseLost("task worker lease no longer owns this task")
            credential = db.get(AwsCredential, task.credential_id)
            if (credential is None or credential.user_id != task.user_id or
                    credential_version(credential) != task.credential_version):
                raise TaskCredentialChanged("凭证或代理版本已变化，任务停止")
            yield db, task
            db.commit()

    def resolve_step(self, sequence: int, status: str, result: dict | None = None) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            owner = connection.execute(select(OperationTask.id).where(
                OperationTask.id == self.task_id, OperationTask.status == "running",
                OperationTask.worker_token == self.token, OperationTask.lease_until > time.time(),
            )).first()
            if not owner:
                raise TaskLeaseLost("task worker lease no longer owns this task")
            values = {"status": status, "completed_at": _utcnow(), "error_message": None}
            if result is not None:
                values["result_json"] = _json(_safe_value(result))
            connection.execute(update(OperationTaskStep).where(
                OperationTaskStep.task_id == self.task_id,
                OperationTaskStep.sequence == sequence,
            ).values(**values))

    def begin_step(self, name: str, intent: dict | None = None) -> int:
        if not self._credential_is_current():
            raise TaskCredentialChanged("凭证或代理版本已变化，任务停止")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = connection.execute(select(OperationTask).where(
                OperationTask.id == self.task_id,
                OperationTask.status == "running",
                OperationTask.worker_token == self.token,
                OperationTask.lease_until > time.time(),
            )).scalar_one_or_none()
            if task is None:
                raise TaskLeaseLost("task worker lease no longer owns this task")
            sequence = connection.execute(select(func.coalesce(
                func.max(OperationTaskStep.sequence), 0)).where(
                    OperationTaskStep.task_id == self.task_id)).scalar_one() + 1
            if sequence > MAX_TASK_STEPS:
                raise TaskUnknownOutcome("任务步骤超过容量限制，请核对已有步骤")
            connection.execute(OperationTaskStep.__table__.insert().values(
                task_id=self.task_id,
                sequence=sequence,
                name=name,
                status="intent",
                intent_json=_json(_safe_value(intent or {})),
            ))
            connection.execute(update(OperationTask).where(
                OperationTask.id == self.task_id,
                OperationTask.worker_token == self.token,
                OperationTask.lease_until > time.time(),
            ).values(current_step=name, lease_until=time.time() + LEASE_SECONDS,
                     updated_at=_utcnow()))
        return sequence

    def finish_step(self, sequence: int, result: dict | None = None, *,
                    status: str = "succeeded", error: str | None = None,
                    operation_id: str | None = None) -> None:
        if status not in {"succeeded", "failed", "unknown", "compensated"}:
            raise ValueError("invalid task step status")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = connection.execute(select(OperationTask.id).where(
                OperationTask.id == self.task_id,
                OperationTask.status == "running",
                OperationTask.worker_token == self.token,
                OperationTask.lease_until > time.time(),
            )).first()
            if not task:
                raise TaskLeaseLost("task worker lease no longer owns this task")
            values = {
                "status": status,
                "error_message": _safe_error(error) if error else None,
                "completed_at": _utcnow(),
            }
            if operation_id is not None:
                values["operation_id"] = operation_id
            if result is not None:
                values["result_json"] = _json(_safe_value(result))
            changed = connection.execute(update(OperationTaskStep).where(
                OperationTaskStep.task_id == self.task_id,
                OperationTaskStep.sequence == sequence,
                OperationTaskStep.status == "intent",
            ).values(**values)).rowcount
            if changed != 1:
                raise TaskLeaseLost("task step is no longer writable")
            connection.execute(update(OperationTask).where(
                OperationTask.id == self.task_id,
                OperationTask.worker_token == self.token,
            ).values(lease_until=time.time() + LEASE_SECONDS, updated_at=_utcnow()))

    def record_operation(self, sequence: int, response: dict) -> list[str]:
        operations = response.get("operations") or []
        operation_ids = [str(item["id"]) for item in operations if item.get("id")]
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = connection.execute(select(OperationTask.id).where(
                OperationTask.id == self.task_id,
                OperationTask.status == "running",
                OperationTask.worker_token == self.token,
                OperationTask.lease_until > time.time(),
            )).first()
            if not task:
                raise TaskLeaseLost("task worker lease no longer owns this task")
            changed = connection.execute(update(OperationTaskStep).where(
                OperationTaskStep.task_id == self.task_id,
                OperationTaskStep.sequence == sequence,
                OperationTaskStep.status == "intent",
            ).values(
                operation_id=operation_ids[0] if operation_ids else None,
                result_json=_json({"operation_ids": operation_ids}),
            )).rowcount
            if changed != 1:
                raise TaskLeaseLost("task step is no longer writable")
        return operation_ids

    def finish(self, status: str, *, result: dict | None = None, error: str | None = None,
               fail_if_no_effect: bool = False) -> None:
        if status not in {"succeeded", "failed", "partial", "reconciliation_required"}:
            raise ValueError("invalid task terminal state")
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            task = connection.execute(select(OperationTask.id).where(
                OperationTask.id == self.task_id,
                OperationTask.status == "running",
                OperationTask.worker_token == self.token,
                OperationTask.lease_until > time.time(),
            )).first()
            if not task:
                raise TaskLeaseLost("task worker lease no longer owns this task")
            unresolved = connection.execute(select(func.count()).select_from(OperationTaskStep).where(
                OperationTaskStep.task_id == self.task_id,
                OperationTaskStep.status.in_(("intent", "unknown")),
            )).scalar_one()
            if fail_if_no_effect:
                steps = connection.execute(select(OperationTaskStep.intent_json, OperationTaskStep.status).where(
                    OperationTaskStep.task_id == self.task_id)).all()
                has_side_effect = any(json.loads(intent).get("mutation") and state != "failed"
                                      for intent, state in steps)
                if not has_side_effect:
                    # Version rotation before any effect is a known failure,
                    # not a permanently unresolved task holding an account lock.
                    status = "failed"
                    unresolved = 0
                    connection.execute(update(OperationTaskStep).where(
                        OperationTaskStep.task_id == self.task_id,
                        OperationTaskStep.status.in_(("intent", "unknown")),
                    ).values(status="failed", error_message="凭证变化前只读步骤中断", completed_at=_utcnow()))
            if unresolved and status != "reconciliation_required":
                status = "reconciliation_required"
            now = _utcnow()
            connection.execute(update(OperationTask).where(
                OperationTask.id == self.task_id,
                OperationTask.worker_token == self.token,
            ).values(
                status=status,
                result_json=_json(_safe_value(result)) if result is not None else None,
                error_message=_safe_error(error) if error else None,
                worker_token=None,
                lease_until=0,
                updated_at=now,
                finished_at=now if status in TERMINAL_STATES else None,
            ))
            user_id = connection.execute(select(OperationTask.user_id).where(
                OperationTask.id == self.task_id)).scalar_one()
            connection.execute(update(DataSnapshot).where(DataSnapshot.user_id == user_id,
                DataSnapshot.kind == "instances").values(expires_at=0, lease_token=None, lease_until=0))
            connection.execute(OperationLog.__table__.insert().values(user_id=user_id,
                action="persistent_task", target=self.task_id, status=status,
                message="任务执行状态已更新，详情通过任务接口查询"))
            if status in TERMINAL_STATES:
                connection.execute(delete(OperationTaskLock).where(
                    OperationTaskLock.task_id == self.task_id))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True)


def _safe_value(value):
    if isinstance(value, dict):
        allowed = {
            "mutation", "region", "method", "parameters", "resource", "instance", "name", "arn",
            "createdAt", "attachedTo", "isAttached", "ipAddress", "az", "old", "new_name", "old_ip",
            "instance_name", "instanceName", "instanceNames", "availabilityZone", "blueprintId", "bundleId",
            "tags", "key", "value", "staticIpName", "forceDeleteAddOns", "portInfos", "fromPort", "toPort",
            "protocol", "cidrs", "ipv6Cidrs", "cidrListAliases", "operation_ids", "statuses", "fact_confirmed",
            "read_interrupted", "restored", "partial", "step_errors", "static_ip_name", "operation", "new_ip",
            "related", "released_static_ips", "count", "errors", "step", "year_month", "window_start", "window_end",
            "message", "manual_resolution", "aws_outcome_confirmed",
        }
        return {key: _safe_value(item) for key, item in value.items()
                if key in allowed}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _safe_error(value) -> str:
    if isinstance(value, Exception):
        if isinstance(value, (TaskKnownFailure, TaskUnknownOutcome, TaskCredentialChanged)):
            return str(value)[:200]
        code = getattr(value, "response", {}).get("Error", {}).get("Code", "")
        return f"AWS/任务步骤异常 ({code if code.isalnum() else type(value).__name__})"
    return sanitize_error_message(str(value))[:1000]


def credential_version(credential: AwsCredential) -> str:
    values = [credential.id, credential.account_id, credential.access_key_id_enc,
              credential.secret_access_key_enc, credential.proxy_url_enc]
    return hashlib.sha256(_json(values).encode()).hexdigest()


def account_scope(credential: AwsCredential) -> tuple[str, str]:
    account = credential.account_id
    if account and len(account) == 12 and account.isdigit():
        return f"account:{account}", account
    return f"credential:{credential.id}", ""


def _fingerprint(payload: dict) -> str:
    from app.config import get_settings
    return hmac.new(get_settings().secret_key.encode(), _json(payload).encode(), hashlib.sha256).hexdigest()


def _lock_key(user_id: int, scope: str, region: str | None, resource: str | None, kind: str) -> str:
    resource_scope = "*" if kind == "account" else (resource or "*")
    raw = f"{user_id}:{scope}:{region or '*'}:{resource_scope}"
    return hashlib.sha256(raw.encode()).hexdigest()


def task_view(db: Session, task: OperationTask) -> dict:
    steps = db.scalars(select(OperationTaskStep).where(
        OperationTaskStep.task_id == task.id).order_by(OperationTaskStep.sequence)).all()
    return {
        "task_id": task.id,
        "kind": task.kind,
        "status": task.status,
        "credential_id": task.credential_id,
        "account_id": task.account_id,
        "region": task.region,
        "resource_name": task.resource_name,
        "current_step": task.current_step,
        "steps": [{"sequence": step.sequence, "name": step.name, "status": step.status,
                   "intent": json.loads(step.intent_json),
                   "result": json.loads(step.result_json) if step.result_json else None,
                   "error": step.error_message, "operation_id": step.operation_id}
                  for step in steps],
        "result": json.loads(task.result_json) if task.result_json else None,
        "error": task.error_message,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "can_cancel": task.status == "queued" and task.attempt == 0 and task.started_at is None,
        "can_resume": task.status == "reconciliation_required" and
            not any(step.status in {"intent", "unknown"} for step in steps),
    }


class DurableTaskQueue:
    def __init__(self, engine: Engine):
        self.engine = engine
        self._pool = ThreadPoolExecutor(max_workers=WORKER_COUNT, thread_name_prefix="operation-task")
        self._guard = threading.Lock()
        self._futures = set()
        self.closed = False

    def accept(self, db: Session, user: User, credential: AwsCredential, kind: str,
               payload: dict, idempotency_key: str, *, region: str | None,
               resource_name: str | None, account_lock: bool = False,
               wait: bool = True) -> dict:
        if kind not in TASK_KINDS:
            raise ValueError("unsupported task kind")
        key = idempotency_key.strip()
        if not key or len(key) > 128:
            raise ValueError("Idempotency-Key must contain 1-128 characters")
        payload_fp = _fingerprint({
            "kind": kind,
            "credential_id": credential.id,
            "region": region,
            "resource_name": resource_name,
            "payload": payload,
        })
        if len(_json(payload).encode()) > MAX_TASK_PAYLOAD:
            raise ValueError("任务请求超过 64 KiB 限制")
        scope, account = account_scope(credential)
        task_id = str(uuid.uuid4())
        now = _utcnow()
        effective_payload = dict(payload)
        if kind == "traffic_sync" and not effective_payload.get("window_end"):
            effective_payload.update(
                window_end=now.isoformat(),
                window_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
                year_month=now.strftime("%Y-%m"))
        task = OperationTask(
            id=task_id, user_id=user.id, credential_id=credential.id,
            account_scope=scope, account_id=account or None,
            credential_version=credential_version(credential), kind=kind,
            region=region, resource_name=resource_name,
            idempotency_key=key, request_fingerprint=payload_fp,
            encrypted_payload=encrypt_secret(_json(effective_payload)), status="queued",
            created_at=now, updated_at=now,
        )
        lock_rows = []
        if account_lock:
            lock_rows.append(OperationTaskLock(
                lock_key=_lock_key(user.id, scope, None, None, "account"),
                task_id=task_id, user_id=user.id, account_scope=scope,
                region=None, resource_name=None, lock_kind="account",
            ))
        else:
            lock_rows.append(OperationTaskLock(
                lock_key=_lock_key(user.id, scope, region, resource_name, "resource"),
                task_id=task_id, user_id=user.id, account_scope=scope,
                region=region, resource_name=resource_name, lock_kind="resource",
            ))
        try:
            # Serialize overlap checks with lock insertion so account-wide sync
            # cannot race a resource mutation under SQLite's single-writer model.
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.exec_driver_sql("UPDATE operation_tasks SET id=id WHERE 0")
            existing = db.scalar(select(OperationTask).where(
                OperationTask.user_id == user.id, OperationTask.idempotency_key == key))
            if existing:
                if not hmac.compare_digest(existing.request_fingerprint, payload_fp):
                    raise TaskConflict("幂等键已用于不同请求")
                view = task_view(db, existing)
                db.commit()
                return view
            current_credential = db.get(AwsCredential, credential.id, populate_existing=True)
            if (db.get(User, user.id, populate_existing=True) is None or
                    not current_credential or current_credential.user_id != user.id or
                    credential_version(current_credential) != task.credential_version):
                raise TaskConflict("凭证版本已变化，请重新提交新意图")
            active_count = db.scalar(select(func.count()).select_from(OperationTask).where(
                OperationTask.user_id == user.id, OperationTask.status.not_in(tuple(TERMINAL_STATES))))
            if active_count >= MAX_ACTIVE_PER_USER:
                raise TaskConflict("未完成任务已达容量限制，请先核对或取消")
            if db.scalar(select(func.count()).select_from(OperationTask).where(
                    OperationTask.status.not_in(tuple(TERMINAL_STATES)))) >= MAX_ACTIVE_GLOBAL:
                raise TaskConflict("系统未完成任务已达容量限制")
            changed_scope_tasks = db.scalar(select(func.count()).select_from(OperationTask).where(
                OperationTask.credential_id == credential.id,
                OperationTask.account_scope != scope,
                OperationTask.status.not_in(tuple(TERMINAL_STATES))))
            if changed_scope_tasks:
                raise TaskConflict("该凭证原账号仍有未解决任务")
            active_locks = db.scalars(select(OperationTaskLock).where(
                OperationTaskLock.user_id == user.id,
                OperationTaskLock.account_scope == scope,
            )).all()
            for existing_lock in active_locks:
                overlaps = account_lock or existing_lock.lock_kind == "account" or (
                    existing_lock.region == region and existing_lock.resource_name == resource_name
                )
                if overlaps:
                    raise TaskConflict("该账号或实例存在未完成任务")
            db.add(task)
            db.flush()
            db.add_all(lock_rows)
            db.commit()
        except Exception as exc:
            db.rollback()
            if isinstance(exc, TaskConflict):
                raise
            duplicate = db.scalar(select(OperationTask).where(
                OperationTask.user_id == user.id, OperationTask.idempotency_key == key))
            if duplicate:
                if not hmac.compare_digest(duplicate.request_fingerprint, payload_fp):
                    raise TaskConflict("幂等键已用于不同请求") from exc
                return task_view(db, duplicate)
            holder = db.scalar(select(OperationTaskLock).where(
                OperationTaskLock.user_id == user.id,
                OperationTaskLock.account_scope == scope))
            if holder:
                raise TaskConflict("该账号或实例存在未完成任务") from exc
            raise
        self.schedule(task_id, wait=wait)
        db.refresh(task)
        return task_view(db, task)

    def schedule(self, task_id: str, *, wait: bool = False) -> None:
        with self._guard:
            if any(getattr(future, "task_id", None) == task_id for future in self._futures):
                return
            future = self._pool.submit(self._execute, task_id)
            future.task_id = task_id
            self._futures.add(future)
        future.add_done_callback(self._discard_future)
        if wait:
            future.result()

    def _discard_future(self, future) -> None:
        with self._guard:
            self._futures.discard(future)

    def _execute(self, task_id: str) -> None:
        token = uuid.uuid4().hex
        with Session(self.engine) as db:
            claimed = db.execute(update(OperationTask).where(
                OperationTask.id == task_id,
                OperationTask.status == "queued",
            ).values(status="running", worker_token=token,
                     lease_until=time.time() + LEASE_SECONDS,
                     attempt=OperationTask.attempt + 1,
                     started_at=func.coalesce(OperationTask.started_at, _utcnow()),
                     updated_at=_utcnow())).rowcount
            db.commit()
            if claimed != 1:
                return
            task = db.get(OperationTask, task_id)
            kind = task.kind
            payload_enc = task.encrypted_payload
            db.commit()
            try:
                payload = json.loads(decrypt_secret(payload_enc))
                handler = _HANDLERS[kind]
                context = TaskContext(self.engine, task_id, token, payload)
                context.check()
                result = handler(context)
                context.finish(
                    "partial" if isinstance(result, dict) and result.get("partial") else "succeeded",
                    result=result if isinstance(result, dict) else {},
                )
            except TaskCredentialChanged as exc:
                try:
                    TaskContext(self.engine, task_id, token, {}).finish("reconciliation_required", error=str(exc),
                                                                      fail_if_no_effect=True)
                except TaskLeaseLost:
                    pass
            except TaskLeaseLost:
                logger.info("task %s worker lease was superseded", task_id)
            except Exception as exc:
                logger.warning("operation task stopped id=%s kind=%s error=%s", task_id, kind, _safe_error(exc))
                try:
                    with Session(self.engine) as check:
                        pending_step = check.scalar(select(func.count()).select_from(OperationTaskStep).where(
                            OperationTaskStep.task_id == task_id,
                            OperationTaskStep.status.in_(("intent", "unknown")),
                        ))
                    TaskContext(self.engine, task_id, token, {}).finish(
                        "reconciliation_required" if pending_step or isinstance(exc, TaskUnknownOutcome)
                        else "failed", error=_safe_error(exc))
                except TaskLeaseLost:
                    pass

    def recover(self, *, expired_only: bool = False) -> None:
        with Session(self.engine) as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            query = select(OperationTask).where(OperationTask.status == "running")
            if expired_only:
                query = query.where(OperationTask.lease_until <= time.time())
            running = db.scalars(query).all()
            for task in running:
                has_steps = db.scalar(select(func.count()).select_from(OperationTaskStep).where(
                    OperationTaskStep.task_id == task.id))
                task.status = "reconciliation_required" if has_steps else "queued"
                task.worker_token = None
                task.lease_until = 0
                task.updated_at = _utcnow()
            db.commit()
            queued_ids = db.scalars(select(OperationTask.id).where(
                OperationTask.status == "queued").order_by(OperationTask.created_at)).all()
        for task_id in queued_ids:
            self.schedule(task_id)
        self.purge_terminal()

    def shutdown(self, *, wait: bool = False) -> None:
        self.closed = True
        self._pool.shutdown(wait=wait, cancel_futures=False)

    def purge_terminal(self) -> int:
        cutoff = _utcnow() - timedelta(days=RETENTION_DAYS)
        with self.engine.begin() as connection:
            return connection.execute(delete(OperationTask).where(
                OperationTask.status.in_(tuple(PURGEABLE_STATES)),
                OperationTask.finished_at < cutoff,
            )).rowcount

    def get(self, db: Session, user_id: int, task_id: str) -> dict:
        task = db.scalar(select(OperationTask).where(
            OperationTask.id == task_id, OperationTask.user_id == user_id))
        if not task:
            raise TaskNotFound(task_id)
        return task_view(db, task)

    def list(self, db: Session, user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        tasks = db.scalars(select(OperationTask).where(
            OperationTask.user_id == user_id).order_by(
                OperationTask.status.not_in(tuple(TERMINAL_STATES)).desc(),
                OperationTask.created_at.desc(), OperationTask.id).offset(offset).limit(limit)).all()
        return [task_view(db, task) for task in tasks]

    def cancel(self, db: Session, user_id: int, task_id: str) -> dict:
        now = _utcnow()
        changed = db.execute(update(OperationTask).where(
            OperationTask.id == task_id,
            OperationTask.user_id == user_id,
            OperationTask.status == "queued",
            OperationTask.attempt == 0,
            OperationTask.started_at.is_(None),
        ).values(status="cancelled", finished_at=now, updated_at=now)).rowcount
        if changed != 1:
            db.rollback()
            task = db.scalar(select(OperationTask).where(
                OperationTask.id == task_id, OperationTask.user_id == user_id))
            if not task:
                raise TaskNotFound(task_id)
            raise TaskConflict("任务已开始或已完成，不能取消")
        db.execute(delete(OperationTaskLock).where(OperationTaskLock.task_id == task_id))
        db.commit()
        return self.get(db, user_id, task_id)

    def reconcile(self, db: Session, user_id: int, task_id: str) -> dict:
        self.get(db, user_id, task_id)
        token = uuid.uuid4().hex
        claimed = db.execute(update(OperationTask).where(OperationTask.id == task_id,
            OperationTask.user_id == user_id, OperationTask.status == "reconciliation_required",
            OperationTask.worker_token.is_(None)).values(status="running", worker_token=token,
                lease_until=time.time() + LEASE_SECONDS, updated_at=_utcnow())).rowcount
        db.commit()
        if not claimed:
            raise TaskConflict("任务状态已变化，不能核对")
        from app.services.task_handlers import reconcile
        context = TaskContext(self.engine, task_id, token, {})
        try:
            context.check()
            result = reconcile(context)
            context.finish("reconciliation_required", result=result)
        except TaskLeaseLost:
            pass
        except Exception as exc:
            context.finish("reconciliation_required", error=_safe_error(exc))
        db.expire_all()
        return self.get(db, user_id, task_id)

    def retry(self, db: Session, user_id: int, task_id: str) -> dict:
        task = db.scalar(select(OperationTask).where(OperationTask.id == task_id,
                                                    OperationTask.user_id == user_id))
        if task is None:
            raise TaskNotFound(task_id)
        if task.status != "reconciliation_required":
            raise TaskConflict("仅已核对的未解决任务可以继续")
        credential = db.get(AwsCredential, task.credential_id)
        if not credential or credential_version(credential) != task.credential_version:
            raise TaskConflict("原凭证版本已变化，禁止借新凭证继续旧任务")
        unresolved = db.scalar(select(func.count()).select_from(OperationTaskStep).where(
            OperationTaskStep.task_id == task_id, OperationTaskStep.status.in_(("intent", "unknown"))))
        if unresolved:
            raise TaskConflict("未知步骤须先执行只读核对，禁止重放")
        changed = db.execute(update(OperationTask).where(OperationTask.id == task_id,
            OperationTask.status == "reconciliation_required", OperationTask.worker_token.is_(None)).values(
                status="queued", error_message=None, updated_at=_utcnow())).rowcount
        db.commit()
        if not changed:
            raise TaskConflict("任务状态已变化")
        self.schedule(task_id)
        db.expire_all()
        return self.get(db, user_id, task_id)

    def resolve(self, db: Session, user_id: int, task_id: str, acknowledgement: str) -> dict:
        """Explicit owner decision; record uncertainty, never infer an AWS outcome."""
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        task = db.scalar(select(OperationTask).where(OperationTask.id == task_id, OperationTask.user_id == user_id))
        if task is None:
            raise TaskNotFound(task_id)
        if acknowledgement != task_id or task.status != "reconciliation_required" or task.worker_token:
            raise TaskConflict("仅待核对且无执行者的任务可显式解除；必须确认完整任务 ID")
        task.status = "resolved"
        task.result_json = _json({"manual_resolution": True, "aws_outcome_confirmed": False,
                                 "message": "所有者已确认自行处理未决结果并显式解除锁；未重放 AWS 请求"})
        task.error_message = "AWS 结果未由系统确认；所有者显式解除资源锁"
        task.finished_at = task.updated_at = _utcnow()
        # Preserve unknown step evidence; manual resolution is distinguishable
        # from an automatically confirmed partial result.
        db.execute(delete(OperationTaskLock).where(OperationTaskLock.task_id == task_id))
        db.add(OperationLog(user_id=user_id, action="resolve_task", target=task_id,
                            status="resolved", message=task.error_message))
        db.commit()
        return self.get(db, user_id, task_id)


def load_task_credentials(engine: Engine, task_id: str, token: str) -> tuple[str, str, str | None]:
    with Session(engine) as db:
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        row = db.execute(select(OperationTask, AwsCredential).join(
            AwsCredential, AwsCredential.id == OperationTask.credential_id).where(
            OperationTask.id == task_id, OperationTask.worker_token == token,
            OperationTask.status == "running", OperationTask.lease_until > time.time(),
            AwsCredential.user_id == OperationTask.user_id)).first()
        if not row:
            raise TaskLeaseLost("task worker lease no longer owns this task")
        task, credential = row
        if task.credential_version != credential_version(credential):
            raise TaskCredentialChanged("凭证或代理版本已变化，任务停止")
        db.execute(update(OperationTask).where(OperationTask.id == task_id,
            OperationTask.worker_token == token, OperationTask.status == "running").values(
                lease_until=time.time() + LEASE_SECONDS))
        db.commit()
        try:
            return (*get_credential_keys(credential), get_credential_proxy_url(credential))
        except Exception as exc:
            raise TaskCredentialChanged("凭证或代理不可用，任务停止") from exc


_HANDLERS: dict[str, object] = {}
_QUEUES: dict[int, DurableTaskQueue] = {}
_QUEUE_GUARD = threading.Lock()


def queue_for(engine: Engine) -> DurableTaskQueue:
    key = id(engine)
    with _QUEUE_GUARD:
        queue = _QUEUES.get(key)
        if queue is None or queue.closed:
            from app.services.task_handlers import install_handlers
            install_handlers()
            queue = DurableTaskQueue(engine)
            _QUEUES[key] = queue
        return queue


def register_handler(kind: str, handler) -> None:
    _HANDLERS[kind] = handler


def has_unresolved_tasks(db: Session, *, user_id: int | None = None,
                         credential_id: int | None = None) -> bool:
    query = select(func.count()).select_from(OperationTask).where(
        OperationTask.status.not_in(tuple(TERMINAL_STATES)))
    if user_id is not None:
        query = query.where(OperationTask.user_id == user_id)
    if credential_id is not None:
        query = query.where(OperationTask.credential_id == credential_id)
    return bool(db.scalar(query))
