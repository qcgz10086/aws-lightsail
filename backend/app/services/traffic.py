from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict
import hashlib
import json

from sqlalchemy.orm import Session

from app.models import AwsCredential, InstanceSetting, TrafficUsage, User
from app.proxy_config import recheck_proxy_url
from app.security import decrypt_secret
from app.services import lightsail as ls


class NoCredentialsError(Exception):
    pass


def get_credential(user: User, credential_id: int | None = None) -> AwsCredential:
    if not user.credentials:
        raise NoCredentialsError("请先绑定 AWS 凭证")
    if credential_id is not None:
        for c in user.credentials:
            if c.id == credential_id:
                return c
        raise NoCredentialsError(f"凭证不存在: {credential_id}")
    cred = user.default_credential
    if not cred:
        raise NoCredentialsError("请先绑定 AWS 凭证")
    return cred


def get_credential_keys(cred: AwsCredential) -> tuple[str, str]:
    ak = decrypt_secret(cred.access_key_id_enc)
    sk = decrypt_secret(cred.secret_access_key_enc)
    return ak, sk


def get_credential_proxy_url(cred: AwsCredential) -> str | None:
    """解密凭证绑定的代理地址；未配置返回 None。

    解密失败时抛 RuntimeError（与密钥解密一致），不静默回落到直连，
    否则用户配好的代理会被悄悄绕过。
    取出后做使用时重校验（V-2）：保存后 DNS 可能被改回内网（重绑定），
    解析到内网/保留地址时抛 ProxyConfigError，调用方按 400 处理。
    """
    if not cred.proxy_url_enc:
        return None
    return recheck_proxy_url(decrypt_secret(cred.proxy_url_enc))


def get_user_aws_keys(user: User, credential_id: int | None = None) -> tuple[str, str]:
    """返回 (access_key, secret_key)。兼容旧调用；多 Key 时默认用 is_default。"""
    cred = get_credential(user, credential_id)
    return get_credential_keys(cred)


def get_user_aws_keys_and_cred(
    user: User, credential_id: int | None = None
) -> tuple[str, str, AwsCredential]:
    cred = get_credential(user, credential_id)
    ak, sk = get_credential_keys(cred)
    return ak, sk, cred


def bytes_to_gb(value: int | float) -> float:
    return round(float(value) / (1024 ** 3), 2)


