"""每个 AWS 凭证单独的代理配置。

一个面板账号可以绑多组 AWS 凭证（= 多个 AWS 账号），每组凭证可以配
自己专用的代理；该凭证名下的全部 AWS 请求（Lightsail、Service Quotas、
STS、Billing、Cost Explorer、流量采集）都走这个代理。留空 = 直连。

代理地址支持 http://、https://、socks5://、socks5h://。SOCKS5 由 PySocks
提供（urllib3 的可选依赖，见 requirements.txt）；socks5h 表示域名由代理
端解析，不泄漏本地 DNS，建议优先用它。地址可带认证，形如
http://user:pass@host:port。

注意：botocore 1.44.3 原生不支持 SOCKS 代理，有两处卡点，本模块导入时
一并打补丁（只针对 socks5:// 与 socks5h://，其余行为保持原样）：
1. ProxyConfiguration._fix_proxy_url 会把非 http(s) 的代理地址前面硬拼
   一个 "http://"（socks5h:// 会被改成 http://socks5h://）。
2. URLLib3Session._get_proxy_manager 经 urllib3.proxy_from_url 建代理
   管理器，而 urllib3 2.x 的 proxy_from_url 只会建普通 ProxyManager，
   socks 协议会抛 ProxySchemeUnknown；且建完后 botocore 会把
   pool_classes_by_scheme 覆盖成 AWS 池（其 ConnectionCls 不是 SOCKS
   的），会把 SOCKS 路由丢掉。所以 SOCKS 分支直接建
   urllib3.contrib.socks.SOCKSProxyManager，且不覆盖它的 SOCKS 池。
   SOCKS 认证走协议本身（URL 里的用户名/密码），HTTP 的
   Proxy-Authorization 头对 SOCKS 无用，且 SOCKSProxyManager 不接受
   proxy_headers 参数，传入会 TypeError，故去掉。
   代价：走 SOCKS 的请求不用 botocore 的 AWS 池，只丢了 100-Continue
   支持，对 Lightsail 这类小请求无影响。
boto3 钉在 1.44.3，升级 boto3 后需复查 botocore 是否已原生支持 SOCKS
（若支持可删掉此补丁）。
"""
from __future__ import annotations

from urllib.parse import urlparse

from botocore.config import Config

try:
    from botocore.httpsession import ProxyConfiguration as _BotocoreProxyConfiguration
    from botocore.httpsession import URLLib3Session as _BotocoreURLLib3Session
    from urllib3.contrib.socks import SOCKSProxyManager as _SOCKSProxyManager

    _orig_fix_proxy_url = _BotocoreProxyConfiguration._fix_proxy_url

    def _fix_proxy_url_allow_socks(self, proxy_url):
        if proxy_url.startswith(("socks5://", "socks5h://")):
            return proxy_url
        return _orig_fix_proxy_url(self, proxy_url)

    _BotocoreProxyConfiguration._fix_proxy_url = _fix_proxy_url_allow_socks

    _orig_get_proxy_manager = _BotocoreURLLib3Session._get_proxy_manager

    def _get_proxy_manager_with_socks(self, proxy_url):
        if not proxy_url.startswith(("socks5://", "socks5h://")):
            return _orig_get_proxy_manager(self, proxy_url)
        if proxy_url not in self._proxy_managers:
            proxy_ssl_context = self._setup_proxy_ssl_context(proxy_url)
            proxy_manager_kwargs = self._get_pool_manager_kwargs()
            proxy_manager_kwargs.update(
                self._proxies_kwargs(proxy_ssl_context=proxy_ssl_context)
            )
            proxy_manager_kwargs.pop("proxy_headers", None)
            proxy_manager_kwargs.pop("proxy_ssl_context", None)
            proxy_manager = _SOCKSProxyManager(proxy_url, **proxy_manager_kwargs)
            # 不覆盖 pool_classes_by_scheme：SOCKSProxyManager 自带的
            # SOCKS 池才是走 SOCKS 的关键。
            self._proxy_managers[proxy_url] = proxy_manager
        return self._proxy_managers[proxy_url]

    _BotocoreURLLib3Session._get_proxy_manager = _get_proxy_manager_with_socks
except ImportError:  # 未装 boto3/urllib3 socks 的纯解析场景，跳过补丁
    pass


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
    if parsed.scheme not in ("http", "https", "socks5", "socks5h"):
        raise ProxyConfigError("代理只支持 http://、https://、socks5://、socks5h://")
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
