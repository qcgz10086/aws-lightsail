"""每组 AWS 凭证单独代理的测试。

覆盖：代理地址校验/脱敏、boto3 工厂透传、service 函数透传、
凭证路由的存取（新建带代理、非法代理 400、改代理、清代理、保持不变）。
不调真实 AWS。运行：
    backend/.venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest.mock import Mock, patch

# fail-fast：配置为必填，测试须先设置合法的测试值再导入 app 代码。
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.proxy_config import ProxyConfigError, mask_proxy_url, parse_proxy_url, proxy_config
from app.security import decrypt_secret, encrypt_secret
from app.services import billing as b
from app.services import lightsail as ls
from app.services.traffic import get_credential_proxy_url

PROXY = "http://alice:s3cret@proxy.example.com:8080"
PROXY_MASKED = "http://alice:***@proxy.example.com:8080"


class TestParseProxyUrl(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_proxy_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(
            parse_proxy_url("https://proxy.example.com:3128"), "https://proxy.example.com:3128"
        )
        self.assertEqual(parse_proxy_url(PROXY), PROXY)
        self.assertEqual(parse_proxy_url("http://proxy.example.com"), "http://proxy.example.com")
        self.assertEqual(parse_proxy_url("socks5://127.0.0.1:1080"), "socks5://127.0.0.1:1080")
        self.assertEqual(
            parse_proxy_url("socks5h://alice:s3cret@proxy.example.com:1080"),
            "socks5h://alice:s3cret@proxy.example.com:1080",
        )
        self.assertEqual(parse_proxy_url("  http://127.0.0.1:8080  "), "http://127.0.0.1:8080")

    def test_empty_means_direct(self):
        self.assertIsNone(parse_proxy_url(None))
        self.assertIsNone(parse_proxy_url(""))
        self.assertIsNone(parse_proxy_url("   "))

    def test_rejects_unsupported(self):
        for bad in (
            "ftp://proxy.example.com:21",
            "proxy.example.com:8080",
            "http://",
            "http://:8080",
            "http://127.0.0.1:0",
            "http://127.0.0.1:99999",
            "socks4://127.0.0.1:1080",
        ):
            with self.assertRaises(ProxyConfigError, msg=bad):
                parse_proxy_url(bad)


class TestMaskProxyUrl(unittest.TestCase):
    def test_masks_password_only(self):
        self.assertEqual(mask_proxy_url(PROXY), PROXY_MASKED)
        self.assertEqual(
            mask_proxy_url("socks5h://alice:s3cret@proxy.example.com:1080"),
            "socks5h://alice:***@proxy.example.com:1080",
        )
        self.assertEqual(mask_proxy_url("http://127.0.0.1:8080"), "http://127.0.0.1:8080")
        self.assertEqual(mask_proxy_url("https://proxy.example.com"), "https://proxy.example.com")

    def test_none(self):
        self.assertIsNone(mask_proxy_url(None))
        self.assertIsNone(mask_proxy_url(""))


class TestProxyConfig(unittest.TestCase):
    def test_none_means_direct(self):
        self.assertIsNone(proxy_config(None))
        self.assertIsNone(proxy_config(""))

    def test_proxies_dict(self):
        cfg = proxy_config(PROXY)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.proxies, {"http": PROXY, "https": PROXY})
        # SOCKS5 同样原样透传给 botocore（urllib3 靠 PySocks 走 SOCKS）
        cfg = proxy_config("socks5h://127.0.0.1:1080")
        self.assertEqual(
            cfg.proxies, {"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"}
        )


class TestBotocoreSocksPatch(unittest.TestCase):
    """botocore 1.44.3 会把 socks5:// 前面拼上 http://；补丁只放行 socks 协议。"""

    def _cfg(self, url):
        from botocore.httpsession import ProxyConfiguration

        return ProxyConfiguration(proxies={"http": url, "https": url})

    def test_socks_url_passes_through_unchanged(self):
        for url in ("socks5h://127.0.0.1:1080", "socks5://127.0.0.1:1080"):
            self.assertEqual(
                self._cfg(url).proxy_url_for("https://lightsail.us-east-1.amazonaws.com"),
                url,
            )

    def test_http_behavior_unchanged(self):
        self.assertEqual(
            self._cfg("http://proxy:8080").proxy_url_for("https://lightsail.us-east-1.amazonaws.com"),
            "http://proxy:8080",
        )
        # 原有行为保留：没写协议的地址仍补 http://
        self.assertEqual(
            self._cfg("proxy:8080").proxy_url_for("https://lightsail.us-east-1.amazonaws.com"),
            "http://proxy:8080",
        )

    def test_socks_manager_uses_socks_pools(self):
        """_get_proxy_manager 对 socks 建 SOCKSProxyManager，且不被 AWS 池覆盖。"""
        from botocore.httpsession import URLLib3Session
        from urllib3 import ProxyManager
        from urllib3.contrib.socks import (
            SOCKSHTTPConnectionPool,
            SOCKSHTTPSConnectionPool,
            SOCKSProxyManager,
        )

        session = URLLib3Session(
            proxies={"http": "socks5h://127.0.0.1:1080", "https": "socks5h://127.0.0.1:1080"}
        )
        mgr = session._get_proxy_manager("socks5h://127.0.0.1:1080")
        self.assertIsInstance(mgr, SOCKSProxyManager)
        self.assertIs(mgr.pool_classes_by_scheme["http"], SOCKSHTTPConnectionPool)
        self.assertIs(mgr.pool_classes_by_scheme["https"], SOCKSHTTPSConnectionPool)

        http_session = URLLib3Session(
            proxies={"http": "http://proxy:8080", "https": "http://proxy:8080"}
        )
        http_mgr = http_session._get_proxy_manager("http://proxy:8080")
        self.assertIsInstance(http_mgr, ProxyManager)
        self.assertNotIsInstance(http_mgr, SOCKSProxyManager)


