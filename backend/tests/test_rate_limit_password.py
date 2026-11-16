"""R-1/R-2/R-4 修复测试：启动密钥强度、登录/改密限流、用户密码强度。

不调真实 AWS。运行：
    backend/.venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest

# R-1 fail-fast：配置为必填，测试须先设置合法的测试值再导入 app 代码。
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pydantic import ValidationError

from app.config import Settings
from app.password_strength import validate_secret_key, validate_strong_password
from app.rate_limit import login_limiter

VALID = {
    "secret_key": "test-only-secret-key-0123456789abcdef",
    "encryption_key": "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=",
    "admin_password": "TestAdminPass123!",
}


def _settings(**overrides):
    kw = dict(VALID)
    kw.update(overrides)
    return Settings(_env_file=None, **kw)


def _make_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base

    # 路由可能在线程池里跑：StaticPool + check_same_thread=False，
    # 让内存库在各线程间共用同一连接。
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestConfigStrength(unittest.TestCase):
    """R-1：弱 ADMIN_PASSWORD / 弱 SECRET_KEY 启动时必须被拒绝。"""

    def test_weak_admin_passwords_rejected(self):
        for bad in (
            "x",  # 审计实测通过的弱值
            "123456",
            "password123",  # 无大写、无符号
            "TestAdminPass123",  # 无符号
            "testadminpass123!",  # 无大写
            "TESTADMINPASS123!",  # 无小写
            "A1!" + "b" * 62,  # 65 位，超长
        ):
            with self.assertRaises(ValidationError, msg=bad):
                _settings(admin_password=bad)

    def test_strong_admin_password_accepted(self):
        s = _settings(admin_password="A1!bcdefghij")
        self.assertEqual(s.admin_password, "A1!bcdefghij")

    def test_low_entropy_secret_keys_rejected(self):
        for bad in (
            "a" * 32,  # 审计实测通过的弱值：单一字符重复
            "abcd" * 8,  # 只有小写一类
            "Aa1!" * 8,  # 32 位、4 类，但只有 4 个不同字符
            "Ab1!" * 7 + "Ab1",  # 31 位
        ):
            with self.assertRaises(ValidationError, msg=bad):
                _settings(secret_key=bad)

    def test_strong_secret_key_accepted(self):
        s = _settings(secret_key="K7$mQ2@xP9!vL4&nR8#wT1*zY5%qA3^b")
        self.assertEqual(s.secret_key, "K7$mQ2@xP9!vL4&nR8#wT1*zY5%qA3^b")


class TestSharedPasswordValidator(unittest.TestCase):
    """R-4：共享校验与实例密码同一标准。"""

    def test_weak_rejected(self):
        for bad in ("123456", "weakpass1", "Abcdefgh1234", "ABCDEFGH123!"):
            with self.assertRaises(ValueError, msg=bad):
                validate_strong_password(bad)

    def test_strong_accepted(self):
        validate_strong_password("Abcdefgh123!")

    def test_secret_key_entropy(self):
        with self.assertRaises(ValueError):
            validate_secret_key("a" * 32)
        validate_secret_key("K7$mQ2@xP9!vL4&nR8#wT1*zY5%qA3^b")


class _HttpBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.models import User
        from app.security import hash_password

        cls._session = _make_session()
        cls.admin = User(username="admin", password_hash=hash_password("AdminPass123!"), is_admin=True)
        cls.bob = User(username="bob", password_hash=hash_password("BobPass123!"), is_admin=False)
        cls._session.add(cls.admin)
        cls._session.add(cls.bob)
        cls._session.commit()
        cls._session.refresh(cls.admin)
        cls._session.refresh(cls.bob)

    @classmethod
    def tearDownClass(cls):
        cls._session.close()

    def _client(self, user=None):
        from fastapi.testclient import TestClient

        from app.database import get_db
        from app.deps import get_current_user
        from app.main import app

        app.dependency_overrides[get_db] = lambda: self._session
        if user is not None:
            app.dependency_overrides[get_current_user] = lambda: user
        self._app = app
        return TestClient(app)

    def tearDown(self):
        self._app.dependency_overrides.clear()
        login_limiter.reset()


class TestUserPasswordHttp(_HttpBase):
    """R-4：建用户/改密的弱密码走真实路由必须返回 400。"""

    def test_create_user_weak_password_returns_400(self):
        client = self._client(user=self.admin)
        for bad in ("123456", "weakpass1"):
            resp = client.post("/api/users", json={"username": f"u_{bad}", "password": bad})
            self.assertEqual(resp.status_code, 400, resp.text)

    def test_create_user_strong_password_returns_201(self):
        client = self._client(user=self.admin)
        resp = client.post(
            "/api/users", json={"username": "u_strong", "password": "StrongPass123!"}
        )
        self.assertEqual(resp.status_code, 201, resp.text)

    def test_update_user_weak_password_returns_400(self):
        client = self._client(user=self.admin)
        resp = client.patch(f"/api/users/{self.bob.id}", json={"password": "123456"})
        self.assertEqual(resp.status_code, 400, resp.text)
        resp = client.patch(f"/api/users/{self.bob.id}", json={"password": "NewStrong123!"})
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_change_password_weak_new_returns_400(self):
        # 用独立用户，避免与其他测试互相改掉密码。
        from app.models import User
        from app.security import hash_password

        carol = User(username="carol", password_hash=hash_password("CarolPass123!"), is_admin=False)
        self._session.add(carol)
        self._session.commit()
        self._session.refresh(carol)
        client = self._client(user=carol)
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "CarolPass123!", "new_password": "123456"},
        )
        self.assertEqual(resp.status_code, 400, resp.text)
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "CarolPass123!", "new_password": "NewStrong123!"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)


class TestLoginRateLimit(_HttpBase):
    """R-2：同一 IP+用户名连续失败 5 次后返回 429，锁定后放行；成功清零。"""

    def setUp(self):
        super().setUp()
        # 测试用短锁定，避免等 15 分钟；窗口保持足够大。
        self._orig = (
            login_limiter.max_failures,
            login_limiter.window_seconds,
            login_limiter.lock_seconds,
        )
        login_limiter.window_seconds = 120
        login_limiter.lock_seconds = 2
        login_limiter.reset()

    def tearDown(self):
        login_limiter.max_failures, login_limiter.window_seconds, login_limiter.lock_seconds = self._orig
        login_limiter.reset()
        super().tearDown()

    def _login(self, client, username, password):
        return client.post("/api/auth/login", json={"username": username, "password": password})

    def test_five_failures_then_429_and_unlock(self):
        client = self._client()
        for _ in range(5):
            resp = self._login(client, "bob", "WrongPass123!")
            self.assertEqual(resp.status_code, 401, resp.text)
        resp = self._login(client, "bob", "WrongPass123!")
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertIn("Retry-After", resp.headers)
        # 锁定期间正确密码也进不去。
        resp = self._login(client, "bob", "BobPass123!")
        self.assertEqual(resp.status_code, 429, resp.text)
        # 锁定过期后放行。
        time.sleep(2.5)
        resp = self._login(client, "bob", "BobPass123!")
        self.assertEqual(resp.status_code, 200, resp.text)

    def test_success_resets_failure_count(self):
        client = self._client()
        for _ in range(4):
            self.assertEqual(self._login(client, "bob", "WrongPass123!").status_code, 401)
        self.assertEqual(self._login(client, "bob", "BobPass123!").status_code, 200)
        # 成功后计数清零：再错一次只是 401，不会 429。
        self.assertEqual(self._login(client, "bob", "WrongPass123!").status_code, 401)

    def test_change_password_rate_limit(self):
        client = self._client(user=self.bob)
        for _ in range(5):
            resp = client.post(
                "/api/auth/change-password",
                json={"old_password": "WrongOld123!", "new_password": "NewStrong123!"},
            )
            self.assertEqual(resp.status_code, 400, resp.text)
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "WrongOld123!", "new_password": "NewStrong123!"},
        )
        self.assertEqual(resp.status_code, 429, resp.text)
        self.assertIn("Retry-After", resp.headers)
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "BobPass123!", "new_password": "NewStrong123!"},
        )
        self.assertEqual(resp.status_code, 429, resp.text)


if __name__ == "__main__":
    unittest.main()
