import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import AwsCredential, OperationLog, User
from app.schemas import (
    CredentialCreate,
    CredentialItem,
    CredentialOut,
    CredentialUpdate,
    RegionEnableOut,
    RegionOptStatusOut,
)
from app.proxy_config import (
    ProxyConfigError,
    mask_proxy_url,
    parse_proxy_url,
    sanitize_error_message,
)
from app.rate_limit import (
    REGION_ENABLE_MAX_CALLS,
    REGION_ENABLE_WINDOW_SECONDS,
    REGION_STATUS_MAX_CALLS,
    REGION_STATUS_WINDOW_SECONDS,
    region_limiter,
)
from app.security import decrypt_secret, encrypt_secret, mask_access_key
from app.services import account as acct
from app.services import billing as billing_svc
from app.services import lightsail as ls
from app.services.traffic import get_credential_proxy_url, reconcile_auto_stop_conflicts

router = APIRouter(prefix="/credentials", tags=["credentials"])
logger = logging.getLogger(__name__)


def _remaining_vcpu(cred: AwsCredential) -> float | None:
    if cred.vcpu_quota is None or cred.used_vcpu is None:
        return None
    try:
        return max(0.0, float(cred.vcpu_quota) - float(cred.used_vcpu))
    except (TypeError, ValueError):
        return None


def _proxy_masked(cred: AwsCredential) -> str | None:
    """脱敏展示该凭证的代理；未配为 None，解密失败显示 ****。"""
    if not cred.proxy_url_enc:
        return None
    try:
        return mask_proxy_url(decrypt_secret(cred.proxy_url_enc))
    except Exception:  # noqa: BLE001
        return "****"


def _stored_proxy(cred: AwsCredential) -> str | None:
    """取已存代理；解密或使用时校验失败均拒绝请求。"""
    try:
        return get_credential_proxy_url(cred)
    except ProxyConfigError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ProxyConfigError("已保存的代理配置无法解密；请明确修复或清除代理") from exc


def _account_identity(ak: str, sk: str, proxy_url: str | None) -> str | None:
    try:
        return billing_svc.get_sts_identity(ak, sk, proxy_url)
    except Exception as exc:  # noqa: BLE001
        logger.info("STS 身份尚未确认：%s", sanitize_error_message(str(exc)))
        return None


def _record_account_identity(cred: AwsCredential, account_id: str | None) -> None:
    cred.account_id = account_id
    if account_id:
        cred.last_confirmed_account_id = account_id


def _parse_proxy_or_400(value: str | None) -> str | None:
    try:
        return parse_proxy_url(value)
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理地址无效: {exc}") from exc


def _item(cred: AwsCredential) -> CredentialItem:
    try:
        ak = decrypt_secret(cred.access_key_id_enc)
        masked = mask_access_key(ak)
    except Exception:  # noqa: BLE001
        masked = "****"
    return CredentialItem(
        id=cred.id,
        access_key_masked=masked,
        account_label=cred.account_label,
        account_id=cred.account_id,
        account_identity_status="confirmed" if cred.account_id else "unknown",
        auto_stop_conflict=bool(cred.auto_stop_conflict),
        proxy_masked=_proxy_masked(cred),
        is_default=bool(cred.is_default),
        last_validated_at=cred.last_validated_at,
        created_at=cred.created_at,
        vcpu_quota=cred.vcpu_quota,
        vcpu_tier=cred.vcpu_tier,
        static_ip_quota=cred.static_ip_quota,
        used_vcpu=cred.used_vcpu,
        used_instance_count=cred.used_instance_count,
        remaining_vcpu=_remaining_vcpu(cred),
        quota_region=cred.quota_region,
        quota_message=cred.quota_message,
        quota_checked_at=cred.quota_checked_at,
    )