class TestFactoriesPassProxy(unittest.TestCase):
    def test_lightsail_factory(self):
        with patch("app.services.lightsail.boto3.client") as mk:
            ls.make_client("AKID", "SK", "us-east-1", PROXY)
            _, kwargs = mk.call_args
            self.assertEqual(kwargs["config"].proxies, {"http": PROXY, "https": PROXY})
        with patch("app.services.lightsail.boto3.client") as mk:
            ls.make_client("AKID", "SK", "us-east-1")
            _, kwargs = mk.call_args
            self.assertIsNone(kwargs["config"])
        with patch("app.services.lightsail.boto3.client") as mk:
            ls.make_service_quotas_client("AKID", "SK", "us-east-1", PROXY)
            _, kwargs = mk.call_args
            self.assertEqual(kwargs["config"].proxies, {"http": PROXY, "https": PROXY})

    def test_billing_factories(self):
        for fn in (b.make_sts_client, b.make_billing_client, b.make_ce_client):
            with patch("app.services.billing.boto3.client") as mk:
                fn("AKID", "SK", PROXY)
                _, kwargs = mk.call_args
                self.assertEqual(kwargs["config"].proxies, {"http": PROXY, "https": PROXY})
            with patch("app.services.billing.boto3.client") as mk:
                fn("AKID", "SK")
                _, kwargs = mk.call_args
                self.assertIsNone(kwargs["config"])


class TestServiceForwarding(unittest.TestCase):
    def test_validate_credentials_forwards_proxy(self):
        with patch("app.services.lightsail.make_client") as mk:
            mk.return_value.get_regions.return_value = {"regions": []}
            ls.validate_credentials("AKID12345678901234", "SK", PROXY)
            args, kwargs = mk.call_args
            passed = kwargs.get("proxy_url") if "proxy_url" in kwargs else (args[3] if len(args) > 3 else None)
            self.assertEqual(passed, PROXY)

    def test_get_aws_credits_forwards_proxy(self):
        sts = Mock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        billing = Mock()
        billing.get_credits.return_value = {"credits": []}
        with patch("app.services.billing.make_sts_client", return_value=sts) as mk_sts, patch(
            "app.services.billing.make_billing_client", return_value=billing
        ) as mk_billing:
            res = b.get_aws_credits("AKID", "SK", proxy_url=PROXY)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["account_id"], "123456789012")
            self.assertEqual(mk_sts.call_args[0][2], PROXY)
            self.assertEqual(mk_billing.call_args[0][2], PROXY)


class TestCredentialProxyStorage(unittest.TestCase):
    def test_encrypted_roundtrip_and_no_plaintext(self):
        from types import SimpleNamespace

        cred = SimpleNamespace(proxy_url_enc=encrypt_secret(PROXY))
        self.assertEqual(get_credential_proxy_url(cred), PROXY)
        # 密文里没有代理主机名明文
        self.assertNotIn("proxy.example.com", cred.proxy_url_enc)
        self.assertIsNone(get_credential_proxy_url(SimpleNamespace(proxy_url_enc=None)))


# ---- 路由级测试：内存库 + TestClient（沿用 test_remaining_fixes 的搭法）----
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AwsCredential, User  # noqa: E402
from app.security import hash_password  # noqa: E402


