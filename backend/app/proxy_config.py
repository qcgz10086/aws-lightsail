"""每个 AWS 凭证单独的代理配置。

一个面板账号可以绑多组 AWS 凭证（= 多个 AWS 账号），每组凭证可以配
自己专用的代理；该凭证名下的全部 AWS 请求（Lightsail、Service Quotas、
STS、Billing、Cost Explorer、流量采集）都走这个代理。留空 = 直连。

代理地址支持 http://、https://、socks5://、socks5h://。SOCKS5 由 PySocks
提供（urllib3 的可选依赖，见 requirements.txt）；socks5h 表示域名由代理
端解析，不泄漏本地 DNS，建议优先用它。地址可带认证，形如
http://user:pass@host:port。
地址会规范化（scheme 统一小写，按解析结果重组）；内网/保留地址不允许
做代理主机（登录用户否则能拿后端探测内网），只接受公网地址。

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

import ipaddress
import re
import socket
import threading
from urllib.parse import urlparse, urlunparse

from botocore.config import Config

# SOCKS 补丁是否成功打上。打不上时应用照常启动（http/https 不受影响），
# SOCKS 在 proxy_config() 里明确报错，不会静默走直连或走坏掉的路径。
_SOCKS_PATCH_APPLIED = False
try:
    from botocore.httpsession import ProxyConfiguration as _BotocoreProxyConfiguration
    from botocore.httpsession import URLLib3Session as _BotocoreURLLib3Session
    from urllib3.contrib.socks import SOCKSProxyManager as _SOCKSProxyManager

    # 补丁依赖的 botocore 私有接口；未来 botocore 改名/删除时不让模块
    # 导入直接炸掉启动，改为降级（见 _SOCKS_PATCH_APPLIED）。
    _patched_attrs = (
        (_BotocoreProxyConfiguration, "_fix_proxy_url"),
        (_BotocoreURLLib3Session, "_get_proxy_manager"),
        (_BotocoreURLLib3Session, "_setup_proxy_ssl_context"),
        (_BotocoreURLLib3Session, "_get_pool_manager_kwargs"),
        (_BotocoreURLLib3Session, "_proxies_kwargs"),
    )
    if not all(hasattr(obj, name) for obj, name in _patched_attrs):
        raise AttributeError("botocore 内部接口已变化，SOCKS 补丁无法应用")

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
    _SOCKS_PATCH_APPLIED = True
except (ImportError, AttributeError):
    # 未装 boto3/urllib3 socks，或 botocore 内部接口已变化：跳过补丁，
    # 应用照常启动；SOCKS 在 proxy_config() 里明确报错。
    pass


class ProxyConfigError(ValueError):
    """代理地址格式错误；路由层直接把 message 当 400 返回。"""


_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".intranet")

# DNS 解析超时（秒）。测试里会调小它来覆盖超时分支。
_DNS_TIMEOUT_SECONDS = 5

# 使用时重校验的 DNS 解析超时（秒）：比保存时短，避免一次 DNS 卡顿
# 拖慢整次 AWS 调用。
_USE_TIME_DNS_TIMEOUT_SECONDS = 2


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """把主机名按字面 IP 解析；支持标准形式与 inet_aton 的简写/进制形式
    （如 127.1、0x7f.0.0.1、0x7f000001、0177.0.0.1、2130706433）。
    解析不到返回 None（按域名处理）。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    if not re.fullmatch(r"[0-9a-fA-FxX.]+", host):
        return None
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    nums: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                nums.append(int(part, 16))
            elif len(part) > 1 and part.startswith("0"):
                nums.append(int(part, 8))
            else:
                nums.append(int(part, 10))
        except ValueError:
            return None
    try:
        if len(nums) == 1:
            (n,) = nums
            if not 0 <= n <= 0xFFFFFFFF:
                return None
            return ipaddress.IPv4Address(n)
        if len(nums) == 2:
            a, b = nums
            if not 0 <= a <= 255 and 0 <= b <= 0xFFFFFF:
                return None
            return ipaddress.IPv4Address((a << 24) | b)
        if len(nums) == 3:
            a, b, c = nums
            if not 0 <= a <= 255 and 0 <= b <= 255 and 0 <= c <= 0xFFFF:
                return None
            return ipaddress.IPv4Address((a << 24) | (b << 16) | c)
        a, b, c, d = nums
        if not all(0 <= x <= 255 for x in (a, b, c, d)):
            return None
        return ipaddress.IPv4Address((a << 24) | (b << 16) | (c << 8) | d)
    except ipaddress.AddressValueError:
        return None


