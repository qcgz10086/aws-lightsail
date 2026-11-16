"""每个 AWS 凭证单独的代理配置。

一个面板账号可以绑多组 AWS 凭证（= 多个 AWS 账号），每组凭证可以配
自己专用的代理；该凭证名下的全部 AWS 请求（Lightsail、Service Quotas、
STS、Billing、Cost Explorer、流量采集）都走这个代理。留空 = 直连。

代理地址只支持 http:// 与 https://（urllib3/botocore 能直接用的协议），
不支持 socks5（需额外装 PySocks，且 AWS 侧无必要）；地址可带认证，
形如 http://user:pass@host:port。
"""
from __future__ import annotations

from urllib.parse import urlparse

from botocore.config import Config


class ProxyConfigError(ValueError):
    """代理地址格式错误；路由层直接把 message 当 400 返回。"""


def parse_proxy_url(value: str | None) -> str | None:
    """校验并规范化代理地址；空/None 表示直连，返回 None。"""
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ProxyConfigError("代理只支持 http:// 或 https://，不支持 socks5 等其他协议")
    if not parsed.hostname:
        raise ProxyConfigError("代理地址缺少主机名，例如 http://127.0.0.1:8080")
    try:
        port = parsed.port
    except ValueError:
        # urlparse 对越界端口直接抛 ValueError，不返回数值
        raise ProxyConfigError("代理端口无效（1-65535）") from None
    if port is not None and not 1 <= port <= 65535:
        raise ProxyConfigError("代理端口无效（1-65535）")
    return url


def mask_proxy_url(value: str | None) -> str | None:
    """脱敏展示：http://user:***@host:port；无认证时直接显示 host:port。"""
    if not value:
        return None
    try:
        parsed = urlparse(value)
        host = parsed.hostname or "****"
        port = f":{parsed.port}" if parsed.port else ""
        if parsed.username:
            return f"{parsed.scheme}://{parsed.username}:***@{host}{port}"
        return f"{parsed.scheme}://{host}{port}"
    except Exception:  # noqa: BLE001 - 脱敏失败也不影响主流程
        return "****"


def proxy_config(proxy_url: str | None) -> Config | None:
    """给 boto3.client 用的 Config；无代理时返回 None（直连）。"""
    if not proxy_url:
        return None
    return Config(proxies={"http": proxy_url, "https": proxy_url})
