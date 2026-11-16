"""AWS 账号管理：区域启用状态查询与启用。

香港（ap-east-1）等区域默认未启用（opt-in），账号没开时调该区域接口
会报 UnrecognizedClientException（跟密钥无关）。本模块用 Account
Management API（固定 us-east-1）在用户点确认后启用区域，生效后重试。

需要 IAM 权限：account:GetRegionOptStatus、account:EnableRegion
（见 README「IAM 权限」一节）。组织成员账号不能自己启用区域，
须由管理账号操作。
"""
from __future__ import annotations

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.proxy_config import proxy_config

ACCOUNT_REGION = "us-east-1"

# GetRegionOptStatus 的 RegionOptStatus 取值（boto3 1.44.3 服务模型）。
ENABLED_STATUSES = ("ENABLED", "ENABLED_BY_DEFAULT")


class AccountError(Exception):
    """账号管理 API 错误；路由层直接把 message 当 400 返回。"""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


def make_account_client(
    access_key_id: str, secret_access_key: str, proxy_url: str | None = None
):
    """Account Management 客户端（固定 us-east-1，该 API 只有该区域端点）。"""
    return boto3.client(
        "account",
        region_name=ACCOUNT_REGION,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=proxy_config(proxy_url),
    )


def _raise_from_aws(exc: Exception) -> None:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        code = err.get("Code", "ClientError")
        msg = err.get("Message", str(exc))
        if code == "AccessDeniedException":
            raise AccountError(
                "权限不足：密钥需要 account:EnableRegion / account:GetRegionOptStatus 权限；"
                "如果是组织成员账号，需由管理账号启用该区域",
                code=code,
            ) from exc
        if code in ("UnrecognizedClientException", "InvalidClientTokenId"):
            raise AccountError(
                "AWS 凭证无效：请检查 Access Key 与 Secret 是否正确、是否为长期 IAM 密钥",
                code=code,
            ) from exc
        raise AccountError(f"AWS 错误 [{code}]: {msg}", code=code) from exc
    if isinstance(exc, BotoCoreError):
        raise AccountError(f"AWS 连接错误: {exc}") from exc
    raise AccountError(str(exc)) from exc


def get_region_opt_status(
    access_key_id: str, secret_access_key: str, region: str, proxy_url: str | None = None
) -> str:
    """返回区域启用状态：ENABLED / ENABLING / DISABLING / DISABLED / ENABLED_BY_DEFAULT。"""
    try:
        client = make_account_client(access_key_id, secret_access_key, proxy_url)
        resp = client.get_region_opt_status(RegionName=region)
        return resp.get("RegionOptStatus") or "UNKNOWN"
    except Exception as exc:  # noqa: BLE001
        _raise_from_aws(exc)
        return "UNKNOWN"


def enable_region(
    access_key_id: str, secret_access_key: str, region: str, proxy_url: str | None = None
) -> str:
    """启用区域，幂等：返回启用后的即时状态（ENABLED 或 ENABLING）。
    启用不是立刻生效，调用方需轮询 get_region_opt_status 到 ENABLED 再重试原操作。"""
    try:
        status = get_region_opt_status(access_key_id, secret_access_key, region, proxy_url)
    except AccountError as exc:
        if exc.code == "AccessDeniedException":
            # 读状态没权限时不直接拦：直接尝试启用（启用本身也可能没权限，
            # 那时再报权限不足）。
            status = None
        else:
            raise
    if status in ENABLED_STATUSES:
        return "ENABLED"
    if status == "ENABLING":
        return "ENABLING"
    try:
        client = make_account_client(access_key_id, secret_access_key, proxy_url)
        client.enable_region(RegionName=region)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConflictException":
            return "ENABLING"  # 已有启用操作在进行中
        if code == "ValidationException":
            # 如区域已启用：再查一次状态确认。
            return get_region_opt_status(access_key_id, secret_access_key, region, proxy_url)
        _raise_from_aws(exc)
        return "UNKNOWN"
    except Exception as exc:  # noqa: BLE001
        _raise_from_aws(exc)
        return "UNKNOWN"
    # 确认一次（读失败时按 ENABLING 返回，由调用方轮询）。
    try:
        return get_region_opt_status(access_key_id, secret_access_key, region, proxy_url)
    except AccountError:
        return "ENABLING"
