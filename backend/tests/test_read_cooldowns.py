"""Structured optional-read failure and account throttle boundaries, entirely offline."""
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from botocore.exceptions import ClientError
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, User
from app.security import encrypt_secret
from app.services import lightsail
from app.services.read_cooldowns import ReadCooldownStore, ReadCooldownActive, cooldown_context, failure_details
from app.services.snapshots import SnapshotReadStopped, scope_for
import test_region_identity as identity_tests


def failure(code="ThrottlingException", retry="120", status=429):
    return ClientError({"Error": {"Code": code, "Message": "read rejected"},
        "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {"retry-after": retry}}}, "GetStaticIps")


class ReadCooldownTests(unittest.TestCase):
    setUp = identity_tests.RegionIdentityTests.setUp

    def store(self, credential=None, user=None):
        return ReadCooldownStore(self.engine, scope_for(user or self.user, credential or self.credential,
                                                        "test", "cooldown"))

    def credential_for(self, account="123456789012", user_id=None):
        row = AwsCredential(user_id=self.user.id if user_id is None else user_id, account_id=account,
            access_key_id_enc=encrypt_secret("AKIAIOSFODNN8EXAMPLE"), secret_access_key_enc=encrypt_secret("fake"))
        self.db.add(row)
        self.db.commit()
        return row

    def client(self):
        client = Mock()
        client.get_instances.return_value = {"instances": [{"name": "stopped", "state": {"name": "stopped"}}]}
        client.get_static_ips.side_effect = failure()
        return client

    def test_static_ip_throttle_keeps_instances_and_skips_only_auxiliary_reads_until_retry(self):
        client = self.client()
        now = time.time()
        with patch("app.services.read_cooldowns.time.time", return_value=now), \
                patch.object(lightsail, "make_client", return_value=client), cooldown_context(self.store()):
            first = lightsail.discover_instances_in_region("fake", "fake", "us-east-1")
            second = lightsail.discover_instances_in_region("fake", "fake", "us-east-1")
        self.assertEqual(first["instances"][0]["name"], "stopped")
        self.assertTrue(first["instances_complete"])
        self.assertEqual(first["auxiliary_failure"]["code"], "ThrottlingException")
        self.assertEqual(first["auxiliary_failure"]["retry_at"], now + 120)
        self.assertEqual(second["auxiliary_failure"]["retry_at"], now + 120)
        self.assertEqual(client.get_instances.call_count, 2)
        self.assertEqual(client.get_static_ips.call_count, 1)
        with patch("app.services.read_cooldowns.time.time", return_value=now + 120), \
                patch.object(lightsail, "make_client", return_value=client), cooldown_context(self.store()):
            lightsail.discover_instances_in_region("fake", "fake", "us-east-1")
        self.assertEqual(client.get_static_ips.call_count, 2)

    def test_account_throttle_is_shared_by_two_keys_but_scope_and_user_remain_isolated(self):
        second = self.credential_for()
        other = User(username="other", password_hash="opaque")
        self.db.add(other)
        self.db.commit()
        foreign = self.credential_for(user_id=other.id)
        different_account = self.credential_for(account="210987654321")
        first_store = self.store()
        first_store.record(failure(), "us-east-1", "lightsail", "GetInstanceMetricData", "web:NetworkIn")
        self.assertIsNotNone(self.store(second).active("us-east-1", "lightsail", "GetInstanceMetricData", "web:NetworkIn"))
        self.assertIsNone(self.store(second).active("us-east-1", "lightsail", "GetInstanceMetricData", "web:NetworkOut"))
        self.assertIsNone(self.store(second).active("eu-west-1", "lightsail", "GetInstanceMetricData", "web:NetworkIn"))
        self.assertIsNone(self.store(different_account).active("us-east-1", "lightsail", "GetInstanceMetricData", "web:NetworkIn"))
        self.assertIsNone(self.store(foreign, other).active("us-east-1", "lightsail", "GetInstanceMetricData", "web:NetworkIn"))

    def test_permission_auth_and_temporary_failures_never_block_other_keys(self):
        second = self.credential_for()
        for exc in (failure("AccessDeniedException", status=403), failure("ExpiredToken", status=403),
                    TimeoutError("temporary outage"), failure("AccessDenied", status=429)):
            with self.subTest(exc=exc):
                self.db.execute(delete(DataSnapshot))
                self.db.commit()
                recorded = self.store().record(exc, "us-east-1", "lightsail", "GetStaticIps")
                self.assertFalse(recorded["shared"])
                self.assertIsNotNone(self.store().active("us-east-1", "lightsail", "GetStaticIps"))
                self.assertIsNone(self.store(second).active("us-east-1", "lightsail", "GetStaticIps"))

    def test_http_429_without_code_is_shared_and_never_shortens_older_deadline(self):
        second = self.credential_for()
        original = self.store().record(failure("", "180"), "us-east-1", "lightsail", "GetInstances")
        shorter = self.store(second).record(failure("", "60"), "us-east-1", "lightsail", "GetInstances")
        self.assertTrue(shorter["shared"])
        self.assertEqual(shorter["code"], "HTTP429")
        self.assertEqual(shorter["retry_at"], original["retry_at"])

    def test_refreshing_one_key_does_not_remove_other_keys_account_throttle(self):
        second = self.credential_for()
        initial = self.store().record(failure(), "us-east-1", "lightsail", "GetInstances")
        self.db.execute(delete(DataSnapshot).where(DataSnapshot.credential_id == self.credential.id))
        self.credential.secret_access_key_enc = encrypt_secret("changed")
        self.db.commit()
        self.assertEqual(self.store(second).active("us-east-1", "lightsail", "GetInstances")["retry_at"], initial["retry_at"])

    def test_no_failure_marker_can_be_recorded_or_read_after_captured_version_changes(self):
        store = self.store()
        self.credential.secret_access_key_enc = encrypt_secret("rotated")
        self.db.commit()
        with self.assertRaises(SnapshotReadStopped):
            store.record(failure(), "us-east-1", "lightsail", "GetStaticIps")
        with self.assertRaises(SnapshotReadStopped):
            store.active("us-east-1", "lightsail", "GetStaticIps")
        self.assertEqual(self.db.scalars(select(DataSnapshot)).all(), [])

    def test_cancellation_is_not_converted_to_auxiliary_failure(self):
        client = self.client()
        client.get_static_ips.side_effect = SnapshotReadStopped("cancelled")
        with patch.object(lightsail, "make_client", return_value=client), cooldown_context(self.store()):
            with self.assertRaises(SnapshotReadStopped):
                lightsail.discover_instances_in_region("fake", "fake", "us-east-1")
        self.assertEqual(self.db.scalars(select(DataSnapshot)).all(), [])

    def test_wrapped_retry_after_and_redacted_optional_failure_remain_structured(self):
        original = failure(retry="150")
        try:
            raise RuntimeError("https://admin:private-password@proxy.local AKIAIOSFODNN7EXAMPLE") from original
        except RuntimeError as wrapped:
            details = failure_details(wrapped, "us-east-1", "lightsail", "GetStaticIps", now=1000)
        self.assertEqual(details["retry_at"], 1150)
        self.assertEqual(details["code"], "ThrottlingException")
        self.assertTrue(details["shared"])
        self.assertNotIn("private-password", details["reason"])
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", details["reason"])

    def test_already_waiting_auxiliary_send_observes_new_shared_throttle_without_extending_deadline(self):
        second = self.credential_for()
        client = self.client()
        calls, first = [], []
        def after_queue_wait():
            from app.services.read_throttle import _current
            # Another credential receives AWS's 429 after this reader entered
            # the global queue but before its actual transport is permitted.
            first.append(self.store(second).record(failure(), "us-east-1", "lightsail", "GetStaticIps"))
            for check in _current.get().checks:
                check()
            calls.append("transport")
            return {"staticIps": []}
        client.get_static_ips.side_effect = after_queue_wait
        with patch.object(lightsail, "make_client", return_value=client), cooldown_context(self.store()):
            result = lightsail.discover_instances_in_region("fake", "fake", "us-east-1")
        self.assertEqual(calls, [])
        self.assertEqual(result["auxiliary_failure"]["retry_at"], first[0]["retry_at"])
        self.assertTrue(result["instances_complete"])
        with self.assertRaises(ReadCooldownActive) as caught:
            self.store().require_available("us-east-1", "lightsail", "GetStaticIps")
        repeated = self.store().record(caught.exception, "us-east-1", "lightsail", "GetStaticIps")
        self.assertEqual(repeated["retry_at"], first[0]["retry_at"])