def _apply_quota_to_cred(
    cred: AwsCredential, ak: str, sk: str, region: str = "us-east-1", proxy_url: str | None = None
) -> None:
    """查询并写回 Lightsail 配额字段（失败不抛，写入 message）。"""
    info = ls.get_lightsail_account_quotas(ak, sk, region=region, include_usage=True, proxy_url=proxy_url)
    cred.vcpu_quota = info.get("vcpu_quota")
    cred.vcpu_tier = info.get("vcpu_tier")
    cred.static_ip_quota = info.get("static_ip_quota")
    cred.used_vcpu = info.get("used_vcpu")
    cred.used_instance_count = info.get("used_instance_count")
    cred.quota_region = info.get("region") or region
    msg = info.get("message")
    if info.get("error") and not msg:
        msg = f"配额查询失败: {info['error']}"
    cred.quota_message = msg
    cred.quota_checked_at = datetime.now(timezone.utc)


def _list_out(user: User) -> CredentialOut:
    items = [_item(c) for c in sorted(user.credentials, key=lambda x: (not x.is_default, x.id))]
    default = next((i for i in items if i.is_default), items[0] if items else None)
    return CredentialOut(
        has_credentials=bool(items),
        items=items,
        id=default.id if default else None,
        access_key_masked=default.access_key_masked if default else None,
        account_label=default.account_label if default else None,
        last_validated_at=default.last_validated_at if default else None,
    )


def _clear_other_defaults(db: Session, user_id: int, keep_id: int | None = None) -> None:
    q = db.query(AwsCredential).filter(AwsCredential.user_id == user_id)
    if keep_id is not None:
        q = q.filter(AwsCredential.id != keep_id)
    for c in q.all():
        if c.is_default:
            c.is_default = False
            db.add(c)


@router.get("", response_model=CredentialOut)
def list_credentials(user: User = Depends(get_current_user)) -> CredentialOut:
    return _list_out(user)


@router.post("", response_model=CredentialOut, status_code=201)
def create_credential(
    body: CredentialCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CredentialOut:
    ak = body.access_key_id.strip()
    sk = body.secret_access_key.strip()
    proxy_url = _parse_proxy_or_400(body.proxy_url)
    try:
        # 新建时就用该代理校验：代理不通当场报错，不等以后调用才发现。
        ls.validate_credentials(ak, sk, proxy_url)
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=f"AWS 凭证校验失败: {exc.message}")
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}")

    now = datetime.now(timezone.utc)
    make_default = body.is_default or not user.credentials
    if make_default:
        _clear_other_defaults(db, user.id)

    cred = AwsCredential(
        user_id=user.id,
        access_key_id_enc=encrypt_secret(ak),
        secret_access_key_enc=encrypt_secret(sk),
        account_label=body.account_label,
        proxy_url_enc=encrypt_secret(proxy_url) if proxy_url else None,
        is_default=make_default,
        last_validated_at=now,
    )
    _record_account_identity(cred, _account_identity(ak, sk, proxy_url))
    _apply_quota_to_cred(cred, ak, sk, proxy_url=proxy_url)
    db.add(cred)
    db.flush()
    db.add(
        OperationLog(
            user_id=user.id,
            action="add_credentials",
            status="success",
            message=body.account_label or mask_access_key(body.access_key_id.strip()),
        )
    )
    reconcile_auto_stop_conflicts(db, user)
    db.commit()
    db.refresh(user)
    return _list_out(user)


