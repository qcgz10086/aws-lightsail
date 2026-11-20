"""Resumable workflows: persist plans and intent before every AWS mutation."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import time

from botocore.exceptions import ClientError
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.models import (AwsCredential, DataSnapshot, InstanceSetting, OperationLog,
                        OperationTask, OperationTaskStep, TrafficUsage, User)
from app.settings_models import TrafficReadFailure
from app.services import lightsail as ls
from app.services.aws_clients import make_client
from app.services.snapshots import retry_seconds
from app.services.read_cooldowns import ReadCooldownStore, ReadCooldownActive
from app.services.task_queue import (TaskContext, TaskCredentialChanged, TaskKnownFailure,
    TaskLeaseLost, TaskUnknownOutcome, TaskPaused, TrafficStopped, _safe_error, load_task_credentials, register_handler)

WAIT_SECONDS = 120.0
POLL_SECONDS = 1.0
TASK_TAG = "lightsail-manager-task-id"


def _client(context, region, *, read=False, gate=None):
    ak, sk, proxy = load_task_credentials(context.engine, context.task_id, context.token)
    # AWS retries of an unacknowledged mutation can repeat a side effect. The
    # durable executor performs reconciliation instead of transport replay.
    return make_client("lightsail", ak, sk, region, proxy, total_max_attempts=2 if read else 1,
                       before_call=gate or context.check)


def _read(context, region, method, *, absent=False, draining=False, **parameters):
    check = context.check_traffic if context.collecting_traffic and not draining else context.check
    store = None
    operation = "".join(part.title() for part in method.split("_"))
    resource = parameters.get("instanceName") or ""
    if context.collecting_traffic and not draining:
        from app.services.snapshots import scope_for
        with Session(context.engine) as db:
            task = db.get(OperationTask, context.task_id)
            user, credential = db.get(User, task.user_id), db.get(AwsCredential, task.credential_id)
            store = ReadCooldownStore(context.engine, scope_for(user, credential, "traffic-read", region))
    def gate():
        check()
        if store is not None:
            store.require_available(region, "lightsail", operation, resource)
    def failure(exc):
        error = TaskKnownFailure(_safe_error(exc))
        if store is not None:
            error.read_failure = store.record(exc, region, "lightsail", operation, resource)
        return error
    try:
        return getattr(_client(context, region, read=True, gate=gate), method)(**parameters)
    except ClientError as exc:
        if absent and exc.response.get("Error", {}).get("Code") == "NotFoundException":
            return None
        raise failure(exc) from exc
    except (TaskLeaseLost, TaskCredentialChanged, TaskPaused, TrafficStopped):
        raise
    except Exception as exc:
        raise failure(exc) from exc


def _instance(context, region, name):
    response = _read(context, region, "get_instance", absent=True, instanceName=name)
    if response is None:
        return None
    if not response.get("instance"):
        raise TaskKnownFailure("AWS 实例查询缺少 instance，不能视为不存在")
    return response["instance"]


def _pages(context, region, method, member, **parameters):
    items, seen = [], set()
    while True:
        response = _read(context, region, method, **parameters)
        items.extend(response.get(member, []))
        token = response.get("nextPageToken")
        if not token:
            return items
        if token in seen or len(seen) >= 1000:
            raise TaskKnownFailure("AWS 分页 token 重复")
        seen.add(token)
        parameters["pageToken"] = token


def _static_ips(context, region):
    return _pages(context, region, "get_static_ips", "staticIps")


def _ip(context, region, name):
    return next((item for item in _static_ips(context, region) if item.get("name") == name), None)


def _identity(item):
    return {key: item.get(key) for key in ("name", "arn", "createdAt", "attachedTo", "isAttached", "ipAddress")}


def _same_resource(current, recorded):
    return bool(current and recorded and current.get("name") == recorded.get("name") and
                recorded.get("arn") and current.get("arn") == recorded["arn"])


def _confirm_instance(context, region, recorded):
    current = _instance(context, region, recorded["name"])
    if not _same_resource(current, recorded):
        raise TaskUnknownOutcome("实例标识已变化或不存在，保留锁等待核对")
    return current


def _wait_instance(context, region, name, *, present):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        item = _instance(context, region, name)
        if not present and item is None:
            return None
        if present and item and (item.get("state") or {}).get("name") not in {None, "pending"}:
            return item
        if time.monotonic() >= deadline:
            raise TaskUnknownOutcome("实例状态在等待窗口内无法确认")
        time.sleep(POLL_SECONDS)


def _operations(context, region, ids):
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        operations = [_read(context, region, "get_operation", draining=True, operationId=oid).get("operation", {}) for oid in ids]
        if all(op.get("id") == oid and op.get("isTerminal") for op, oid in zip(operations, ids)):
            return operations
        if time.monotonic() >= deadline:
            raise TaskUnknownOutcome("AWS Operation 尚未进入可确认的终态")
        time.sleep(POLL_SECONDS)


def _resolve_mutation(context, step):
    """Only reads AWS; return a known outcome or keep the original intent."""
    intent = step["intent"]
    region, method = intent["region"], intent["method"]
    parameters = intent["parameters"]
    ids = step["result"].get("operation_ids") or ([step["operation_id"]] if step["operation_id"] else [])
    if ids:
        operations = _operations(context, region, ids)
        status = "succeeded" if all(op.get("status") in {"Succeeded", "Completed"} for op in operations) else "failed"
        context.resolve_step(step["sequence"], status, {"operation_ids": ids,
            "statuses": [op.get("status") for op in operations]})
        return status
    # A lost response has no operation ID. Resource facts can prove these
    # outcomes, but running -> running cannot prove a reboot happened.
    success = False
    if method == "create_instances":
        item = _instance(context, region, parameters["instanceNames"][0])
        success = bool(item and any(tag.get("key") == TASK_TAG and tag.get("value") == context.task_id
                                   for tag in item.get("tags", [])))
    elif method in {"start_instance", "stop_instance"}:
        item = _instance(context, region, parameters["instanceName"])
        recorded = intent.get("resource")
        success = bool(_same_resource(item, recorded) and (item.get("state") or {}).get("name") ==
                       ("running" if method == "start_instance" else "stopped"))
    elif method == "delete_instance":
        success = _instance(context, region, parameters["instanceName"]) is None
    elif method in {"attach_static_ip", "allocate_static_ip", "release_static_ip", "detach_static_ip"}:
        item = _ip(context, region, parameters["staticIpName"])
        if method == "attach_static_ip":
            instance = _instance(context, region, parameters["instanceName"])
            success = bool(_same_resource(item, intent.get("resource")) and
                           _same_resource(instance, intent.get("instance")) and item.get("isAttached")
                           and item.get("attachedTo") == parameters["instanceName"])
        elif method == "release_static_ip":
            success = item is None
        elif method == "detach_static_ip":
            # An unacknowledged detach could still execute later. Do not infer
            # its completion merely from a transient unbound state.
            success = False
        else:
            # Allocation intent records absence first and uses a full random
            # task identity; match creation time as well as the exact name.
            created = item.get("createdAt") if item else None
            try:
                created = created if isinstance(created, datetime) else datetime.fromisoformat(str(created))
                written = datetime.fromisoformat(step["created_at"])
                success = bool(item.get("arn") and created.replace(tzinfo=timezone.utc) >=
                               written.replace(tzinfo=timezone.utc, microsecond=0))
            except (ValueError, TypeError, AttributeError):
                success = False
    if success:
        context.resolve_step(step["sequence"], "succeeded", {"fact_confirmed": True})
        return "succeeded"
    raise TaskUnknownOutcome("原写请求结果仍不明确，未重放，资源锁保留")


def _mutate(context, name, region, method, *, retry_safe=False, before_write=None,
            expected_resource=None, expected_instance=None, **parameters):
    previous = context.step(name)
    if previous:
        status = previous["status"]
        if status in {"intent", "unknown"}:
            status = _resolve_mutation(context, previous)
        if status == "succeeded":
            return
        if not (status == "failed" and retry_safe):
            raise TaskKnownFailure("此前该步骤已明确失败，未重放")
    intent = {"mutation": True, "region": region, "method": method, "parameters": parameters}
    if parameters.get("instanceName"):
        intent["instance"] = _identity(_instance(context, region, parameters["instanceName"]) or {})
        intent["resource"] = intent["instance"]
    if parameters.get("staticIpName") and method != "allocate_static_ip":
        intent["resource"] = _identity(_ip(context, region, parameters["staticIpName"]) or {})
    if expected_resource and not _same_resource(intent.get("resource"), expected_resource):
        raise TaskUnknownOutcome("副作用前资源 ARN 已变化，未提交写请求")
    if expected_instance and not _same_resource(intent.get("instance"), expected_instance):
        raise TaskUnknownOutcome("副作用前实例 ARN 已变化，未提交写请求")
    if before_write is not None:
        before_write()
    def write_gate():
        if context.collecting_traffic:
            context.check_traffic()
            if before_write is not None:
                before_write()
        else:
            context.check()
    client = _client(context, region, gate=write_gate)
    sequence = context.begin_step(name, intent)
    try:
        try:
            response = getattr(client, method)(**parameters)
        except (TrafficStopped, TaskPaused):
            context.finish_step(sequence, status="failed", error="后续关机步骤未发送")
            raise
        ids = context.record_operation(sequence, response)
        if not ids:
            raise TaskUnknownOutcome("AWS 写响应缺少 Operation ID")
        operations = _operations(context, region, ids)
        statuses = [operation.get("status") for operation in operations]
        succeeded = all(status in {"Succeeded", "Completed"} for status in statuses)
        context.finish_step(sequence, {"operation_ids": ids, "statuses": statuses},
                            status="succeeded" if succeeded else "failed", operation_id=ids[0])
        if not succeeded:
            raise TaskKnownFailure("AWS Operation 明确失败")
    except (TaskLeaseLost, TaskCredentialChanged, TaskPaused, TrafficStopped):
        raise
    except TaskKnownFailure:
        # Read failures during polling are still unknown writes; a recorded
        # terminal Failed was already marked failed above.
        latest = context.step(name)
        if latest["status"] == "intent":
            context.finish_step(sequence, status="unknown", error="AWS 结果核对失败")
            raise TaskUnknownOutcome("AWS 结果核对失败") from None
        raise
    except Exception as exc:
        context.finish_step(sequence, status="unknown", error=_safe_error(exc))
        raise TaskUnknownOutcome("AWS 写请求未确认，等待事实核对") from None


def _invalidate(context):
    with context.transaction() as (db, task):
        if task.kind == "traffic_sync" and not db.scalar(select(OperationTaskStep.id).where(
                OperationTaskStep.task_id == task.id,
                func.json_extract(OperationTaskStep.intent_json, "$.mutation") == 1,
                OperationTaskStep.status != "failed").limit(1)):
            return
        db.execute(update(DataSnapshot).where(DataSnapshot.user_id == task.user_id,
            DataSnapshot.kind.in_(("instances", "query-instances"))).values(
                expires_at=0, lease_token=None, lease_until=0))


def _create(context):
    p = context.payload
    region, name = p["region"], p["instance_name"]
    def build_plan():
        if _instance(context, region, name):
            raise TaskKnownFailure("同名实例已存在，未接管已有资源")
        az = p.get("availability_zone")
        if not az:
            regions = _read(context, "us-east-1", "get_regions", includeAvailabilityZones=True).get("regions", [])
            selected = next((r for r in regions if r.get("name") == region), {})
            azs = [a["zoneName"] for a in selected.get("availabilityZones", [])
                   if a.get("zoneName") and a.get("state") == "available"]
            if not azs:
                raise TaskKnownFailure("AWS 未提供可用区，未提交建机")
            az = azs[0]
        return {"az": az, "name": name}
    plan = context.plan("create_plan", build_plan)
    parameters = {"instanceNames": [name], "availabilityZone": plan["az"],
        "blueprintId": p["blueprint_id"], "bundleId": p["bundle_id"],
        "tags": [{"key": TASK_TAG, "value": context.task_id}]}
    if p.get("password"):
        parameters["userData"] = ls.build_password_user_data(p["password"], p.get("platform"), p["blueprint_id"])
    _mutate(context, "create_instance", region, "create_instances", **parameters)
    current = _wait_instance(context, region, name, present=True)
    if not any(tag.get("key") == TASK_TAG and tag.get("value") == context.task_id for tag in current.get("tags", [])):
        raise TaskUnknownOutcome("本任务 marker 不能证明实例归属")
    created_identity = context.plan("created_identity", lambda: _identity(current))
    if not _same_resource(current, created_identity):
        raise TaskUnknownOutcome("建机后的实例 ARN 已变化")
    errors = []
    if p.get("open_all_ports"):
        try:
            _mutate(context, "open_firewall", region, "put_instance_public_ports", retry_safe=True,
                expected_resource=created_identity,
                instanceName=name, portInfos=[
                    {"fromPort": 0, "toPort": 65535, "protocol": "all", "cidrs": ["0.0.0.0/0"], "ipv6Cidrs": ["::/0"]},
                    {"fromPort": 22, "toPort": 22, "protocol": "tcp", "cidrListAliases": ["lightsail-connect"]},
                    {"fromPort": 3389, "toPort": 3389, "protocol": "tcp", "cidrListAliases": ["lightsail-connect"]}])
        except TaskKnownFailure:
            raise TaskUnknownOutcome("实例已创建，防火墙设置明确失败；可核对后仅重试剩余步骤") from None
    sip = None
    if p.get("allocate_static_ip", True):
        try:
            sip = _provision_ip(context, region, name, old=None, instance=created_identity)
        except TaskKnownFailure:
            allocation = context.step("allocate_static_ip")
            if allocation and allocation["status"] == "failed" and _ip(context, region, _new_ip_name(context)) is None:
                errors.append("实例已创建，静态 IP 分配明确失败且未产生 IP，可另行提交换 IP 任务")
            elif context.step("ip_rollback_complete"):
                errors.append("实例已创建，静态 IP 绑定明确失败；新 IP 已释放补偿，可另行提交换 IP 任务")
            else:
                raise
    with context.transaction() as (db, task):
        user = db.get(User, task.user_id)
        existing = db.scalar(select(InstanceSetting).where(InstanceSetting.user_id == user.id,
            InstanceSetting.credential_id == task.credential_id, InstanceSetting.region == region,
            InstanceSetting.instance_name == name))
        if existing is None:
            db.add(InstanceSetting(user_id=user.id, credential_id=task.credential_id, region=region,
                instance_name=name, auto_stop_on_limit=bool(user.auto_stop_on_limit_default),
                auto_stop_configured=bool(user.auto_stop_on_limit_default),
                auto_stop_account_id=task.account_id if user.auto_stop_on_limit_default else None))
    _invalidate(context)
    return {"name": name, "region": region, "static_ip_name": sip, "partial": bool(errors), "step_errors": errors}


def _simple(context):
    p = context.payload
    region, name, kind = p["region"], p["resource_name"], p["kind"]
    identity = context.plan("resource_identity", lambda: _identity(_instance(context, region, name) or {}))
    if not identity.get("arn"):
        raise TaskKnownFailure("实例不存在，未提交资源变更")
    _confirm_instance(context, region, identity)
    _mutate(context, kind, region, kind + "_instance", expected_resource=identity, instanceName=name)
    _invalidate(context)
    return {"region": region, "name": name, "operation": kind}


def _new_ip_name(context):
    return "lsm-" + context.task_id.replace("-", "")


def _provision_ip(context, region, name, old, instance):
    new_name = _new_ip_name(context)
    def absent_plan():
        if _ip(context, region, new_name):
            raise TaskKnownFailure("任务预定静态 IP 名已存在，未认领")
        return {"new_name": new_name, "old": old, "instance_name": name}
    context.plan("ip_plan", absent_plan)
    if context.step("ip_rollback_complete"):
        raise TaskKnownFailure("IP 任务已完成补偿，未重放原流程")
    _mutate(context, "allocate_static_ip", region, "allocate_static_ip", staticIpName=new_name)
    def allocated_identity():
        item = _ip(context, region, new_name)
        if not item or not item.get("arn"):
            raise TaskUnknownOutcome("分配后的静态 IP 尚不可见，保留意图等待核对")
        return _identity(item)
    allocated = context.plan("new_ip_identity", allocated_identity)
    attach = context.step("attach_new")
    if attach and attach["status"] == "failed":
        _rollback_ip(context, region, name, old, allocated, instance)
        raise TaskKnownFailure("IP 绑定失败，补偿已完成")
    current = _ip(context, region, new_name)
    if not _same_resource(current, allocated):
        raise TaskUnknownOutcome("新静态 IP 身份未确认")
    release = context.step("release_old")
    if old and not release:
        current_old = _ip(context, region, old["name"])
        if not _same_resource(current_old, old):
            raise TaskUnknownOutcome("旧静态 IP 身份已变化，暂停变更")
        if not context.step("detach_old"):
            if current_old.get("attachedTo") != name:
                raise TaskUnknownOutcome("旧静态 IP 绑定已变化")
        _mutate(context, "detach_old", region, "detach_static_ip", expected_resource=old, staticIpName=old["name"])
    try:
        _mutate(context, "attach_new", region, "attach_static_ip", expected_resource=allocated,
                expected_instance=instance, staticIpName=new_name, instanceName=name)
        attached = _ip(context, region, new_name)
        if not _same_resource(attached, allocated) or not attached.get("isAttached") or attached.get("attachedTo") != name:
            raise TaskUnknownOutcome("新绑定尚未确认，旧 IP 未释放")
    except TaskKnownFailure:
        # A terminal failure allows compensation. Unknown/Started is never
        # compensated while an original attach may still complete later.
        _rollback_ip(context, region, name, old, allocated, instance)
        raise
    if old:
        current_old = _ip(context, region, old["name"])
        if current_old and (not _same_resource(current_old, old) or current_old.get("isAttached")):
            raise TaskUnknownOutcome("旧 IP 不满足安全释放条件")
        if current_old:
            _mutate(context, "release_old", region, "release_static_ip", retry_safe=True,
                    expected_resource=old, staticIpName=old["name"])
        if _ip(context, region, old["name"]) is not None:
            raise TaskUnknownOutcome("旧 IP 释放未确认")
    return new_name


def _rollback_ip(context, region, name, old, allocated, instance):
    try:
        new = _ip(context, region, allocated["name"])
        release_step = context.step("release_new_compensation")
        released = bool(new is None and release_step and release_step["status"] == "succeeded")
        if not released and (not _same_resource(new, allocated) or new.get("isAttached")):
            raise TaskUnknownOutcome("不能安全释放补偿中的新 IP")
        if old:
            current_old = _ip(context, region, old["name"])
            if not _same_resource(current_old, old):
                raise TaskUnknownOutcome("旧 IP 身份变化，不能恢复")
            if current_old.get("attachedTo") != name:
                _mutate(context, "restore_old", region, "attach_static_ip", expected_resource=old,
                        expected_instance=instance, retry_safe=True, staticIpName=old["name"], instanceName=name)
            restored = _ip(context, region, old["name"])
            if not _same_resource(restored, old) or restored.get("attachedTo") != name:
                raise TaskUnknownOutcome("旧绑定恢复尚未确认")
        if not released:
            _mutate(context, "release_new_compensation", region, "release_static_ip", retry_safe=True,
                    expected_resource=allocated, staticIpName=allocated["name"])
        if _ip(context, region, allocated["name"]) is not None:
            raise TaskUnknownOutcome("补偿释放尚未确认")
        context.plan("ip_rollback_complete", lambda: {"restored": True})
    except (TaskKnownFailure, TaskUnknownOutcome):
        raise TaskUnknownOutcome("IP 补偿未完成，保留锁等待核对") from None


def _change_ip(context):
    p = context.payload
    region, name = p["region"], p["resource_name"]
    def build():
        instance = _instance(context, region, name)
        if not instance or not instance.get("arn"):
            raise TaskKnownFailure("实例不存在")
        if (instance.get("state") or {}).get("name") == "pending":
            raise TaskKnownFailure("pending 实例不能更换 IP")
        old = next((item for item in _static_ips(context, region) if item.get("isAttached") and item.get("attachedTo") == name), None)
        return {"instance": _identity(instance), "old": _identity(old) if old else None,
                "old_ip": instance.get("publicIpAddress")}
    plan = context.plan("change_ip_plan", build)
    _confirm_instance(context, region, plan["instance"])
    new = _provision_ip(context, region, name, plan["old"], plan["instance"])
    instance = _confirm_instance(context, region, plan["instance"])
    _invalidate(context)
    return {"instance_name": name, "region": region, "old_ip": plan["old_ip"],
            "new_ip": instance.get("publicIpAddress"), "static_ip_name": new}


def _delete(context):
    p = context.payload
    region, name = p["region"], p["resource_name"]
    def build():
        instance = _instance(context, region, name)
        return {"instance": _identity(instance) if instance else None,
                "related": [_identity(item) for item in _static_ips(context, region)
                            if instance and item.get("name") and item.get("isAttached") and item.get("attachedTo") == name]}
    plan = context.plan("delete_plan", build)
    if plan["instance"] is not None:
        if not context.step("delete_instance"):
            _confirm_instance(context, region, plan["instance"])
        _mutate(context, "delete_instance", region, "delete_instance", expected_resource=plan["instance"],
                instanceName=name, forceDeleteAddOns=True)
        _wait_instance(context, region, name, present=False)
    released = []
    for index, recorded in enumerate(plan["related"]):
        current = _ip(context, region, recorded["name"])
        if current:
            if not _same_resource(current, recorded) or current.get("isAttached") or current.get("attachedTo"):
                raise TaskUnknownOutcome("清理 IP 身份或绑定状态变化，保留锁")
            try:
                _mutate(context, f"cleanup_ip:{index}", region, "release_static_ip", retry_safe=True,
                        expected_resource=recorded, staticIpName=recorded["name"])
            except TaskKnownFailure:
                raise TaskUnknownOutcome("实例已删除，IP 清理明确失败；只重试清理") from None
            if _ip(context, region, recorded["name"]):
                raise TaskUnknownOutcome("IP 清理未确认")
        released.append(recorded["name"])
    with context.transaction() as (db, task):
        credentials = select(AwsCredential.id).where(AwsCredential.user_id == task.user_id,
                                                   AwsCredential.account_id == task.account_id)
        target = InstanceSetting.credential_id.in_(credentials) if task.account_id else InstanceSetting.credential_id == task.credential_id
        db.execute(delete(InstanceSetting).where(InstanceSetting.user_id == task.user_id,
            InstanceSetting.region == region, InstanceSetting.instance_name == name, target))
    _invalidate(context)
    return {"name": name, "region": region, "released_static_ips": released}


def _traffic_failure(exc, region, step, name=None):
    saved = getattr(exc, "read_failure", None)
    if saved:
        denied = saved.get("code") in {"AccessDenied", "AccessDeniedException", "UnauthorizedException"}
        identity = saved.get("code") in {"UnrecognizedClientException", "InvalidClientTokenId", "ExpiredToken",
            "ExpiredTokenException", "AuthFailure", "SignatureDoesNotMatch", "InvalidAccessKeyId", "InvalidSignatureException"}
        return {**saved, "region": region, "step": step,
            "retry_condition": "补齐当前范围只读权限并等待冷却后重新同步" if denied else
                "校验当前凭据身份和区域访问条件并等待冷却后重新同步" if identity else
                "等待所示冷却时间后重新同步" if saved.get("shared") else
                "处理当前读取故障并等待冷却后重新同步", **({"name": name} if name else {})}
    cause = exc.__cause__ or exc
    code = getattr(cause, "response", {}).get("Error", {}).get("Code", "")
    code = code if isinstance(code, str) and code.isascii() and code.isalnum() and len(code) <= 80 else type(cause).__name__
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedException"}:
        reason, retry = "当前区域的只读查询权限不足", "补齐该区域所需只读权限后重新同步"
    elif code in {"UnrecognizedClientException", "InvalidClientTokenId", "ExpiredToken", "ExpiredTokenException",
                  "AuthFailure", "SignatureDoesNotMatch"}:
        reason, retry = "当前区域的身份认证未通过", "检查该区域访问条件并重新校验凭据后同步"
    elif code in {"Throttling", "ThrottlingException", "TooManyRequestsException", "RequestLimitExceeded"}:
        reason, retry = "AWS 请求频率受限", "等待至少 60 秒后重新同步；若 AWS 要求更长等待则按其提示重试"
    elif code in {"TimeoutError", "ConnectTimeoutError", "ReadTimeoutError", "EndpointConnectionError",
                  "ConnectionError", "ServiceUnavailable", "ServiceUnavailableException", "InternalFailure"}:
        reason, retry = "网络超时或 AWS 服务暂不可用", "确认网络或服务恢复后重新同步"
    elif step == "auto_stop":
        reason, retry = "自动关机步骤未完成", "检查自动关机策略及任务步骤后重新同步"
    else:
        reason, retry = "当前步骤未完成", "检查区域访问条件和任务步骤后重新同步"
    return {"region": region, "step": step, "code": code, "reason": reason,
            "retry_condition": retry, **({"name": name} if name else {})}


def _traffic_regions(context):
    from app.query_models import QueryRound
    from app.services.query_rounds import TERMINAL, region_plan, submit_query

    def plan():
        with Session(context.engine) as db:
            task = db.get(OperationTask, context.task_id)
            user, credential = db.get(User, task.user_id), db.get(AwsCredential, task.credential_id)
            user_id, credential_id = user.id, credential.id
            current = region_plan(db, user, credential)
        if current["state"] == "pending":
            round_id = submit_query(context.engine, user_id, credential_id,
                                    subscriber=f"task:{context.task_id}")
            while True:
                context.check_traffic()
                with Session(context.engine) as db:
                    round_state = db.scalar(select(QueryRound.status).where(QueryRound.id == round_id))
                    if round_state in TERMINAL or round_state is None:
                        current = region_plan(db, db.get(User, user_id), db.get(AwsCredential, credential_id))
                        if round_state in {"cancelled", "invalidated"}:
                            raise TrafficStopped("区域发现已停止，未开始流量读取", cancelled=round_state == "cancelled")
                        if round_state is None or round_state == "failed" and not current["regions"]:
                            raise TaskKnownFailure("首次区域发现未完成，请查看全局扫描结果")
                        break
                time.sleep(.25)
        return {"regions": current["regions"], "discovery_state": current["discovery_state"]}

    return context.plan("traffic_regions", plan)


def _traffic_cooldown(context, region):
    # Current failures are checked per actual action in _read, including after
    # transport queueing. A failed metric must not suppress GetInstances or a
    # different metric, and another key cannot bypass an account throttle.
    return None


def _commit_traffic_failure(context, error, exc):
    saved = getattr(exc, "read_failure", None)
    if saved:
        error.update(next_retry_at=saved["next_retry_at"])
        return error
    retry_at = time.time() + retry_seconds(exc)
    error["next_retry_at"] = datetime.fromtimestamp(retry_at, timezone.utc).isoformat()
    with context.transaction() as (db, task):
        values = dict(user_id=task.user_id, credential_id=task.credential_id,
                      credential_version=task.credential_version, region=error["region"],
                      retry_at=retry_at, details=json.dumps(error, ensure_ascii=False))
        stmt = insert(TrafficReadFailure).values(**values)
        db.execute(stmt.on_conflict_do_update(index_elements=["user_id", "credential_id", "credential_version", "region"],
            set_={"retry_at": stmt.excluded.retry_at, "details": stmt.excluded.details},
            where=TrafficReadFailure.retry_at <= stmt.excluded.retry_at))
    return error


def _observed_traffic(db, task, region, name, period):
    filters = [TrafficUsage.user_id == task.user_id, TrafficUsage.account_id == (task.account_id or ""),
               TrafficUsage.region == region, TrafficUsage.instance_name == name, TrafficUsage.year_month == period]
    if not task.account_id:
        filters.append(TrafficUsage.credential_id == task.credential_id)
    return db.scalar(select(TrafficUsage).where(*filters)
                     .order_by(TrafficUsage.last_synced_at.desc(), TrafficUsage.id.desc()).limit(1))


def _can_auto_stop(db, task, region, name, period, end):
    from app.services.traffic import resolve_auto_stop_policy
    observed = _observed_traffic(db, task, region, name, period)
    if (period != datetime.now(timezone.utc).strftime("%Y-%m") or observed is None or observed.last_synced_at is None
            or observed.credential_id != task.credential_id or observed.last_synced_at.replace(tzinfo=timezone.utc) != end):
        return False
    user, cred = db.get(User, task.user_id), db.get(AwsCredential, task.credential_id)
    enabled, limit, conflict, changed = resolve_auto_stop_policy(db, user, cred, region, name)
    return (enabled and not conflict and not changed and limit is not None
            and observed.network_in_bytes + observed.network_out_bytes > limit * 1024**3)


def _traffic_sync(context):
    context.collecting_traffic = True
    context.check_traffic()
    p = context.payload
    start, end = (datetime.fromisoformat(p[key]) for key in ("window_start", "window_end"))
    plan = _traffic_regions(context)
    regions = plan["regions"]
    count, errors, completed_regions = 0, [], 0
    for region in regions:
        context.check_traffic()
        cooldown = _traffic_cooldown(context, region)
        if cooldown:
            errors.append(cooldown)
            continue
        try:
            instances = _pages(context, region, "get_instances", "instances")
        except TaskKnownFailure as exc:
            errors.append(_commit_traffic_failure(context, _traffic_failure(exc, region, "instances"), exc))
            continue
        previous_errors = len(errors)
        for instance in instances:
            name = instance.get("name")
            if not name:
                continue
            step = "NetworkIn"
            try:
                totals = []
                for metric in ("NetworkIn", "NetworkOut"):
                    step = metric
                    response = _read(context, region, "get_instance_metric_data", instanceName=name,
                        metricName=metric, period=86400, startTime=start, endTime=end,
                        unit="Bytes", statistics=["Sum"])
                    from decimal import Decimal
                    totals.append(int(sum((Decimal(str(dp["sum"])) for dp in response.get("metricData", [])
                                           if dp.get("sum") is not None), Decimal(0))))
                with context.transaction() as (db, task):
                    row = {"user_id": task.user_id, "credential_id": task.credential_id, "region": region,
                           "instance_name": name, "year_month": p["year_month"], "account_id": task.account_id or "",
                           "network_in_bytes": totals[0], "network_out_bytes": totals[1],
                           "last_synced_at": end}
                    statement = insert(TrafficUsage).values(**row)
                    accepted = db.execute(statement.on_conflict_do_update(index_elements=["user_id", "credential_id", "region",
                        "instance_name", "year_month", "account_id"], set_={key: row[key] for key in
                        ("network_in_bytes", "network_out_bytes", "last_synced_at")},
                        where=TrafficUsage.last_synced_at <= end)).rowcount
                if not accepted:
                    # Historical windows can be read but cannot supersede a
                    # newer observation or authorize a fresh cloud mutation.
                    continue
                count += 1
                # Reads, policy evaluation and stop remain inside this account
                # task. No child task can deadlock on the parent's account lock.
                with Session(context.engine) as db:
                    task = db.get(OperationTask, context.task_id)
                    if not _can_auto_stop(db, task, region, name, p["year_month"], end):
                        continue
                step = "instance_state"
                current = _instance(context, region, name)
                if _same_resource(current, _identity(instance)) and (current.get("state") or {}).get("name") == "running":
                    step = "auto_stop"
                    def recheck_policy():
                        with context.transaction() as (db, task):
                            if not _can_auto_stop(db, task, region, name, p["year_month"], end):
                                raise TaskKnownFailure("流量观测或自动关机策略已变化，未提交关机")
                    _mutate(context, f"auto_stop:{region}:{name}", region, "stop_instance",
                            expected_resource=_identity(current), before_write=recheck_policy, instanceName=name)
            except TaskKnownFailure as exc:
                error = _traffic_failure(exc, region, step, name)
                if step != "auto_stop":
                    _commit_traffic_failure(context, error, exc)
                errors.append(error)
        if len(errors) == previous_errors:
            completed_regions += 1
    _invalidate(context)
    return {"count": count, "partial": bool(errors), "errors": errors,
            "successful_regions": completed_regions, "failed_regions": len({error["region"] for error in errors}),
            **({"message": ("尚未发现活跃区域，部分区域未完成发现，可执行全局扫描"
                            if plan["discovery_state"] == "partial" else "暂无活跃区域，可执行全局扫描")} if not regions else {}),
            "year_month": p["year_month"], "window_start": p["window_start"], "window_end": p["window_end"]}


def reconcile(context):
    with Session(context.engine) as db:
        names = db.scalars(select(OperationTaskStep.name).where(OperationTaskStep.task_id == context.task_id,
            OperationTaskStep.status.in_(("intent", "unknown"))).order_by(OperationTaskStep.sequence)).all()
    for name in names:
        step = context.step(name)
        if step["intent"].get("mutation"):
            _resolve_mutation(context, step)
        else:
            context.resolve_step(step["sequence"], "failed", {"read_interrupted": True})
    return {"message": "写入结果已核对；继续任务前须显式重试"}


def install_handlers():
    for kind, handler in {"create": _create, "start": _simple, "stop": _simple, "reboot": _simple,
                          "change_ip": _change_ip, "delete": _delete, "traffic_sync": _traffic_sync}.items():
        def guarded(context, run=handler):
            try:
                return run(context)
            except TaskKnownFailure:
                if not context.step("ip_rollback_complete"):
                    with Session(context.engine) as db:
                        steps = db.scalars(select(OperationTaskStep).where(
                            OperationTaskStep.task_id == context.task_id,
                            OperationTaskStep.status == "succeeded")).all()
                        import json
                        if any(json.loads(step.intent_json).get("mutation") for step in steps):
                            raise TaskUnknownOutcome("任务已有已执行步骤，须核对剩余工作") from None
                raise
        register_handler(kind, guarded)
