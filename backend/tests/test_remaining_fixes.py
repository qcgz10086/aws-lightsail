"""R-3/R-5/R-6/R-7/R-8/R-9/R-10 修复测试。


不调真实 AWS。运行：
    backend/.venv/bin/python -m unittest discover -s tests -v
"""
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

# R-1 fail-fast：配置为必填，测试须先设置合法的测试值再导入 app 代码。
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from jose import jwt as jose_jwt

from app.config import Settings
from app.database import Base, get_db
from app.main import app
from app.models import User
from app.security import hash_password

SECRET = "test-only-secret-key-0123456789abcdef"
VALID = {
    "secret_key": SECRET,
    "encryption_key": "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=",
    "admin_password": "TestAdminPass123!",
}


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
    user = User(
        username=username,
        password_hash=hash_password(password),
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class _ClientCase(unittest.TestCase):
    """每条测试用独立内存库 + TestClient，get_db 覆盖到该库。"""

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


class TestTokenRevocation(_ClientCase):
    """R-3：改密后旧 token 失效，新 token 可用；token 带 ver/jti，仍 7 天过期。"""

    def test_change_password_revokes_old_token(self):
        _create_user(self.db, "r3alice", "OldPass123!x")
        token1 = self._login("r3alice", "OldPass123!x")

        claims = jose_jwt.decode(token1, SECRET, algorithms=["HS256"])
        self.assertEqual(claims.get("ver"), 0)
        self.assertTrue(claims.get("jti"))
        # 仍为 7 天有效期（误差 60 秒内）
        self.assertAlmostEqual(claims["exp"] - time.time(), 7 * 86400, delta=60)

        r = self.client.post(
            "/api/auth/change-password",
            json={"old_password": "OldPass123!x", "new_password": "NewPass456!y"},
            headers={"Authorization": f"Bearer {token1}"},
        )
        self.assertEqual(r.status_code, 200, r.text)

        # 旧 token 失效
        r = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token1}"})
        self.assertEqual(r.status_code, 401)

        # 新密码登录成功，新 token 可用
        token2 = self._login("r3alice", "NewPass456!y")
        r = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token2}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "r3alice")


class TestCookieAuth(_ClientCase):
    """R-5：HttpOnly Cookie 登录、登出清除 Cookie 且旧 token 失效。"""

    def test_cookie_login_and_logout(self):
        _create_user(self.db, "r5bob", "BobPass123!x")
        r = self.client.post(
            "/api/auth/login", json={"username": "r5bob", "password": "BobPass123!x"}
        )
        self.assertEqual(r.status_code, 200, r.text)
        set_cookie = r.headers.get("set-cookie", "")
        self.assertIn("access_token=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=lax", set_cookie)
        token = r.json()["access_token"]

        # 只带 Cookie（不带 Authorization 头）也能调 /me
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["username"], "r5bob")

        # 登出：Cookie 被清除
        r = self.client.post("/api/auth/logout")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Max-Age=0", r.headers.get("set-cookie", ""))

        # Cookie 失效
        r = self.client.get("/api/auth/me")
        self.assertEqual(r.status_code, 401)

        # 旧 token（Bearer 头）同样失效（登出递增 token_version）
        r = self.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 401)


class TestDocsDisabled(_ClientCase):
    """R-9：docs/redoc/openapi 默认关闭。"""

    def test_docs_endpoints_404_by_default(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 404, path)


class TestLoginTiming(_ClientCase):
    """R-10：用户不存在与存在时登录都返回 401；不存在时也做了 dummy bcrypt 校验。"""

    def test_unknown_user_login_401_with_dummy_check(self):
        _create_user(self.db, "r10carol", "CarolPass123!x")
        t0 = time.monotonic()
        r = self.client.post(
            "/api/auth/login", json={"username": "r10ghost", "password": "Whatever123!x"}
        )
        dt = time.monotonic() - t0
        self.assertEqual(r.status_code, 401)
        # dummy bcrypt 校验约 200-300ms；下限放宽到 0.05s，只证明校验确实跑了。
        self.assertGreaterEqual(dt, 0.05, f"dummy bcrypt 疑似未执行，耗时仅 {dt:.3f}s")

        r = self.client.post(
            "/api/auth/login", json={"username": "r10carol", "password": "WrongPass123!x"}
        )
        self.assertEqual(r.status_code, 401)


class TestSecurityHeadersAndCors(_ClientCase):
    """R-7 安全响应头；R-8 CORS 默认不放行。"""

    def test_security_headers_present_no_hsts(self):
        r = self.client.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(r.headers.get("x-frame-options"), "DENY")
        self.assertIn("default-src 'self'", r.headers.get("content-security-policy", ""))
        self.assertIn("camera=()", r.headers.get("permissions-policy", ""))
        self.assertNotIn("strict-transport-security", r.headers)

    def test_cors_default_closed(self):
        r = self.client.options(
            "/api/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertNotIn("access-control-allow-origin", r.headers)

    def test_cors_origin_list_settings(self):
        s = Settings(_env_file=None, **VALID)
        self.assertEqual(s.cors_origin_list, [])
        s2 = Settings(
            _env_file=None,
            cors_origins="http://localhost:5173, https://example.com",
            **VALID,
        )
        self.assertEqual(
            s2.cors_origin_list, ["http://localhost:5173", "https://example.com"]
        )


class TestDependencyPins(unittest.TestCase):
    """R-6：依赖钉到已修复已知漏洞的版本。"""

    def test_requirements_pins(self):
        path = os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
        req = open(path).read()
        for pin in (
            "fastapi==0.163.1",
            "python-jose[cryptography]==3.5.0",
            "cryptography==50.0.2",
            "python-multipart==0.0.33",
            "starlette==1.10.0",
        ):
            self.assertIn(pin, req)


if __name__ == "__main__":
    unittest.main()
