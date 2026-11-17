"""Integration checks for the offline host, using real authentication and routes."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx
from tools.optimization.processes import stop_process_tree

ROOT = Path(__file__).resolve().parents[3]


class OfflineHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="lightsail-t00-test-")
        cls.addClassCleanup(cls.temp.cleanup)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        cls.url = f"http://127.0.0.1:{port}"
        cls.log = open(Path(cls.temp.name) / "server.log", "w", encoding="utf-8")
        cls.addClassCleanup(cls.log.close)
        cls.proc = subprocess.Popen(
            [sys.executable, "-m", "tools.optimization.benchmark_server",
             "--port", str(port), "--data-dir", cls.temp.name, "--samples", "1",
             "--delay-ms", "0"], cwd=ROOT, stdout=cls.log, stderr=subprocess.STDOUT,
        )
        def stop():
            if cls.proc.poll() is None:
                stop_process_tree(cls.proc)
        cls.addClassCleanup(stop)
        for _ in range(150):
            if cls.proc.poll() is not None:
                cls.log.flush()
                raise AssertionError((Path(cls.temp.name) / "server.log").read_text())
            try:
                if httpx.get(cls.url + "/api/health", timeout=0.2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        raise AssertionError("offline host did not start")

    def login(self, name):
        client = httpx.Client(base_url=self.url)
        self.addCleanup(client.close)
        response = client.post("/api/auth/login", json={
            "username": name, "password": "T00FixturePass123!"})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_real_billing_route_uses_fixture_account(self):
        client = self.login("t00-api-billing-00")
        response = client.get("/api/billing/summary")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["aws_account_id"], "111111111111")
        self.assertEqual(body["total_cost"], 30)
        self.assertEqual(body["aws_credits_status"], "ok")
        self.assertEqual(body["aws_credits"][0]["remaining_amount"], 80)

    def test_other_users_credential_cannot_be_queried(self):
        owner = self.login("t00-api-billing-00")
        credential = owner.get("/api/credentials").json()["items"][0]["id"]
        other = self.login("t00-api-instances-00")
        response = other.get("/api/instances", params={"credential_id": credential})
        self.assertEqual(response.status_code, 404)

    def test_unauthenticated_route_and_spa_fallback(self):
        self.assertEqual(httpx.get(self.url + "/api/instances").status_code, 401)
        response = httpx.get(self.url + "/billing")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="root"', response.text)
        self.assertIn("style-src 'self' 'unsafe-inline'",
                      response.headers["content-security-policy"])
