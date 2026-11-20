"""Credential deletion through HTTP with real SQLite foreign-key enforcement."""
import os
import sys
import unittest
from datetime import date
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    AwsCredential, CreditRecord, DataSnapshot, InstanceSetting, OperationLog,
    OperationTask, OperationTaskStep, TrafficUsage, User,
)
from app.routers.credentials import router
from app.security import create_access_token
from app.services import snapshots


class CredentialDeleteTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite:///:memory:", connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False)
        self.preload = False
        self.request_sessions_clean = []
        self.app = FastAPI()
        self.app.include_router(router, prefix="/api")

        def database():
            with self.session_factory() as db:
                retained = []
                if self.preload:
                    retained = list(db.scalars(select(AwsCredential)))
                    for credential in retained:
                        list(credential.instance_settings)
                        list(credential.traffic_usages)
                    retained.extend(db.scalars(select(CreditRecord)))
                    retained.extend(db.scalars(select(DataSnapshot)))
                try:
                    yield db
                finally:
                    self.request_sessions_clean.append(not db.in_transaction())

        self.app.dependency_overrides[get_db] = database
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.transport_patch = patch(
            "botocore.endpoint.Endpoint._send", side_effect=AssertionError("AWS transport forbidden")
        )
        self.transport = self.transport_patch.start()
        self.addCleanup(self.transport_patch.stop)
        self.addCleanup(lambda: self.assertEqual(self.transport.call_count, 0))
        with self.session_factory() as db:
            alice = User(username="delete-alice", password_hash="unused", is_admin=False)
            bob = User(username="delete-bob", password_hash="unused", is_admin=False)
            db.add_all([alice, bob])
            db.flush()
            self.user_id, self.other_user_id = alice.id, bob.id
            credentials = [
                AwsCredential(user_id=alice.id, access_key_id_enc="offline-key-one",
                    secret_access_key_enc="offline-secret", account_id="123456789012", is_default=True),
                AwsCredential(user_id=alice.id, access_key_id_enc="offline-key-two",
                    secret_access_key_enc="offline-secret", account_id="123456789012", is_default=False),
                AwsCredential(user_id=bob.id, access_key_id_enc="offline-key-three",
                    secret_access_key_enc="offline-secret", account_id="123456789012", is_default=True),
            ]
            db.add_all(credentials)
            db.flush()
            self.credential_id, self.next_id, self.other_id = [row.id for row in credentials]
            self.scope = snapshots.scope_for(alice, credentials[0], "instances", "us-east-1")
            for index, credential in enumerate(credentials):
                db.add_all([
                    InstanceSetting(user_id=credential.user_id, credential_id=credential.id,
                        region="us-east-1", instance_name="web", monthly_limit_gb=10),
                    TrafficUsage(user_id=credential.user_id, credential_id=credential.id,
                        region="us-east-1", instance_name="web", year_month="2026-11",
                        account_id="123456789012", network_in_bytes=100, network_out_bytes=200),
                    CreditRecord(user_id=credential.user_id, credential_id=credential.id,
                        name=f"manual grant {index}", total_amount=50, total_amount_decimal="50.00",
                        amount_precision="exact", account_id="123456789012", start_date=date(2026, 11, 1)),
                    DataSnapshot(**snapshots.scope_for(
                        alice if index < 2 else bob, credential, "instances", "us-east-1"),
                        payload='{"instances":["web"]}', payload_bytes=21,
                        lease_token="old-worker", lease_until=9999999999),
                ])
            db.add(OperationLog(user_id=alice.id, action="stop_instance", status="success", message="history"))
            task = self.task("finished", "succeeded")
            db.add(task)
            db.flush()
            db.add(OperationTaskStep(task_id=task.id, sequence=1, name="stop", status="succeeded"))
            db.commit()
        self.headers = {"Authorization": "Bearer " + create_access_token("delete-alice", token_version=0)}

    def task(self, task_id, status, credential_id=None, user_id=None):
        return OperationTask(
            id=task_id, user_id=user_id or self.user_id,
            credential_id=credential_id or self.credential_id,
            account_scope="account:123456789012", account_id="123456789012",
            credential_version="v" * 64, kind="stop", idempotency_key=task_id,
            request_fingerprint="f" * 64, encrypted_payload="offline-payload", status=status,
        )

    def delete(self, credential_id=None):
        return self.client.delete(
            f"/api/credentials/{credential_id or self.credential_id}", headers=self.headers,
        )

    def assert_deleted_contract(self):
        with self.session_factory() as db:
            self.assertIsNone(db.get(AwsCredential, self.credential_id))
            for model in (InstanceSetting, TrafficUsage, DataSnapshot):
                remaining = list(db.scalars(select(model)))
                self.assertEqual({row.credential_id for row in remaining}, {self.next_id, self.other_id})
            credits = list(db.scalars(select(CreditRecord).order_by(CreditRecord.id)))
            self.assertEqual(len(credits), 3)
            self.assertIsNone(credits[0].credential_id)
            self.assertEqual((credits[0].account_id, credits[0].total_amount_decimal), ("123456789012", "50.00"))
            self.assertEqual([row.credential_id for row in credits[1:]], [self.next_id, self.other_id])
            self.assertTrue(db.get(AwsCredential, self.next_id).is_default)
            self.assertTrue(db.get(AwsCredential, self.other_id).is_default)
            self.assertIsNotNone(db.get(OperationTask, "finished"))
            self.assertEqual(len(list(db.scalars(select(OperationTaskStep)))), 1)
            self.assertEqual(
                [row.action for row in db.scalars(select(OperationLog).order_by(OperationLog.id))],
                ["stop_instance", "delete_credentials"],
            )

    def test_delete_with_unloaded_relationships_preserves_grants_history_and_other_credentials(self):
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_deleted_contract()

    def test_delete_with_loaded_relationships_preserves_grants_history_and_other_credentials(self):
        self.preload = True
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_deleted_contract()

    def test_delete_without_instance_setting_still_deletes_nonnullable_traffic(self):
        with self.session_factory() as db:
            row = db.scalar(select(InstanceSetting).where(InstanceSetting.credential_id == self.credential_id))
            db.delete(row)
            db.commit()
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_deleted_contract()

    def test_other_users_credential_returns_404_and_keeps_all_rows(self):
        response = self.delete(self.other_id)
        self.assertEqual(response.status_code, 404, response.text)
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AwsCredential)))), 3)
            self.assertEqual(len(list(db.scalars(select(TrafficUsage)))), 3)
            self.assertEqual(len(list(db.scalars(select(OperationLog)))), 1)

    def test_unresolved_resource_tasks_return_409_without_mutation(self):
        for status in ("queued", "running", "reconciliation_required"):
            with self.subTest(status=status):
                with self.session_factory() as db:
                    db.add(self.task("active", status))
                    db.commit()
                response = self.delete()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn("待核对", response.json()["detail"])
                with self.session_factory() as db:
                    self.assertTrue(db.get(AwsCredential, self.credential_id).is_default)
                    self.assertFalse(db.get(AwsCredential, self.next_id).is_default)
                    self.assertEqual(len(list(db.scalars(select(TrafficUsage)))), 3)
                    self.assertEqual(len(list(db.scalars(select(OperationLog)))), 1)
                    db.delete(db.get(OperationTask, "active"))
                    db.commit()

    def test_another_credentials_active_task_does_not_block_deletion(self):
        with self.session_factory() as db:
            db.add(self.task("other-active", "running", self.other_id, self.other_user_id))
            db.commit()
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_deleted_contract()

    def test_failure_after_parent_delete_rolls_back_children_grants_and_default_switch(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("""CREATE TRIGGER fail_delete_audit BEFORE INSERT ON operation_logs
                WHEN NEW.action = 'delete_credentials'
                BEGIN SELECT RAISE(ABORT, 'synthetic deletion failure'); END""")
        response = self.delete()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(response.headers.get("content-type"), "application/json", response.text)
        self.assertIn("回滚", response.json()["detail"])
        self.assertNotIn("synthetic", response.text)
        self.assertTrue(self.request_sessions_clean[-1])
        with self.session_factory() as db:
            self.assertEqual(len(list(db.scalars(select(AwsCredential)))), 3)
            for model in (InstanceSetting, TrafficUsage, DataSnapshot):
                self.assertEqual(len(list(db.scalars(select(model)))), 3)
            self.assertEqual(
                {row.credential_id for row in db.scalars(select(CreditRecord))},
                {self.credential_id, self.next_id, self.other_id},
            )
            self.assertTrue(db.get(AwsCredential, self.credential_id).is_default)
            self.assertFalse(db.get(AwsCredential, self.next_id).is_default)
            self.assertEqual(len(list(db.scalars(select(OperationLog)))), 1)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER fail_delete_audit")
        retry = self.delete()
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assert_deleted_contract()

    def test_deletion_discards_inflight_snapshot_and_prevents_new_refresh_claim(self):
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        result = snapshots._publish(self.engine, self.scope, "old-worker", 60, data={"instances": ["late"]})
        self.assertIsNone(result)
        view, lease = snapshots._read_or_claim(self.engine, self.scope, True)
        self.assertEqual(view["status"], "invalidated")
        self.assertIsNone(lease)
        self.assert_deleted_contract()

    def test_deleting_nondefault_credential_keeps_current_default(self):
        response = self.delete(self.next_id)
        self.assertEqual(response.status_code, 200, response.text)
        with self.session_factory() as db:
            self.assertIsNone(db.get(AwsCredential, self.next_id))
            self.assertTrue(db.get(AwsCredential, self.credential_id).is_default)
            self.assertTrue(db.get(AwsCredential, self.other_id).is_default)


if __name__ == "__main__":
    unittest.main()