@router.put("/{credential_id}", response_model=CredentialOut)
def update_credential(
    credential_id: int,
    body: CredentialUpdate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CredentialOut:
    cred = (
        db.query(AwsCredential)
        .filter(AwsCredential.id == credential_id, AwsCredential.user_id == user.id)
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")

    # 代理：字段未出现=保持；空串=清除（恢复直连）；有值=更新。
    # 换密钥或换代理都要用生效后的代理重新校验一次，不通当场报错。
    proxy_changed = "proxy_url" in body.model_fields_set
    new_proxy = _parse_proxy_or_400(body.proxy_url) if proxy_changed else None
    try:
        effective_proxy = new_proxy if proxy_changed else _stored_proxy(cred)
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    keys_updated = bool(body.access_key_id or body.secret_access_key)
    if keys_updated or proxy_changed:
        try:
            old_ak = decrypt_secret(cred.access_key_id_enc)
            old_sk = decrypt_secret(cred.secret_access_key_enc)
        except Exception:  # noqa: BLE001
            old_ak, old_sk = "", ""
        ak = (body.access_key_id or old_ak).strip()
        sk = (body.secret_access_key or old_sk).strip()
        try:
            ls.validate_credentials(ak, sk, effective_proxy)
        except ls.LightsailError as exc:
            raise HTTPException(status_code=400, detail=f"AWS 凭证校验失败: {exc.message}")
        except ProxyConfigError as exc:
            raise HTTPException(status_code=400, detail=f"代理不可用: {exc}")
        if keys_updated:
            cred.last_confirmed_account_id = None
        if body.access_key_id:
            cred.access_key_id_enc = encrypt_secret(body.access_key_id.strip())
        if body.secret_access_key:
            cred.secret_access_key_enc = encrypt_secret(body.secret_access_key.strip())
        if keys_updated or proxy_changed:
            _record_account_identity(cred, _account_identity(ak, sk, effective_proxy))
            cred.last_validated_at = datetime.now(timezone.utc)
        if keys_updated:
            _apply_quota_to_cred(cred, ak, sk, proxy_url=effective_proxy)
    if proxy_changed:
        cred.proxy_url_enc = encrypt_secret(new_proxy) if new_proxy else None

    if body.account_label is not None:
        cred.account_label = body.account_label
    if body.is_default is True:
        _clear_other_defaults(db, user.id, keep_id=cred.id)
        cred.is_default = True
    elif body.is_default is False and cred.is_default:
        # 不允许没有任何默认：若取消，自动把另一组设为默认
        cred.is_default = False
        other = (
            db.query(AwsCredential)
            .filter(AwsCredential.user_id == user.id, AwsCredential.id != cred.id)
            .order_by(AwsCredential.id.asc())
            .first()
        )
        if other:
            other.is_default = True
            db.add(other)
        else:
            cred.is_default = True

    db.add(cred)
    db.flush()
    reconcile_auto_stop_conflicts(db, user)
    db.commit()
    db.refresh(user)
    return _list_out(user)


@router.post("/{credential_id}/default", response_model=CredentialOut)
def set_default(
    credential_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CredentialOut:
    cred = (
        db.query(AwsCredential)
        .filter(AwsCredential.id == credential_id, AwsCredential.user_id == user.id)
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    _clear_other_defaults(db, user.id, keep_id=cred.id)
    cred.is_default = True
    db.add(cred)
    db.commit()
    db.refresh(user)
    return _list_out(user)


@router.post("/{credential_id}/validate", response_model=CredentialItem)
def validate_one(
    credential_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CredentialItem:
    cred = (
        db.query(AwsCredential)
        .filter(AwsCredential.id == credential_id, AwsCredential.user_id == user.id)
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    try:
        ak = decrypt_secret(cred.access_key_id_enc)
        sk = decrypt_secret(cred.secret_access_key_enc)
        proxy_url = get_credential_proxy_url(cred)
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"解密凭证失败: {exc}") from exc
    try:
        ls.validate_credentials(ak, sk, proxy_url)
    except ls.LightsailError as exc:
        _record_account_identity(cred, None)
        reconcile_auto_stop_conflicts(db, user)
        db.commit()
        raise HTTPException(status_code=400, detail=f"校验失败: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        _record_account_identity(cred, None)
        reconcile_auto_stop_conflicts(db, user)
        db.commit()
        raise HTTPException(status_code=400, detail=sanitize_error_message(str(exc)))
    _record_account_identity(cred, _account_identity(ak, sk, proxy_url))
    cred.last_validated_at = datetime.now(timezone.utc)
    _apply_quota_to_cred(cred, ak, sk, proxy_url=proxy_url)
    db.add(cred)
    reconcile_auto_stop_conflicts(db, user)
    db.commit()
    db.refresh(cred)
    return _item(cred)


_REGION_RE = re.compile(r"^[a-z]{2}-[a-z]+-[0-9]+$")


def _check_region_or_400(region: str) -> str:
    if not _REGION_RE.match(region):
        raise HTTPException(status_code=400, detail="地区格式无效")
    return region


def _get_cred_or_404(db: Session, credential_id: int, user: User) -> AwsCredential:
    cred = (
        db.query(AwsCredential)
        .filter(AwsCredential.id == credential_id, AwsCredential.user_id == user.id)
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    return cred


def _decrypt_cred_keys(cred: AwsCredential) -> tuple[str, str, str | None]:
    try:
        ak = decrypt_secret(cred.access_key_id_enc)
        sk = decrypt_secret(cred.secret_access_key_enc)
        return ak, sk, get_credential_proxy_url(cred)
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"解密凭证失败: {exc}") from exc


@router.get("/{credential_id}/regions/{region}/status", response_model=RegionOptStatusOut)
def region_opt_status(
    credential_id: int,
    region: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RegionOptStatusOut:
    """查询该账号下某区域的启用状态；凭证无效或缺权限时返回 UNKNOWN + 说明（200）。"""
    _check_region_or_400(region)
    cred = _get_cred_or_404(db, credential_id, user)
    if not region_limiter.allow(
        f"region-status:{user.id}:{credential_id}",
        REGION_STATUS_MAX_CALLS,
        REGION_STATUS_WINDOW_SECONDS,
    ):
        raise HTTPException(status_code=429, detail="区域状态查询过于频繁，请稍后再试")
    ak, sk, proxy_url = _decrypt_cred_keys(cred)
    try:
        status = acct.get_region_opt_status(ak, sk, region, proxy_url)
    except acct.AccountError as exc:
        return RegionOptStatusOut(region=region, status="UNKNOWN", message=exc.message)
    except Exception as exc:  # noqa: BLE001
        return RegionOptStatusOut(
            region=region, status="UNKNOWN", message=sanitize_error_message(str(exc))
        )
    return RegionOptStatusOut(region=region, status=status)


@router.post("/{credential_id}/regions/{region}/enable", response_model=RegionEnableOut)
def region_enable(
    credential_id: int,
    region: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RegionEnableOut:
    """启用该账号下某区域（Account Management API）；返回启用后的即时状态。"""
    _check_region_or_400(region)
    cred = _get_cred_or_404(db, credential_id, user)
    if not region_limiter.allow(
        f"region-enable:{user.id}:{credential_id}",
        REGION_ENABLE_MAX_CALLS,
        REGION_ENABLE_WINDOW_SECONDS,
    ):
        raise HTTPException(status_code=429, detail="区域启用请求过于频繁，请稍后再试")
    ak, sk, proxy_url = _decrypt_cred_keys(cred)
    try:
        status = acct.enable_region(ak, sk, region, proxy_url)
    except acct.AccountError as exc:
        db.add(
            OperationLog(
                user_id=user.id,
                action="enable_region",
                status="failed",
                region=region,
                target=f"credential #{credential_id}",
                message=exc.message,
            )
        )
        db.commit()
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except Exception as exc:  # noqa: BLE001
        msg = sanitize_error_message(str(exc))
        db.add(
            OperationLog(
                user_id=user.id,
                action="enable_region",
                status="failed",
                region=region,
                target=f"credential #{credential_id}",
                message=msg,
            )
        )
        db.commit()
        raise HTTPException(status_code=400, detail=msg) from exc
    if status == "ENABLED":
        msg = f"区域 {region} 已启用"
    else:
        msg = f"区域 {region} 正在启用，生效需要几分钟，稍后重试"
    db.add(
        OperationLog(
            user_id=user.id,
            action="enable_region",
            status="success",
            region=region,
            target=f"credential #{credential_id}",
            message=msg,
        )
    )
    db.commit()
    return RegionEnableOut(region=region, status=status, message=msg)


@router.post("/{credential_id}/quotas", response_model=CredentialItem)
def refresh_quotas(
    credential_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CredentialItem:
    """检测该账号 Lightsail 配额（vCPU 档位如 5V/8V/32V，以及静态 IP 等）。"""
    cred = (
        db.query(AwsCredential)
        .filter(AwsCredential.id == credential_id, AwsCredential.user_id == user.id)
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    try:
        ak = decrypt_secret(cred.access_key_id_enc)
        sk = decrypt_secret(cred.secret_access_key_enc)
        proxy_url = get_credential_proxy_url(cred)
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"解密凭证失败: {exc}")
    _apply_quota_to_cred(cred, ak, sk, proxy_url=proxy_url)
    db.add(cred)
    db.add(
        OperationLog(
            user_id=user.id,
            action="refresh_quotas",
            status="success" if cred.vcpu_quota is not None else "partial",
            message=cred.quota_message or cred.vcpu_tier or f"credential #{credential_id}",
        )
    )
    db.commit()
    db.refresh(cred)
    return _item(cred)


@router.delete("/{credential_id}")
def delete_credential(
    credential_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    cred = (
        db.query(AwsCredential)
        .filter(AwsCredential.id == credential_id, AwsCredential.user_id == user.id)
        .first()
    )
    if not cred:
        raise HTTPException(status_code=404, detail="凭证不存在")
    was_default = cred.is_default
    db.delete(cred)
    db.flush()
    if was_default:
        other = (
            db.query(AwsCredential)
            .filter(AwsCredential.user_id == user.id)
            .order_by(AwsCredential.id.asc())
            .first()
        )
        if other:
            other.is_default = True
            db.add(other)
    db.add(
        OperationLog(
            user_id=user.id,
            action="delete_credentials",
            status="success",
            message=f"deleted credential #{credential_id}",
        )
    )
    reconcile_auto_stop_conflicts(db, user)
    db.commit()
    return {"message": "已删除凭证"}


# ---- 兼容旧接口：PUT /credentials 作为「新增或更新默认」----
@router.put("", response_model=CredentialOut)
def put_compat(
    body: CredentialCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CredentialOut:
    """兼容：若无凭证则新增；若有默认凭证则更新默认那一组。"""
    ak = body.access_key_id.strip()
    sk = body.secret_access_key.strip()
    req_proxy = _parse_proxy_or_400(body.proxy_url)

    now = datetime.now(timezone.utc)
    default = user.default_credential
    # 未传 proxy_url 时沿用默认凭证已存的代理；传了（哪怕空串）就按传的来。
    try:
        proxy_url = req_proxy if "proxy_url" in body.model_fields_set else (
            _stored_proxy(default) if default is not None else None
        )
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc
    try:
        ls.validate_credentials(ak, sk, proxy_url)
    except ls.LightsailError as exc:
        raise HTTPException(status_code=400, detail=f"AWS 凭证校验失败: {exc.message}")
    except ProxyConfigError as exc:
        raise HTTPException(status_code=400, detail=f"代理不可用: {exc}") from exc

    identity = _account_identity(ak, sk, proxy_url)
    if default is None:
        cred = AwsCredential(
            user_id=user.id,
            access_key_id_enc=encrypt_secret(ak),
            secret_access_key_enc=encrypt_secret(sk),
            account_label=body.account_label,
            proxy_url_enc=encrypt_secret(proxy_url) if proxy_url else None,
            is_default=True,
            last_validated_at=now,
            account_id=identity,
            last_confirmed_account_id=identity,
        )
        _apply_quota_to_cred(cred, ak, sk, proxy_url=proxy_url)
        db.add(cred)
        action = "add_credentials"
    else:
        default.access_key_id_enc = encrypt_secret(ak)
        default.secret_access_key_enc = encrypt_secret(sk)
        default.last_confirmed_account_id = None
        _record_account_identity(default, identity)
        if body.account_label is not None:
            default.account_label = body.account_label
        if "proxy_url" in body.model_fields_set:
            default.proxy_url_enc = encrypt_secret(proxy_url) if proxy_url else None
        default.last_validated_at = now
        _apply_quota_to_cred(default, ak, sk, proxy_url=proxy_url)
        db.add(default)
        action = "update_credentials"
    db.add(
        OperationLog(
            user_id=user.id,
            action=action,
            status="success",
            message=body.account_label or mask_access_key(body.access_key_id.strip()),
        )
    )
    reconcile_auto_stop_conflicts(db, user)
    db.commit()
    db.refresh(user)
    return _list_out(user)