def _make_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _create_user(db, username, password, is_admin=False):
    user = User(username=username, password_hash=hash_password(password), is_admin=is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _quota_result():
    return {
        "vcpu_quota": None,
        "vcpu_tier": None,
        "static_ip_quota": None,
        "used_vcpu": None,
        "used_instance_count": None,
        "region": "us-east-1",
        "message": None,
        "error": None,
    }


class _ClientCase(unittest.TestCase):
    def setUp(self):
        self.db = _make_session()
        self._orig = dict(app.dependency_overrides)
        session = self.db

        def _override():
            try:
                yield session
            finally:
                pass

        app.dependency_overrides[get_db] = _override
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self._orig)
        self.db.close()

    def _login(self, username, password):
        r = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["access_token"]

    def _auth(self, token):
        return {"Authorization": f"Bearer {token}"}


class TestCredentialProxyApi(_ClientCase):
    def setUp(self):
        super().setUp()
        _create_user(self.db, "proxyalice", "AlicePass123!x")
        self.token = self._login("proxyalice", "AlicePass123!x")
        self._validate_patcher = patch(
            "app.routers.credentials.ls.validate_credentials", return_value=None
        )
        self._quota_patcher = patch(
            "app.routers.credentials.ls.get_lightsail_account_quotas",
            return_value=_quota_result(),
        )
        self._validate_patcher.start()
        self._quota_patcher.start()

    def tearDown(self):
        self._validate_patcher.stop()
        self._quota_patcher.stop()
        super().tearDown()

    def _create(self, proxy_url=None):
        body = {
            "access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        }
        if proxy_url is not None:
            body["proxy_url"] = proxy_url
        return self.client.post("/api/credentials", json=body, headers=self._auth(self.token))

    def test_create_with_proxy_stores_encrypted_and_masks(self):
        r = self._create(PROXY)
        self.assertEqual(r.status_code, 201, r.text)
        item = r.json()["items"][0]
        self.assertEqual(item["proxy_masked"], PROXY_MASKED)
        # 库里是密文，没有明文主机名
        cred = self.db.query(AwsCredential).first()
        self.assertIsNotNone(cred.proxy_url_enc)
        self.assertNotIn("proxy.example.com", cred.proxy_url_enc)
        self.assertEqual(decrypt_secret(cred.proxy_url_enc), PROXY)

    def test_create_with_bad_proxy_400_and_not_saved(self):
        for bad in ("ftp://x.example.com", "http://", "socks4://127.0.0.1:1080"):
            r = self._create(bad)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("代理", r.json()["detail"])
        self.assertEqual(self.db.query(AwsCredential).count(), 0)

    def test_create_with_socks5_proxy(self):
        r = self._create("socks5h://alice:s3cret@proxy.example.com:1080")
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(
            r.json()["items"][0]["proxy_masked"], "socks5h://alice:***@proxy.example.com:1080"
        )

    def test_create_without_proxy_is_direct(self):
        r = self._create()
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIsNone(r.json()["items"][0]["proxy_masked"])
        cred = self.db.query(AwsCredential).first()
        self.assertIsNone(cred.proxy_url_enc)

    def test_update_proxy_set_clear_keep(self):
        r = self._create()
        self.assertEqual(r.status_code, 201, r.text)
        cid = r.json()["items"][0]["id"]
        # 改代理
        r = self.client.put(
            f"/api/credentials/{cid}",
            json={"proxy_url": "http://bob:pw@proxy2.example.com:3128"},
            headers=self._auth(self.token),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["items"][0]["proxy_masked"], "http://bob:***@proxy2.example.com:3128"
        )
        # 不带 proxy_url 字段 = 保持不变
        r = self.client.put(
            f"/api/credentials/{cid}",
            json={"account_label": "主账号"},
            headers=self._auth(self.token),
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json()["items"][0]["proxy_masked"], "http://bob:***@proxy2.example.com:3128"
        )
        # 空串 = 清除，恢复直连
        r = self.client.put(
            f"/api/credentials/{cid}", json={"proxy_url": ""}, headers=self._auth(self.token)
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["items"][0]["proxy_masked"])
        self.assertIsNone(self.db.query(AwsCredential).first().proxy_url_enc)

    def test_update_with_unreachable_proxy_fails_and_keeps_old(self):
        r = self._create(PROXY)
        cid = r.json()["items"][0]["id"]
        # 代理换成不通的：校验抛错，400，旧代理保留
        from app.services.lightsail import LightsailError

        with patch(
            "app.routers.credentials.ls.validate_credentials",
            side_effect=LightsailError("代理连接失败"),
        ):
            r = self.client.put(
                f"/api/credentials/{cid}",
                json={"proxy_url": "http://127.0.0.1:9"},
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 400, r.text)
        cred = self.db.query(AwsCredential).first()
        self.assertEqual(decrypt_secret(cred.proxy_url_enc), PROXY)


if __name__ == "__main__":
    unittest.main()
