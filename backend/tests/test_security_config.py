"""H-1/H-2/H-3 修复测试：配置 fail-fast、实例默认值与密码强度。

不调真实 AWS；boto3 客户端全部用 unittest.mock 模拟。
运行：backend/.venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from unittest.mock import Mock, patch

# H-1/H-2 fail-fast：配置为必填，测试须先设置合法的测试值再导入 app 代码。
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError

from app.config import Settings
from app.schemas import InstanceCreate
from app.services import lightsail as ls

VALID = {
    "secret_key": "test-only-secret-key-0123456789abcdef",
    "encryption_key": "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=",
    "admin_password": "TestAdminPass123!",
}


def _settings(**overrides):
    kw = dict(VALID)
    kw.update(overrides)
    return Settings(_env_file=None, **kw)


class TestConfigFailFast(unittest.TestCase):
    def test_missing_all_three_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValidationError) as ctx:
                Settings(_env_file=None)
        fields = {err["loc"][0] for err in ctx.exception.errors()}
        self.assertEqual(
            fields, {"secret_key", "encryption_key", "admin_password"}
        )

    def test_public_default_secret_key_rejected(self):
        for bad in ("dev-insecure-secret-change-me", "change-me-to-a-long-random-string"):
            with self.assertRaises(ValidationError, msg=bad):
                _settings(secret_key=bad)

    def test_short_secret_key_rejected(self):
        with self.assertRaises(ValidationError):
            _settings(secret_key="too-short")

    def test_invalid_fernet_key_rejected(self):
        with self.assertRaises(ValidationError):
            _settings(encryption_key="not-a-fernet-key")

    def test_empty_encryption_key_rejected(self):
        with self.assertRaises(ValidationError):
            _settings(encryption_key="")

    def test_default_admin_password_rejected(self):
        with self.assertRaises(ValidationError):
            _settings(admin_password="admin123")

    def test_valid_settings_accepted(self):
        s = _settings()
        self.assertEqual(s.admin_username, "admin")
        self.assertEqual(s.admin_password, "TestAdminPass123!")


class TestInstanceDefaults(unittest.TestCase):
    def _base(self, **overrides):
        kw = {
            "region": "us-east-1",
            "instance_name": "test-1",
            "blueprint_id": "ubuntu_22_04",
            "bundle_id": "nano_3_0",
        }
        kw.update(overrides)
        return InstanceCreate(**kw)

    def test_open_all_ports_defaults_to_false(self):
        body = self._base()
        self.assertIs(body.open_all_ports, False)

    def test_password_defaults_to_none(self):
        body = self._base()
        self.assertIsNone(body.password)


class TestInstancePasswordStrength(unittest.TestCase):
    def test_short_password_rejected(self):
        with self.assertRaises(ls.LightsailError):
            ls._validate_instance_password("Short1!", windows=False)

    def test_missing_uppercase_rejected(self):
        with self.assertRaises(ls.LightsailError):
            ls._validate_instance_password("abcdefgh123!", windows=False)

    def test_missing_lowercase_rejected(self):
        with self.assertRaises(ls.LightsailError):
            ls._validate_instance_password("ABCDEFGH123!", windows=False)

    def test_missing_digit_rejected(self):
        with self.assertRaises(ls.LightsailError):
            ls._validate_instance_password("Abcdefghijk!", windows=False)

    def test_missing_symbol_rejected(self):
        with self.assertRaises(ls.LightsailError):
            ls._validate_instance_password("Abcdefgh1234", windows=False)

    def test_strong_password_accepted(self):
        # 不抛异常即通过；同时确认仅在填密码时才生成开启密码登录的 userData
        ls._validate_instance_password("Abcdefgh123!", windows=False)
        user_data = ls.build_password_user_data("Abcdefgh123!")
        self.assertIn("PasswordAuthentication yes", user_data)


class TestCreateInstanceNoPassword(unittest.TestCase):
    def _mock_client(self):
        client = Mock()
        client.create_instances.return_value = {}
        return client

    def test_no_password_no_user_data_and_no_open_ports(self):
        client = self._mock_client()
        with (
            patch.object(ls, "make_client", return_value=client),
            patch.object(ls, "_wait_instance_not_pending", return_value=None),
        ):
            result = ls.create_instance(
                "AKID", "SECRET", "us-east-1", "test-1",
                "ubuntu_22_04", "nano_3_0",
                availability_zone="us-east-1a",
                allocate_static_ip=False,
                password=None,
            )
        _, kwargs = client.create_instances.call_args
        self.assertNotIn("userData", kwargs)
        client.put_instance_public_ports.assert_not_called()
        self.assertEqual(result["name"], "test-1")

    def test_weak_password_rejected_before_aws_call(self):
        client = self._mock_client()
        with (
            patch.object(ls, "make_client", return_value=client),
            patch.object(ls, "_wait_instance_not_pending", return_value=None),
        ):
            with self.assertRaises(ls.LightsailError):
                ls.create_instance(
                    "AKID", "SECRET", "us-east-1", "test-1",
                    "ubuntu_22_04", "nano_3_0",
                    availability_zone="us-east-1a",
                    allocate_static_ip=False,
                    password="weakpass1",
                )
        client.create_instances.assert_not_called()

    def test_strong_password_and_explicit_open_ports(self):
        client = self._mock_client()
        with (
            patch.object(ls, "make_client", return_value=client),
            patch.object(ls, "_wait_instance_not_pending", return_value=None),
        ):
            result = ls.create_instance(
                "AKID", "SECRET", "us-east-1", "test-1",
                "ubuntu_22_04", "nano_3_0",
                availability_zone="us-east-1a",
                allocate_static_ip=False,
                password="Abcdefgh123!",
                open_all_ports=True,
            )
        _, kwargs = client.create_instances.call_args
        self.assertIn("userData", kwargs)
        client.put_instance_public_ports.assert_called_once()
        self.assertIn("已开放全部防火墙端口", result["message"])


class TestCreateInstanceHttp400(unittest.TestCase):
    """走真实路由：弱密码应返回 400，默认不开放全部端口。"""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.database import Base
        from app.models import AwsCredential, User
        from app.security import encrypt_secret, hash_password

        # 路由把建实例放到线程池里跑：用 StaticPool + check_same_thread=False，
        # 让内存库在各线程间共用同一连接，否则会报 SQLite 跨线程错误。
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        cls._session = sessionmaker(bind=engine)()
        cls.user = User(username="tester", password_hash=hash_password("x"), is_admin=False)
        cls._session.add(cls.user)
        cls._session.commit()
        cls._session.refresh(cls.user)
        cred = AwsCredential(
            user_id=cls.user.id,
            access_key_id_enc=encrypt_secret("AKIDEXAMPLE"),
            secret_access_key_enc=encrypt_secret("secret-example"),
            is_default=True,
        )
        cls._session.add(cred)
        cls._session.commit()

    @classmethod
    def tearDownClass(cls):
        cls._session.close()

    def setUp(self):
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.deps import get_current_user
        from app.main import app

        app.dependency_overrides[get_db] = lambda: self._session
        app.dependency_overrides[get_current_user] = lambda: self.user
        self._app = app
        self.client = TestClient(app)

    def tearDown(self):
        self._app.dependency_overrides.clear()

    def _post(self, password):
        body = {
            "region": "us-east-1",
            "instance_name": "test-1",
            "blueprint_id": "ubuntu_22_04",
            "bundle_id": "nano_3_0",
            "availability_zone": "us-east-1a",
            "allocate_static_ip": False,
            "password": password,
        }
        from task_fakes import FakeLightsail
        client = FakeLightsail(name=None)
        with client.installed():
            response = self.client.post("/api/instances", json=body)
            if response.status_code == 202:
                import time
                deadline = time.monotonic() + 5
                state = response.json()
                while state['status'] in {'queued', 'running'} and time.monotonic() < deadline:
                    time.sleep(0.02)
                    final = self.client.get('/api/v2/tasks/' + state['task_id'])
                    self.assertEqual(final.status_code, 200, final.text)
                    state = final.json()
                self.assertEqual(state['status'], 'succeeded', state)
            return response, client

    def test_weak_password_returns_400(self):
        resp, client = self._post("weakpass1")
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(client.count("CreateInstances"), 0)

    def test_short_password_returns_400(self):
        resp, _ = self._post("Ab1!")
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_strong_password_completes_and_ports_closed_by_default(self):
        resp, client = self._post("Abcdefgh123!")
        self.assertIn(resp.status_code, (200, 202), resp.text)
        self.assertEqual(client.count("PutInstancePublicPorts"), 0)
        self.assertNotIn("已开放全部防火墙端口", resp.json()["message"])


if __name__ == "__main__":
    unittest.main()