def _embedded_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """NAT64 前缀 64:ff9b::/96 内嵌的 IPv4（U-5）：is_global 对这类地址
    返回 True，必须把内嵌的 IPv4 拿出来单独复核（内嵌 169.254.169.254
    这类保留地址时拒绝）。IPv4-mapped（::ffff:0:0/96）不需要，is_global
    本身已正确处理。"""
    if isinstance(ip, ipaddress.IPv6Address) and ip in ipaddress.IPv6Network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _resolve_host_ips(host: str, timeout: float | None = None) -> list[str]:
    """解析主机名，返回全部解析到的 IP 字符串。

    解析失败或超时时抛 ProxyConfigError（fail-closed：解析不到就
    不让它做代理，避免把后端指向未知的地址）。解析用线程包一层，
    避免 getaddrinfo 长时间阻塞保存接口。"""
    if timeout is None:
        timeout = _DNS_TIMEOUT_SECONDS
    outcome: dict[str, object] = {}

    def _lookup() -> None:
        try:
            infos = socket.getaddrinfo(host, None)
            outcome["ips"] = [info[4][0] for info in infos]
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=_lookup, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    if thread.is_alive():
        raise ProxyConfigError(f"代理主机 {host} 解析超时，请检查代理地址")
    if "error" in outcome:
        raise ProxyConfigError(f"代理主机 {host} 无法解析，请检查代理地址")
    ips = outcome.get("ips")
    if not isinstance(ips, list) or not ips:
        raise ProxyConfigError(f"代理主机 {host} 无法解析，请检查代理地址")
    return ips


def _check_proxy_host_allowed(host: str, dns_timeout: float | None = None) -> None:
    """内网/保留地址不允许做代理主机：登录用户否则能拿后端向任意内网
    地址建 TCP 连接做端口探测、投递握手字节。只接受公网地址。
    注意：只在新建/修改代理时校验；此前已存的旧代理不受影响。
    域名要解析后复核（U-1）：只校验字面量会被 lvh.me、*.nip.io、
    *.sslip.io 这类解析到内网的域名绕过。"""
    h = host.rstrip(".").lower()
    if h == "localhost" or h.endswith(_BLOCKED_HOST_SUFFIXES):
        raise ProxyConfigError("代理主机不能是 localhost 等内网域名，请使用公网代理地址")
    if "%" in h or any(ord(c) < 32 or c.isspace() for c in h):
        raise ProxyConfigError("代理主机名格式无效")
    ip = _parse_ip_literal(h)
    if ip is not None:
        if not ip.is_global:
            raise ProxyConfigError(f"代理主机 {h} 是内网/保留地址，不允许作为代理，请使用公网代理地址")
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not embedded.is_global:
            raise ProxyConfigError(
                f"代理主机 {h} 内嵌的 IPv4 地址 {embedded} 是内网/保留地址，不允许作为代理"
            )
        return
    for resolved in _resolve_host_ips(h, timeout=dns_timeout):
        try:
            rip = ipaddress.ip_address(resolved)
        except ValueError:
            raise ProxyConfigError(f"代理主机 {host} 解析结果无效") from None
        if not rip.is_global:
            raise ProxyConfigError(
                f"代理主机 {h} 解析到内网/保留地址 {resolved}，不允许作为代理，请使用公网代理地址"
            )
        # V-1：解析结果同样复核 NAT64 内嵌 IPv4（只判 is_global 会
        # 放行 64:ff9b::a9fe:a9fe 这类地址，它的 is_global 是 True）。
        embedded = _embedded_ipv4(rip)
        if embedded is not None and not embedded.is_global:
            raise ProxyConfigError(
                f"代理主机 {h} 解析到内嵌内网地址的 NAT64 地址 {resolved}，不允许作为代理"
            )


