"""Actual HTTP task contracts, owner isolation and secret-free validation."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import test_t06_task_queue as queue_tests
from app.database import get_db
from app.deps import get_current_user, get_current_admin
from app.main import app
from app.models import User, OperationTask
from app.services.task_queue import queue_for


class TaskApiTests(unittest.TestCase):
    def setUp(self):
        queue_tests.DurableTaskQueueTests.setUp(self)
        self.old_overrides = dict(app.dependency_overrides)
        self.current_user_id = self.user_id
        with Session(self.engine) as db:
            user = User(username="other", password_hash="opaque", is_admin=True)
            db.add(user)
            db.commit()
            self.other_id = user.id
        def database():
            with Session(self.engine) as db:
                yield db
        def current_user():
            with Session(self.engine) as db:
                user = db.get(User, self.current_user_id)
                list(user.credentials)
                db.expunge(user)
                return user
        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user] = current_user
        app.dependency_overrides[get_current_admin] = current_user
        self.addCleanup(self.restore_overrides)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.actual_queue = queue_for(self.engine)
        self.addCleanup(self.actual_queue.shutdown, wait=True)
        schedule = patch.object(self.actual_queue, "schedule")
        schedule.start()
        self.addCleanup(schedule.stop)

    def restore_overrides(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.old_overrides)

    def submit(self, key="browser-key", **values):
        return self.client.post("/api/v2/tasks", headers={"Idempotency-Key": key}, json={
            "kind": "stop", "credential_id": 1, "region": "us-east-1", "resource_name": "web", **values})

    def test_202_recovery_and_conflicting_envelope(self):
        first = self.submit()
        self.assertEqual(first.status_code, 202, first.text)
        task_id = first.json()["task_id"]
        self.assertEqual(first.headers["location"], f"/api/v2/tasks/{task_id}")
        self.assertEqual(self.submit().json()["task_id"], task_id)
        self.assertEqual(self.submit(kind="reboot").status_code, 409)
        self.assertEqual(self.client.get("/api/v2/tasks/by-key", params={"key": "browser-key"}).json()["task_id"], task_id)
        self.assertEqual(self.client.post(f"/api/v2/tasks/{task_id}/cancel").json()["status"], "cancelled")

    def test_every_task_route_hides_foreign_owner(self):
        task_id = self.submit().json()["task_id"]
        self.current_user_id = self.other_id
        self.assertEqual(self.client.get("/api/v2/tasks").json(), [])
        self.assertEqual(self.client.get("/api/v2/tasks/by-key", params={"key": "browser-key"}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/v2/tasks/{task_id}").status_code, 404)
        for action in ("cancel", "reconcile", "retry", "resolve"):
            response = self.client.post(f"/api/v2/tasks/{task_id}/{action}",
                json={"acknowledge_task_id": task_id} if action == "resolve" else None)
            self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.submit().status_code, 404)

    def test_validation_does_not_echo_password_or_userdata(self):
        secret = "SecretFromInvalidBody123!"
        bodies = [
            {"kind": "invalid", "credential_id": 1, "payload": {"password": secret}},
            {"kind": "create", "credential_id": 1, "payload": {"password": secret}},
            {"kind": "stop", "credential_id": 1, "region": "us-east-1", "resource_name": "web",
             "payload": {"userData": secret}},
        ]
        for body in bodies:
            response = self.client.post("/api/v2/tasks", headers={"Idempotency-Key": "invalid"}, json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn(secret, response.text)
        response = self.client.post("/api/instances", json={"password": secret})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(secret, response.text)

    def test_legacy_timeout_returns_task_identity_without_false_success(self):
        with patch("app.services.task_submission.LEGACY_WAIT_SECONDS", 0):
            response = self.client.post("/api/instances/us-east-1/web/stop?credential_id=1",
                headers={"Idempotency-Key": "legacy"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["status"], "queued")
        self.assertTrue(response.json()["task_id"])
        self.assertEqual(self.client.get(response.json()["status_url"]).status_code, 200)

    def test_delete_gates_hold_until_unstarted_task_cancelled(self):
        task_id = self.submit().json()["task_id"]
        self.assertEqual(self.client.delete("/api/credentials/1").status_code, 409)
        self.current_user_id = self.other_id
        self.assertEqual(self.client.delete(f"/api/users/{self.user_id}").status_code, 409)
        self.current_user_id = self.user_id
        self.assertEqual(self.client.post(f"/api/v2/tasks/{task_id}/cancel").status_code, 200)
        self.assertEqual(self.client.delete("/api/credentials/1").status_code, 200)

    def test_unresolved_tasks_precede_new_terminal_history_with_paging(self):
        task_id = self.submit().json()["task_id"]
        with Session(self.engine) as db:
            original = db.get(OperationTask, task_id)
            original.status = "reconciliation_required"
            original.created_at = datetime.now(timezone.utc) - timedelta(days=31)
            for index in range(101):
                db.add(OperationTask(id=f"terminal-{index}", user_id=self.user_id, credential_id=1,
                    account_scope=original.account_scope, credential_version=original.credential_version,
                    kind="stop", idempotency_key=f"terminal-{index}", request_fingerprint="f" * 64,
                    encrypted_payload=original.encrypted_payload, status="succeeded"))
            db.commit()
        first = self.client.get("/api/v2/tasks?limit=100").json()
        second = self.client.get("/api/v2/tasks?limit=100&offset=100").json()
        self.assertEqual(first[0]["task_id"], task_id)
        self.assertEqual(len(first), 100)
        self.assertEqual(len(second), 2)
        self.assertFalse({row["task_id"] for row in first} & {row["task_id"] for row in second})
