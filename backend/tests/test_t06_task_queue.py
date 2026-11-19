"""SQLite-only tests for durable task acceptance and lock transitions."""
import os
import tempfile
import unittest
import uuid
import time
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AwsCredential, OperationTask, OperationTaskLock, OperationTaskStep, User
from app.security import encrypt_secret
from app.services.task_queue import DurableTaskQueue, TaskConflict, TaskContext


class DurableTaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'tasks.db'}")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.queue = DurableTaskQueue(self.engine)
        self.addCleanup(lambda: self.queue.shutdown(wait=True))
        with Session(self.engine) as db:
            user = User(username="owner", password_hash="opaque")
            db.add(user)
            db.flush()
            for credential_id in (1, 2):
                db.add(AwsCredential(
                    id=credential_id, user_id=user.id,
                    access_key_id_enc=encrypt_secret(f"AKIAIOSFODNN7EXAMPLE{credential_id}"),
                    secret_access_key_enc=encrypt_secret(f"secret-key-{credential_id}-long"),
                    account_id="123456789012", is_default=credential_id == 1,
                ))
            db.commit()
            self.user_id = user.id

    def accept(self, credential_id=1, *, key=None, payload=None, account_lock=False, wait=False):
        with Session(self.engine) as db:
            user = db.get(User, self.user_id)
            credential = db.get(AwsCredential, credential_id)
            return self.queue.accept(
                db, user, credential, "stop", payload or {"region": "us-east-1", "resource_name": "web"},
                key or str(uuid.uuid4()), region="us-east-1", resource_name="web",
                account_lock=account_lock, wait=wait,
            )

    def test_idempotency_returns_same_task_and_rejects_different_envelope(self):
        finished = Event()
        from app.services import task_queue
        with patch.dict(task_queue._HANDLERS, {"stop": lambda _ctx: (finished.set() or {"ok": True})}):
            first = self.accept(key="browser-intent-1")
            same = self.accept(key="browser-intent-1")
            self.assertEqual(first["task_id"], same["task_id"])
            with self.assertRaises(TaskConflict):
                self.accept(key="browser-intent-1", payload={"region": "eu-west-1", "resource_name": "web"})
            self.assertTrue(finished.wait(2))

    def test_same_confirmed_account_lock_conflicts_across_credentials(self):
        from app.services import task_queue
        hold = Event()
        with patch.dict(task_queue._HANDLERS, {"stop": lambda _ctx: (hold.wait(2) or {"ok": True})}):
            self.accept(credential_id=1, key="first")
            with self.assertRaises(TaskConflict):
                self.accept(credential_id=2, key="second")
            hold.set()

    def test_schedule_callback_does_not_deadlock_when_future_already_finished(self):
        from app.services import task_queue
        with patch.dict(task_queue._HANDLERS, {"stop": lambda _ctx: {"ok": True}}):
            result = self.accept(key="fast", wait=True)
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(self.queue._futures)

    def test_cancel_requires_atomic_queued_transition_and_keeps_running_lock(self):
        with patch.object(self.queue, "schedule"):
            result = self.accept(key="cancel-me")
        task_id = result["task_id"]
        with Session(self.engine) as db:
            task = db.get(OperationTask, task_id)
            task.status = "running"
            task.worker_token = "worker"
            db.commit()
        with Session(self.engine) as db:
            with self.assertRaises(TaskConflict):
                self.queue.cancel(db, self.user_id, task_id)
        with Session(self.engine) as db:
            self.assertIsNotNone(db.scalar(select(OperationTaskLock).where(
                OperationTaskLock.task_id == task_id)))

    def test_task_payload_is_encrypted_and_intents_are_scrubbed(self):
        with patch.object(self.queue, "schedule"):
            result = self.accept(key="encrypted", payload={
                "region": "us-east-1", "resource_name": "web", "password": "custom-password",
            })
        with Session(self.engine) as db:
            task = db.get(OperationTask, result["task_id"])
            self.assertNotIn("custom-password", task.encrypted_payload)
            self.assertNotIn("custom-password", task.request_fingerprint)
            token = "worker-token"
            task.status = "running"
            task.worker_token = token
            task.lease_until = time.time() + 60
            db.commit()
        context = TaskContext(self.engine, result["task_id"], token, {})
        sequence = context.begin_step("safe", {"password": "custom-password", "name": "web"})
        with Session(self.engine) as db:
            step = db.scalar(select(OperationTaskStep).where(
                OperationTaskStep.task_id == result["task_id"],
                OperationTaskStep.sequence == sequence,
            ))
            self.assertNotIn("custom-password", step.intent_json)
            self.assertIn("web", step.intent_json)


if __name__ == "__main__":
    unittest.main()
