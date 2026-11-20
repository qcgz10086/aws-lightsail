from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import InstanceSetting, OperationLog, TrafficUsage, User
from app.proxy_config import ProxyConfigError
from app.schemas import (
    ChangeIpResponse,
    CreateInstanceResponse,
    InstanceCreate,
    InstanceOut,
    InstanceSettingsUpdate,
    InstanceTraffic,
    OperationResult,
)
from app.services import lightsail as ls
from app.services.task_submission import submit, legacy_result
from app.services.aws_clients import select_scan_credentials
from app.services.credential_reads import CredentialReadGuard
from app.services.traffic import (
    NoCredentialsError,
    effective_auto_stop,
    effective_limit_gb,
    resolve_auto_stop_policy,
    get_credential_proxy_url,
    get_setting,
    get_user_aws_keys_and_cred,
    reconcile_auto_stop_conflicts,
    resolve_auto_stop_policies,
    resolve_auto_stop_conflict,
    auto_stop_conflict_details,
    traffic_for_instance,
    current_year_month,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/instances", tags=["instances"])

def _keys(user: User, credential_id: int | None = None):
    """返回 (ak, sk, cred, proxy_url)；代理解密失败同样按 400 处理。"""
    try:
        ak, sk, cred = get_user_aws_keys_and_cred(user, credential_id)
        return ak, sk, cred, get_credential_proxy_url(cred)
    except NoCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _log(
    db: Session,
    user_id: int,
    action: str,
    region: str | None,
    target: str | None,
    status: str,
    message: str | None = None,
) -> None:
    if status == "success" and action in {"create_instance", "start", "stop", "reboot", "change_ip", "delete_instance"}:
        _expire_instance_snapshots(db, user_id)
    db.add(
        OperationLog(
            user_id=user_id,
            action=action,
            region=region,
            target=target,
            status=status,
            message=message,
        )
    )


def _expire_instance_snapshots(db, user_id):
    from sqlalchemy import update
    from app.models import DataSnapshot
    # Existing write routes keep their response contract. The next query must
    # refresh state rather than presenting a minute-old pre-operation snapshot.
    db.execute(update(DataSnapshot).where(DataSnapshot.user_id == user_id,
        DataSnapshot.kind.in_(("instances", "query-instances"))).values(expires_at=0, lease_token=None, lease_until=0))


def _to_out(
    db: Session,
    user: User,
    raw: dict,
    credential_id: int,
    account_label: str | None,
    policy: tuple[bool, float | None, bool, bool] | None = None,
    setting: InstanceSetting | None = None,
    usage: TrafficUsage | None = None,
    preloaded: bool = False,
) -> InstanceOut:
    region = raw["region"]
    name = raw["name"]
    if not preloaded:
        setting = get_setting(db, user.id, region, name, credential_id)
    credential = next((item for item in user.credentials if item.id == credential_id), None)
    account_id = credential.account_id if credential else None
    traffic = traffic_for_instance(
        db, user, region, name, credential_id, account_id, setting, usage, preloaded
    ) or {}
    auto_stop_enabled, policy_limit, conflict, identity_changed = (
        policy or resolve_auto_stop_policy(db, user, credential, region, name)
        if credential else (effective_auto_stop(user, setting), effective_limit_gb(user, setting), False, False)
    )
    limit = None if identity_changed else policy_limit
    traffic["limit_gb"] = limit
    traffic["over_limit"] = limit is not None and traffic.get("total_gb", 0) > limit
    return InstanceOut(
        name=name,
        region=region,
        availability_zone=raw.get("availability_zone"),
        state=raw.get("state") or "unknown",
        public_ip=raw.get("public_ip"),
        private_ip=raw.get("private_ip"),
        blueprint_id=raw.get("blueprint_id"),
        blueprint_name=raw.get("blueprint_name"),
        bundle_id=raw.get("bundle_id"),
        is_static_ip=bool(raw.get("is_static_ip")),
        static_ip_name=raw.get("static_ip_name"),
        created_at=raw.get("created_at"),
        traffic=InstanceTraffic(**traffic) if traffic else None,
        monthly_limit_gb=setting.monthly_limit_gb if setting else None,
        auto_stop_on_limit=auto_stop_enabled,
        configured_auto_stop_on_limit=(bool(setting.auto_stop_on_limit)
                                       if setting and setting.auto_stop_configured else auto_stop_enabled),
        auto_stop_conflict=conflict,
        auto_stop_identity_changed=identity_changed,
        note=setting.note if setting else None,
        credential_id=credential_id,
        account_label=account_label,
    )


def outputs_for_raw(db: Session, user: User, raw_items: list) -> list[InstanceOut]:
    # SQLAlchemy Session 非并发安全：统一在主协程组装输出
    credential_by_id = {credential.id: credential for credential in user.credentials}
    settings = db.query(InstanceSetting).filter(InstanceSetting.user_id == user.id).all()
    policies = resolve_auto_stop_policies(db, user, [
        (credential_by_id[cred_id], raw["region"], raw["name"])
        for cred_id, _label, raw in raw_items if cred_id in credential_by_id
    ], list(credential_by_id.values()), settings)
    requested_keys = {(cid, raw["region"], raw["name"]) for cid, _label, raw in raw_items}
    credentials = credential_by_id
    credential_ids = set(credentials)
    setting_map = {
        (row.credential_id, row.region, row.instance_name): row
        for row in settings if (row.credential_id, row.region, row.instance_name) in requested_keys
    }
    usage_rows = db.query(TrafficUsage).filter(
        TrafficUsage.user_id == user.id,
        TrafficUsage.year_month == current_year_month(),
        TrafficUsage.credential_id.in_(credential_ids),
    ).all() if credential_ids else []
    usage_map = {
        (row.credential_id, row.region, row.instance_name, row.account_id): row
        for row in usage_rows
    }
    instances = [
        _to_out(db, user, raw, cred_id, label,
                policies.get((cred_id, raw["region"], raw["name"])),
                setting_map.get((cred_id, raw["region"], raw["name"])),
                usage_map.get((cred_id, raw["region"], raw["name"],
                               credentials[cred_id].account_id or "") if cred_id in credentials else None),
                True)
        for cred_id, label, raw in raw_items
    ]
    instances.sort(key=lambda x: (x.region, x.name, x.credential_id or 0))
    return instances


@router.get("", response_model=list[InstanceOut])
async def list_instances(
    credential_id: int | None = Query(None, description="仅扫描指定凭证；空=全部凭证"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InstanceOut]:
    if not user.credentials:
        raise HTTPException(status_code=400, detail="请先绑定 AWS 凭证")

    creds = list(user.credentials)
    if credential_id is not None:
        creds = [c for c in creds if c.id == credential_id]
        if not creds:
            raise HTTPException(status_code=404, detail="凭证不存在")

    if credential_id is None:
        creds = select_scan_credentials(creds)

    from app.services.instance_cache import instance_scopes
    scope_rows = await instance_scopes(db.get_bind(), user, creds, wait=True)
    labels = {credential.id: credential.account_label for credential in creds}
    cached_items = [(scope["credential_id"], labels[scope["credential_id"]], raw)
                    for scope in scope_rows for raw in scope["data"] or []]
    errors = [scope["error"] for scope in scope_rows if scope.get("error")]
    instances = outputs_for_raw(db, user, cached_items)
    if not instances and errors:
        raise HTTPException(status_code=400, detail="获取实例失败: " + "; ".join(errors[:8]))
    return instances


@router.get("/{region}/{name}", response_model=InstanceOut)
async def get_instance(
    region: str,
    name: str,
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InstanceOut:
    ak, sk, cred, proxy_url = _keys(user, credential_id)
    guard = CredentialReadGuard.capture(db.get_bind(), user, cred)
    try:
        with guard.context():
            raw = await asyncio.to_thread(ls.get_instance, ak, sk, region, name, proxy_url=proxy_url)
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=exc.message)
    return _to_out(db, user, raw, cred.id, cred.account_label)


@router.post("", response_model=CreateInstanceResponse)
async def create_instance(body: InstanceCreate,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    state = submit(db, user, "create", body.credential_id,
                   payload=body.model_dump(mode="json"), idempotency_key=idempotency_key)
    return await legacy_result(db, user, state)


@router.post("/{region}/{name}/start", response_model=OperationResult)
async def start_instance(region: str, name: str, credential_id: int | None = Query(None),
                         idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                         user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _simple_op(user, db, region, name, "start", credential_id, idempotency_key)


@router.post("/{region}/{name}/stop", response_model=OperationResult)
async def stop_instance(region: str, name: str, credential_id: int | None = Query(None),
                        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                        user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _simple_op(user, db, region, name, "stop", credential_id, idempotency_key)


@router.post("/{region}/{name}/reboot", response_model=OperationResult)
async def reboot_instance(region: str, name: str, credential_id: int | None = Query(None),
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _simple_op(user, db, region, name, "reboot", credential_id, idempotency_key)


async def _simple_op(user, db, region, name, action, credential_id, idempotency_key):
    state = submit(db, user, action, credential_id, region=region, resource_name=name,
                   idempotency_key=idempotency_key)
    return await legacy_result(db, user, state)


@router.post("/{region}/{name}/change-ip", response_model=ChangeIpResponse)
async def change_ip(region: str, name: str, credential_id: int | None = Query(None),
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _simple_op(user, db, region, name, "change_ip", credential_id, idempotency_key)


@router.delete("/{region}/{name}", response_model=OperationResult)
async def delete_instance(region: str, name: str, credential_id: int | None = Query(None),
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                          user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return await _simple_op(user, db, region, name, "delete", credential_id, idempotency_key)


@router.get("/{region}/{name}/policy-conflict")
def policy_conflict(
    region: str,
    name: str,
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _ak, _sk, cred, _proxy = _keys(user, credential_id)
    return auto_stop_conflict_details(db, user, cred, region, name)


@router.patch("/{region}/{name}/settings", response_model=InstanceOut)
async def update_settings(
    region: str,
    name: str,
    body: InstanceSettingsUpdate,
    credential_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InstanceOut:
    ak, sk, cred, proxy_url = _keys(user, credential_id)
    guard = CredentialReadGuard.capture(db.get_bind(), user, cred)
    # Capture an existing conflict before accepting changes to a single row.
    reconcile_auto_stop_conflicts(db, user)
    setting = get_setting(db, user.id, region, name, cred.id)
    if setting is None:
        setting = InstanceSetting(
            user_id=user.id,
            credential_id=cred.id,
            region=region,
            instance_name=name,
            auto_stop_on_limit=bool(user.auto_stop_on_limit_default),
            auto_stop_configured=bool(user.auto_stop_on_limit_default),
            auto_stop_account_id=cred.account_id if user.auto_stop_on_limit_default else None,
        )
        db.add(setting)
    if "monthly_limit_gb" in body.model_fields_set:
        setting.monthly_limit_gb = body.monthly_limit_gb
    if "auto_stop_on_limit" in body.model_fields_set and body.auto_stop_on_limit is not None:
        setting.auto_stop_on_limit = bool(body.auto_stop_on_limit)
        setting.auto_stop_configured = True
        if cred.account_id:
            setting.auto_stop_account_id = cred.account_id
    if "note" in body.model_fields_set:
        setting.note = body.note
    db.add(setting)
    reconcile_auto_stop_conflicts(db, user)
    if body.resolve_conflict_credential_ids is not None:
        try:
            resolve_auto_stop_conflict(db, user, cred, region, name,
                                       body.resolve_conflict_credential_ids, body.resolve_conflict_token)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()

    try:
        with guard.context():
            raw = await asyncio.to_thread(ls.get_instance, ak, sk, region, name, proxy_url=proxy_url)
        return _to_out(db, user, raw, cred.id, cred.account_label)
    except ls.LightsailError:
        traffic = traffic_for_instance(db, user, region, name, cred.id, cred.account_id)
        auto_stop_enabled, _policy_limit, conflict, identity_changed = resolve_auto_stop_policy(
            db, user, cred, region, name
        )
        if traffic:
            traffic["limit_gb"] = None if identity_changed else _policy_limit
            traffic["over_limit"] = (traffic["limit_gb"] is not None
                                     and traffic["total_gb"] > traffic["limit_gb"])
        return InstanceOut(
            name=name,
            region=region,
            state="unknown",
            traffic=InstanceTraffic(**traffic) if traffic else None,
            monthly_limit_gb=setting.monthly_limit_gb,
            auto_stop_on_limit=auto_stop_enabled,
            configured_auto_stop_on_limit=bool(setting.auto_stop_on_limit),
            auto_stop_conflict=conflict,
            auto_stop_identity_changed=identity_changed,
            note=setting.note,
            credential_id=cred.id,
            account_label=cred.account_label,
        )