def recheck_proxy_url(url: str | None) -> str | None:
    """使用时重校验已存代理（V-2）：保存后 DNS 可能被改回内网（重绑定），
    只在保存时校验堵不住。每次取用已存代理都重新解析主机名，要求
    全部解析结果为公网地址；不通过抛 ProxyConfigError，不静默回落直连。
    返回原 URL（不改写）；未配置返回 None。"""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except ValueError:
        raise ProxyConfigError("已存代理地址格式无效，请重新保存") from None
    if not hostname:
        raise ProxyConfigError("已存代理地址缺少主机名，请重新保存")
    _check_proxy_host_allowed(hostname, dns_timeout=_USE_TIME_DNS_TIMEOUT_SECONDS)
    return url


def parse_proxy_url(value: str | None) -> str | None:
    """校验并规范化代理地址；空/None 表示直连，返回 None。
    返回规范化后的地址：scheme 统一小写，按解析结果重组（丢弃无意义的
    path/query/fragment）。"""
    if value is None:
        return None
    url = value.strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        # U-3：代理密码含 [ 或 ] 等字符时 urlparse 直接抛 ValueError，
        # 不接住会变成 500；转成 ProxyConfigError 走 400。
        raise ProxyConfigError("代理地址格式无效") from None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        raise ProxyConfigError("代理只支持 http://、https://、socks5://、socks5h://")
    try:
        hostname = parsed.hostname
    except ValueError:
        raise ProxyConfigError("代理地址格式无效") from None
    if not hostname:
        raise ProxyConfigError("代理地址缺少主机名，例如 http://proxy.example.com:8080")
    try:
        port = parsed.port
    except ValueError:
        # urlparse 对越界端口直接抛 ValueError，不返回数值
        raise ProxyConfigError("代理端口无效（1-65535）") from None
    if port is not None and not 1 <= port <= 65535:
        raise ProxyConfigError("代理端口无效（1-65535）")
    _check_proxy_host_allowed(hostname)
    # 按解析结果重组：scheme 统一小写（大写 SOCKS5H:// 否则会绕过
    # botocore 补丁的大小写敏感检查，畸形 URL 还会让代理密码明文进报错）。
    auth = ""
    if parsed.username or parsed.password is not None:
        auth = parsed.username or ""
        if parsed.password is not None:
            auth += f":{parsed.password}"
        auth += "@"
    host = hostname
    if ":" in host:  # IPv6 字面量补回方括号
        host = f"[{host}]"
    netloc = f"{auth}{host}"
    if port is not None:
        netloc += f":{port}"
    return urlunparse((scheme, netloc, "", "", "", ""))


_PROXY_AUTH_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/\s]+)@")


def sanitize_error_message(text: str | None) -> str:
    """把异常串里可能夹带的代理 URL 认证信息脱敏（U-2）。

    urllib3/botocore 的异常串可能原样带上含密码的完整代理 URL
    （如 LocationParseError），直接回显会让密码进接口返回。
    认证段用贪婪匹配到最后一个 @（V-3）：密码本身含 @ 时
    （如 user:p@ss@host），非贪婪会在第一个 @ 截断、尾部明文残留。
    只遮认证段，其余内容不动；无认证信息的 URL 不受影响。"""
    if not text:
        return ""
    return _PROXY_AUTH_RE.sub(r"\1***:***@", text)


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


def _normalize_scheme(url: str) -> str:
    """仅把 scheme 小写。兼容旧版本存下的大写代理地址（如 SOCKS5H://），
    避免它绕过 botocore 补丁的大小写敏感检查、让密码明文进报错。"""
    scheme, sep, rest = url.partition("://")
    if sep and scheme.lower() in ("http", "https", "socks5", "socks5h"):
        return f"{scheme.lower()}://{rest}"
    return url


def proxy_config(proxy_url: str | None) -> Config:
    """Return an explicit per-credential proxy config, including an explicit direct route."""
    if not proxy_url:
        # An explicit empty per-scheme value keeps transport behavior independent
        # of process proxy variables; a missing proxy means direct connection.
        return Config(proxies={"http": None, "https": None})
    proxy_url = _normalize_scheme(proxy_url)
    if urlparse(proxy_url).scheme in ("socks5", "socks5h") and not _SOCKS_PATCH_APPLIED:
        raise ProxyConfigError("当前 botocore 版本不支持 SOCKS 代理，请改用 http(s) 代理或检查 boto3 版本")
    return Config(proxies={"http": proxy_url, "https": proxy_url})
