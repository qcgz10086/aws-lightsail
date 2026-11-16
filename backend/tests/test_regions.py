"""区域启用（Account Management API）的测试。

覆盖：account 客户端固定 us-east-1 且代理透传、状态查询与错误映射、
启用幂等（已启用/进行中/Conflict）、路由级 status/enable 接口。
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

from botocore.exceptions import BotoCoreError, ClientError

from app.services.account import AccountError, enable_region, get_region_opt_status, make_account_client

PROXY = "http://alice:s3cret@proxy.example.com:8080"


def _client_error(code: str, op: str = "GetRegionOptStatus") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} message"}}, op)


class TestMakeAccountClient(unittest.TestCase):
    def test_fixed_us_east_1_and_proxy_passthrough(self):
        with patch("app.services.account.boto3.client") as mk:
            make_account_client("AKID", "SK", PROXY)
            args, kwargs = mk.call_args
            self.assertEqual(args[0], "account")
            self.assertEqual(kwargs["region_name"], "us-east-1")
            self.assertEqual(kwargs["config"].proxies, {"http": PROXY, "https": PROXY})
        with patch("app.services.account.boto3.client") as mk:
            make_account_client("AKID", "SK")
            _, kwargs = mk.call_args
            self.assertIsNone(kwargs["config"])


class TestGetRegionOptStatus(unittest.TestCase):
    def _mock_client(self, **behaviors):
        client = Mock()
        for name, value in behaviors.items():
            attr = getattr(client, name)
            if isinstance(value, Exception):
                attr.side_effect = value
            else:
                attr.return_value = value
        return client

    def test_returns_status(self):
        for status in ("ENABLED", "ENABLING", "DISABLED", "ENABLED_BY_DEFAULT"):
            client = self._mock_client(
                get_region_opt_status={"RegionOptStatus": status}
            )
            with patch("app.services.account.make_account_client", return_value=client):
                self.assertEqual(get_region_opt_status("AKID", "SK", "ap-east-1"), status)

    def test_access_denied_friendly(self):
        client = self._mock_client(
            get_region_opt_status=_client_error("AccessDeniedException")
        )
        with patch("app.services.account.make_account_client", return_value=client):
            with self.assertRaises(AccountError) as ctx:
                get_region_opt_status("AKID", "SK", "ap-east-1")
            self.assertIn("account:EnableRegion", ctx.exception.message)
            self.assertEqual(ctx.exception.code, "AccessDeniedException")

    def test_invalid_credentials_friendly(self):
        client = self._mock_client(
            get_region_opt_status=_client_error("UnrecognizedClientException")
        )
        with patch("app.services.account.make_account_client", return_value=client):
            with self.assertRaises(AccountError) as ctx:
                get_region_opt_status("AKID", "SK", "ap-east-1")
            self.assertIn("凭证无效", ctx.exception.message)

    def test_connection_error(self):
        client = self._mock_client(get_region_opt_status=BotoCoreError())
        with patch("app.services.account.make_account_client", return_value=client):
            with self.assertRaises(AccountError) as ctx:
                get_region_opt_status("AKID", "SK", "ap-east-1")
            self.assertIn("连接错误", ctx.exception.message)


class TestEnableRegion(unittest.TestCase):
    def test_already_enabled_returns_without_calling(self):
        client = Mock()
        with patch(
            "app.services.account.get_region_opt_status", return_value="ENABLED"
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLED")
            client.enable_region.assert_not_called()

    def test_enabling_returns_enabling(self):
        client = Mock()
        with patch(
            "app.services.account.get_region_opt_status", return_value="ENABLING"
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLING")
            client.enable_region.assert_not_called()

    def test_disabled_enables_and_confirms(self):
        client = Mock()
        with patch(
            "app.services.account.get_region_opt_status",
            side_effect=["DISABLED", "ENABLING"],
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLING")
            client.enable_region.assert_called_once_with(RegionName="ap-east-1")

    def test_disabled_enables_to_enabled(self):
        client = Mock()
        with patch(
            "app.services.account.get_region_opt_status",
            side_effect=["DISABLED", "ENABLED"],
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLED")

    def test_conflict_treated_as_enabling(self):
        client = Mock()
        client.enable_region.side_effect = _client_error("ConflictException", "EnableRegion")
        with patch(
            "app.services.account.get_region_opt_status", return_value="DISABLED"
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLING")

    def test_validation_error_rechecks_status(self):
        client = Mock()
        client.enable_region.side_effect = _client_error("ValidationException", "EnableRegion")
        with patch(
            "app.services.account.get_region_opt_status",
            side_effect=["DISABLED", "ENABLED"],
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLED")

    def test_access_denied_friendly(self):
        client = Mock()
        client.enable_region.side_effect = _client_error("AccessDeniedException", "EnableRegion")
        with patch(
            "app.services.account.get_region_opt_status", return_value="DISABLED"
        ), patch("app.services.account.make_account_client", return_value=client):
            with self.assertRaises(AccountError) as ctx:
                enable_region("AKID", "SK", "ap-east-1")
            self.assertIn("account:EnableRegion", ctx.exception.message)

    def test_status_check_denied_then_enable_proceeds(self):
        # 读状态没权限时不直接拦：直接尝试启用。
        client = Mock()
        with patch(
            "app.services.account.get_region_opt_status",
            side_effect=[
                AccountError("权限不足", code="AccessDeniedException"),
                AccountError("权限不足", code="AccessDeniedException"),
            ],
        ), patch("app.services.account.make_account_client", return_value=client):
            self.assertEqual(enable_region("AKID", "SK", "ap-east-1"), "ENABLING")
            client.enable_region.assert_called_once_with(RegionName="ap-east-1")


# ---- 路由级测试：内存库 + TestClient（沿用 test_proxy.py 的搭法）----
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AwsCredential, User  # noqa: E402
from app.security import encrypt_secret, hash_password  # noqa: E402


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

    def _add_credential(self, user_id: int) -> int:
        cred = AwsCredential(
            user_id=user_id,
            access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
            secret_access_key_enc=encrypt_secret("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
            is_default=True,
        )
        self.db.add(cred)
        self.db.commit()
        self.db.refresh(cred)
        return cred.id


class TestRegionEndpoints(_ClientCase):
    def setUp(self):
        super().setUp()
        user = User(username="regalice", password_hash=hash_password("AlicePass123!x"))
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        self.token = self._login("regalice", "AlicePass123!x")
        self.cid = self._add_credential(user.id)

    def test_status_disabled(self):
        with patch(
            "app.routers.credentials.acct.get_region_opt_status", return_value="DISABLED"
        ):
            r = self.client.get(
                f"/api/credentials/{self.cid}/regions/ap-east-1/status",
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), {"region": "ap-east-1", "status": "DISABLED", "message": None})

    def test_status_unknown_on_account_error(self):
        with patch(
            "app.routers.credentials.acct.get_region_opt_status",
            side_effect=AccountError("AWS 凭证无效：请检查", code="UnrecognizedClientException"),
        ):
            r = self.client.get(
                f"/api/credentials/{self.cid}/regions/ap-east-1/status",
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "UNKNOWN")
            self.assertIn("凭证无效", r.json()["message"])

    def test_status_404(self):
        r = self.client.get(
            "/api/credentials/9999/regions/ap-east-1/status", headers=self._auth(self.token)
        )
        self.assertEqual(r.status_code, 404, r.text)

    def test_status_bad_region(self):
        for bad in ("not-a-region", "us_east_1", "AP-EAST-1"):
            r = self.client.get(
                f"/api/credentials/{self.cid}/regions/{bad}/status",
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 400, bad)

    def test_enable_enabling(self):
        with patch(
            "app.routers.credentials.acct.enable_region", return_value="ENABLING"
        ):
            r = self.client.post(
                f"/api/credentials/{self.cid}/regions/ap-east-1/enable",
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "ENABLING")

    def test_enable_enabled(self):
        with patch(
            "app.routers.credentials.acct.enable_region", return_value="ENABLED"
        ):
            r = self.client.post(
                f"/api/credentials/{self.cid}/regions/ap-east-1/enable",
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()["status"], "ENABLED")

    def test_enable_access_denied_400(self):
        with patch(
            "app.routers.credentials.acct.enable_region",
            side_effect=AccountError("权限不足：密钥需要", code="AccessDeniedException"),
        ):
            r = self.client.post(
                f"/api/credentials/{self.cid}/regions/ap-east-1/enable",
                headers=self._auth(self.token),
            )
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("权限不足", r.json()["detail"])

    def test_enable_404_and_bad_region(self):
        r = self.client.post(
            "/api/credentials/9999/regions/ap-east-1/enable", headers=self._auth(self.token)
        )
        self.assertEqual(r.status_code, 404, r.text)
        r = self.client.post(
            f"/api/credentials/{self.cid}/regions/bad_region/enable",
            headers=self._auth(self.token),
        )
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