def current_year_month(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y-%m")


def month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def effective_limit_gb(user: User, setting: InstanceSetting | None) -> float | None:
    if setting is not None and setting.monthly_limit_gb is not None:
        return setting.monthly_limit_gb
    return user.monthly_limit_gb


def effective_auto_stop(user: User, setting: InstanceSetting | None, account_id: str | None = None) -> bool:
    if setting is not None:
        if setting.auto_stop_conflict:
            return False
        if setting.auto_stop_configured:
            return bool(setting.auto_stop_on_limit and setting.auto_stop_account_id == account_id)
    return bool(user.auto_stop_on_limit_default)


def _calculate_auto_stop_conflicts(user: User, credentials: list[AwsCredential],
                                   settings: list[InstanceSetting]) -> dict:
    credentials_by_id = {cred.id: cred for cred in credentials}
    groups = defaultdict(list)
    for setting in settings:
        cred = credentials_by_id.get(setting.credential_id)
        # A previously bound strategy remains a conservative stop guard while a
        # credential identity is unknown, including after an unverified rotation.
        account_id = setting.auto_stop_account_id if cred else None
        if (setting.auto_stop_configured
                and account_id and len(account_id) == 12
                and account_id.isdigit()):
            groups[(account_id, setting.region, setting.instance_name)].append(setting)

    conflicts = {}
    for identity, rows in groups.items():
        enabled = {bool(row.auto_stop_on_limit) for row in rows}
        limits = {
            float(row.monthly_limit_gb if row.monthly_limit_gb is not None else user.monthly_limit_gb)
            if row.monthly_limit_gb is not None or user.monthly_limit_gb is not None else None
            for row in rows if row.auto_stop_on_limit
        }
        if (any(row.auto_stop_conflict for row in rows)
                or len(rows) > 1 and (len(enabled) > 1 or len(limits) > 1)):
            conflicts[identity] = tuple(rows)

    conflict_ids = {row.id for rows in conflicts.values() for row in rows}
    conflict_accounts = {account_id for account_id, _, _ in conflicts}
    for row in settings:
        row.auto_stop_conflict = row.id in conflict_ids
    conflict_credentials = {row.credential_id for rows in conflicts.values() for row in rows}
    for credential in credentials:
        identity = credential.account_id or credential.last_confirmed_account_id
        credential.auto_stop_conflict = identity in conflict_accounts or credential.id in conflict_credentials
    return conflicts


def reconcile_auto_stop_conflicts(db: Session, user: User) -> dict:
    db.flush()
    credentials = db.query(AwsCredential).filter(AwsCredential.user_id == user.id).all()
    settings = db.query(InstanceSetting).filter(InstanceSetting.user_id == user.id).all()
    return _calculate_auto_stop_conflicts(user, credentials, settings)


def resolve_auto_stop_conflict(db: Session, user: User, credential: AwsCredential,
                               region: str, name: str, acknowledged_ids: list[int],
                               expected_token: str | None) -> None:
    """Release a latched pause only after explicit acknowledgement of every policy."""
    db.flush()
    credentials = db.query(AwsCredential).filter(AwsCredential.user_id == user.id).all()
    settings = db.query(InstanceSetting).filter(InstanceSetting.user_id == user.id).all()
    identity = credential.account_id
    if not identity:
        raise ValueError("必须先确认所有竞争凭证的 AWS 账号身份")
    rows = [row for row in settings if row.auto_stop_configured
            and row.auto_stop_account_id == identity and row.region == region
            and row.instance_name == name]
    actual_ids = {row.credential_id for row in rows}
    if (not rows or not any(row.auto_stop_conflict for row in rows)
            or set(acknowledged_ids) != actual_ids or len(acknowledged_ids) != len(actual_ids)):
        raise ValueError("需明确确认该账号同实例的全部竞争凭证策略")
    current = {cred.id: cred.account_id for cred in credentials}
    if any(current.get(row.credential_id) != identity for row in rows):
        raise ValueError("竞争凭证身份未知或已变化，不能解除冲突暂停")
    if expected_token != _policy_confirmation_token(user, identity, rows, current):
        raise ValueError("策略已变化或尚未查看全部策略，请重新确认")
    enabled = {bool(row.auto_stop_on_limit) for row in rows}
    limits = {effective_limit_gb(user, row) for row in rows if row.auto_stop_on_limit}
    if len(enabled) > 1 or len(limits) > 1:
        raise ValueError("竞争策略尚未协调一致，不能解除冲突暂停")
    for row in rows:
        row.auto_stop_conflict = False
    _calculate_auto_stop_conflicts(user, credentials, settings)


def _policy_confirmation_token(user, identity, rows, current):
    values = [(row.credential_id, row.auto_stop_on_limit, effective_limit_gb(user, row),
               current.get(row.credential_id)) for row in sorted(rows, key=lambda row: row.credential_id)]
    payload = json.dumps([user.id, identity, values], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def auto_stop_conflict_details(db: Session, user: User, credential: AwsCredential,
                                region: str, name: str) -> dict:
    conflicts = reconcile_auto_stop_conflicts(db, user)
    identity = credential.account_id or credential.last_confirmed_account_id
    rows = conflicts.get((identity, region, name), ())
    credentials = {cred.id: cred for cred in user.credentials}
    return {"account_id": identity,
            "confirmation_token": _policy_confirmation_token(
                user, identity, rows, {cred.id: cred.account_id for cred in credentials.values()}),
            "strategies": [
        {"credential_id": row.credential_id, "auto_stop_on_limit": row.auto_stop_on_limit,
         "limit_gb": effective_limit_gb(user, row),
         "identity_confirmed": bool(credentials.get(row.credential_id)
                                    and credentials[row.credential_id].account_id == identity)}
        for row in sorted(rows, key=lambda row: row.credential_id)
    ]}


def resolve_auto_stop_policies(
    db: Session,
    user: User,
    instances: list[tuple[AwsCredential, str, str]],
    credentials: list[AwsCredential] | None = None,
    settings: list[InstanceSetting] | None = None,
) -> dict[tuple[int, str, str], tuple[bool, float | None, bool, bool]]:
    if credentials is None and settings is None:
        db.flush()
    credentials = credentials if credentials is not None else db.query(AwsCredential).filter(
        AwsCredential.user_id == user.id).all()
    settings = settings if settings is not None else db.query(InstanceSetting).filter(
        InstanceSetting.user_id == user.id).all()
    conflict_map = _calculate_auto_stop_conflicts(user, credentials, settings)
    settings_by_key = {(row.credential_id, row.region, row.instance_name): row for row in settings}
    confirmed = {cred.id: cred.account_id for cred in credentials}
    grouped_settings = defaultdict(list)
    for row in settings:
        if (row.auto_stop_configured and row.auto_stop_account_id
                and confirmed.get(row.credential_id) == row.auto_stop_account_id):
            grouped_settings[(row.auto_stop_account_id, row.region, row.instance_name)].append(row)

    policies = {}
    for credential, region, name in instances:
        key = (credential.id, region, name)
        direct = settings_by_key.get(key)
        identity = credential.account_id or credential.last_confirmed_account_id
        conflict_key = (identity, region, name)
        if conflict_key in conflict_map:
            policies[key] = (False, effective_limit_gb(user, direct), True, False)
            continue
        if credential.account_id and direct and direct.auto_stop_configured and (
            direct.auto_stop_account_id != credential.account_id
        ):
            policies[key] = (False, None, False, True)
            continue
        if not credential.account_id:
            if direct and direct.auto_stop_configured and direct.auto_stop_account_id:
                policies[key] = (False, None, False, True)
            elif direct and direct.auto_stop_configured:
                policies[key] = (bool(direct.auto_stop_on_limit), effective_limit_gb(user, direct),
                                 False, False)
            else:
                policies[key] = (bool(user.auto_stop_on_limit_default), user.monthly_limit_gb,
                                 False, bool(identity))
            continue
        if not identity or len(identity) != 12 or not identity.isdigit():
            policies[key] = (False, effective_limit_gb(user, direct), False, False)
            continue
        account_settings = grouped_settings.get((identity, region, name), [])
        if account_settings:
            selected = next((row for row in account_settings if row.credential_id == credential.id),
                            account_settings[0])
            policies[key] = (any(row.auto_stop_on_limit for row in account_settings),
                              effective_limit_gb(user, selected), False, False)
        elif direct is not None:
            policies[key] = (False, effective_limit_gb(user, direct), False, False)
        else:
            policies[key] = (bool(user.auto_stop_on_limit_default), user.monthly_limit_gb, False, False)
    return policies


def resolve_auto_stop_policy(db: Session, user: User, credential: AwsCredential,
                             region: str, instance_name: str) -> tuple[bool, float | None, bool, bool]:
    return resolve_auto_stop_policies(db, user, [(credential, region, instance_name)])[
        (credential.id, region, instance_name)
    ]


def has_auto_stop_conflict(db: Session, credential: AwsCredential, region: str, instance_name: str) -> bool:
    if not credential.account_id or not credential.auto_stop_conflict:
        return False
    settings = db.query(InstanceSetting).filter(
        InstanceSetting.user_id == credential.user_id,
        InstanceSetting.auto_stop_configured.is_(True),
        InstanceSetting.auto_stop_account_id == credential.account_id,
        InstanceSetting.region == region,
        InstanceSetting.instance_name == instance_name,
        InstanceSetting.auto_stop_conflict.is_(True),
    ).count()
    return settings >= 2


def conflicting_auto_stop_credentials(db: Session, credential: AwsCredential) -> set[int]:
    account_id = credential.account_id
    if not account_id or len(account_id) != 12 or not account_id.isdigit():
        return set()
    return {row.id for row in db.query(AwsCredential).filter(
        AwsCredential.user_id == credential.user_id,
        AwsCredential.account_id == account_id,
    ).all()}


def get_setting(
    db: Session,
    user_id: int,
    region: str,
    instance_name: str,
    credential_id: int | None = None,
) -> InstanceSetting | None:
    q = db.query(InstanceSetting).filter(
        InstanceSetting.user_id == user_id,
        InstanceSetting.region == region,
        InstanceSetting.instance_name == instance_name,
    )
    if credential_id is not None:
        q = q.filter(InstanceSetting.credential_id == credential_id)
    return q.first()


def upsert_traffic(
    db: Session,
    user_id: int,
    credential_id: int,
    region: str,
    instance_name: str,
    year_month: str,
    network_in_bytes: int,
    network_out_bytes: int,
    account_id: str | None = None,
) -> TrafficUsage:
    account_id = account_id or ""
    row = (
        db.query(TrafficUsage)
        .filter(
            TrafficUsage.user_id == user_id,
            TrafficUsage.credential_id == credential_id,
            TrafficUsage.region == region,
            TrafficUsage.instance_name == instance_name,
            TrafficUsage.year_month == year_month,
            TrafficUsage.account_id == account_id,
        )
        .first()
    )
    now = datetime.now(timezone.utc)
    if row is None:
        row = TrafficUsage(
            user_id=user_id,
            credential_id=credential_id,
            region=region,
            instance_name=instance_name,
            year_month=year_month,
            account_id=account_id,
            network_in_bytes=network_in_bytes,
            network_out_bytes=network_out_bytes,
            last_synced_at=now,
        )
        db.add(row)
    else:
        # 全量覆盖，禁止增量累加
        row.network_in_bytes = network_in_bytes
        row.network_out_bytes = network_out_bytes
        row.last_synced_at = now
        db.add(row)
    return row


def build_traffic_summary(
    db: Session, user: User, credential_id: int | None = None
) -> dict:
    ym = current_year_month()
    q = db.query(TrafficUsage).filter(
        TrafficUsage.user_id == user.id, TrafficUsage.year_month == ym
    )
    if credential_id is not None:
        q = q.filter(TrafficUsage.credential_id == credential_id)
    rows = q.all()

    settings = db.query(InstanceSetting).filter(InstanceSetting.user_id == user.id).all()
    setting_map = {
        (s.credential_id, s.region, s.instance_name): s for s in settings
    }
    instances = []
    region_totals: dict[str, dict] = {}

    credentials = {cred.id: cred for cred in user.credentials}
    deduplicated = {}
    for row in rows:
        identity = (("account", row.account_id, row.region, row.instance_name) if row.account_id
                    else ("credential", row.credential_id, row.region, row.instance_name))
        prior = deduplicated.get(identity)
        if prior is None or (row.last_synced_at is not None and (
            prior.last_synced_at is None or row.last_synced_at > prior.last_synced_at
        )):
            deduplicated[identity] = row

    policy_requests = [
        (credentials[row.credential_id], row.region, row.instance_name)
        for row in deduplicated.values() if row.credential_id in credentials
    ]
    policies = resolve_auto_stop_policies(db, user, policy_requests, list(credentials.values()), settings)
    for r in deduplicated.values():
        setting = setting_map.get((r.credential_id, r.region, r.instance_name))
        row_credential = credentials.get(r.credential_id)
        enabled, limit, auto_stop_conflict, identity_changed = policies.get(
            (r.credential_id, r.region, r.instance_name),
            (effective_auto_stop(user, setting), effective_limit_gb(user, setting), False, False),
        )
        if not row_credential or (r.account_id or None) != row_credential.account_id:
            enabled, limit, auto_stop_conflict, identity_changed = False, None, False, True
        auto_stop = enabled and not identity_changed
        total_bytes = (r.network_in_bytes or 0) + (r.network_out_bytes or 0)
        total_gb = bytes_to_gb(total_bytes)
        over = limit is not None and total_gb > limit
        instances.append(
            {
                "region": r.region,
                "name": r.instance_name,
                "credential_id": r.credential_id,
                "account_label": (
                    row_credential.account_label
                    if row_credential and row_credential.account_id == (r.account_id or None) else None
                ),
                "account_id": r.account_id or None,
                "in_gb": bytes_to_gb(r.network_in_bytes or 0),
                "out_gb": bytes_to_gb(r.network_out_bytes or 0),
                "total_gb": total_gb,
                "limit_gb": limit,
                "over_limit": over,
                "auto_stop_on_limit": auto_stop,
                "auto_stop_conflict": auto_stop_conflict,
                "year_month": ym,
            }
        )
        if not r.account_id:
            continue
        bucket = region_totals.setdefault(
            r.region, {"region": r.region, "total_gb": 0.0, "instance_count": 0}
        )
        bucket["total_gb"] = round(bucket["total_gb"] + total_gb, 2)
        bucket["instance_count"] += 1

    by_region = sorted(region_totals.values(), key=lambda x: x["region"])
    instances.sort(key=lambda x: (x["region"], x["name"], x.get("credential_id") or 0))
    return {
        "year_month": ym,
        "instances": instances,
        "by_region": by_region,
        "note": "流量基于 Lightsail 指标估算；账号未确认的历史记录单独展示，不计入地区确认总额",
    }


def traffic_for_instance(
    db: Session,
    user: User,
    region: str,
    name: str,
    credential_id: int | None = None,
    account_id: str | None = None,
    setting: InstanceSetting | None = None,
    usage: TrafficUsage | None = None,
    preloaded: bool = False,
) -> dict | None:
    ym = current_year_month()
    q = db.query(TrafficUsage).filter(
        TrafficUsage.user_id == user.id,
        TrafficUsage.region == region,
        TrafficUsage.instance_name == name,
        TrafficUsage.year_month == ym,
    )
    if credential_id is not None:
        q = q.filter(TrafficUsage.credential_id == credential_id)
    row = usage if preloaded else q.filter(TrafficUsage.account_id == (account_id or "")).first()
    if not preloaded:
        setting = get_setting(db, user.id, region, name, credential_id)
    limit = effective_limit_gb(user, setting)
    if row is None:
        return {
            "in_gb": 0.0,
            "out_gb": 0.0,
            "total_gb": 0.0,
            "limit_gb": limit,
            "over_limit": False,
            "year_month": ym,
        }
    total = (row.network_in_bytes or 0) + (row.network_out_bytes or 0)
    total_gb = bytes_to_gb(total)
    return {
        "in_gb": bytes_to_gb(row.network_in_bytes or 0),
        "out_gb": bytes_to_gb(row.network_out_bytes or 0),
        "total_gb": total_gb,
        "limit_gb": limit,
        "over_limit": limit is not None and total_gb > limit,
        "year_month": ym,
    }
