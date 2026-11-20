"""Regional authentication failures require independent STS confirmation."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class RegionIdentityTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.database import Base
        from app.models import AwsCredential, User
        from app.security import encrypt_secret
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name).as_posix()}/app.db",
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.db.close)
        self.user = User(username="region-identity", password_hash="opaque")
        self.db.add(self.user)
        self.db.flush()
        self.credential = AwsCredential(user_id=self.user.id, account_id="123456789012",
            access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
            secret_access_key_enc=encrypt_secret("fake-secret"))
        self.db.add(self.credential)
        self.db.commit()
        self.transport = patch("botocore.endpoint.Endpoint._send",
                               side_effect=AssertionError("AWS transport forbidden")).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: self.assertEqual(self.transport.call_count, 0))

    def warm(self):
        from app.services import billing, lightsail
        from app.services.instance_cache import instance_scopes
        from app.services.snapshots import read_snapshot, scope_for
        async def cost(): return {"amount": "12.50"}
        self.billing_scope = scope_for(self.user, self.credential, "billing-costs", billing._today_utc().isoformat())
        asyncio.run(read_snapshot(self.engine, self.billing_scope, 1800, cost, wait=True))
        with patch.object(lightsail, "get_regions", return_value=[{"name": name} for name in
                ("us-east-1", "eu-west-1", "ap-southeast-1")]), \
             patch.object(lightsail, "list_instances_in_region", side_effect=lambda _a, _s, name, **_kw:
                          [{"name": "kept-" + name, "region": name}]):
            asyncio.run(instance_scopes(self.engine, self.user, [self.credential], wait=True))

    def refresh(self, identity):
        from app.services import billing, lightsail
        from app.services.instance_cache import instance_scopes
        def region(_ak, _sk, name, **_kw):
            if name != "us-east-1":
                raise lightsail.LightsailError("region authentication failed", code="UnrecognizedClientException")
            return [{"name": "healthy", "region": name}]
        kwargs = {"side_effect": identity} if isinstance(identity, Exception) or callable(identity) else {"return_value": identity}
        with patch.object(lightsail, "list_instances_in_region", side_effect=region), \
             patch.object(billing, "get_sts_identity", **kwargs) as sts:
            views = asyncio.run(instance_scopes(self.engine, self.user, [self.credential], wait=True, force=True))
        return views, sts.call_count

    def assert_preserved(self, views):
        from app.models import DataSnapshot
        self.db.expire_all()
        self.assertEqual(views[0]["data"], [{"name": "healthy", "region": "us-east-1"}])
        self.assertIsNotNone(self.db.get(DataSnapshot, self.billing_scope["key"]).payload)
        self.assertTrue(all(row["data"] is None and row["error"] for row in views[1:]))

    def test_region_errors_coalesce_sts_and_preserve_other_scopes(self):
        self.warm()
        views, calls = self.refresh("123456789012")
        self.assert_preserved(views)
        self.assertEqual(calls, 1)

    def test_unavailable_denied_or_unknown_sts_preserves_other_scopes(self):
        from botocore.exceptions import ClientError
        from app.services.billing import BillingUnavailableError
        for failure in (BillingUnavailableError("temporary STS outage"),
                        ClientError({"Error": {"Code": "AccessDenied"}}, "GetCallerIdentity"), None):
            with self.subTest(failure=failure):
                self.warm()
                views, calls = self.refresh(failure)
                self.assert_preserved(views)
                self.assertEqual(calls, 1)
                from app.models import DataSnapshot
                self.db.query(DataSnapshot).delete()
                self.db.commit()

    def test_sts_expired_or_changed_account_invalidates_all_scopes(self):
        from botocore.exceptions import ClientError
        from app.models import DataSnapshot
        for identity in (ClientError({"Error": {"Code": "ExpiredToken"}}, "GetCallerIdentity"), "210987654321"):
            with self.subTest(identity=identity):
                self.warm()
                views, calls = self.refresh(identity)
                self.assertEqual(calls, 1)
                self.assertTrue(all(row["data"] is None for row in views))
                self.db.expire_all()
                self.assertTrue(all(row.payload is None for row in self.db.query(DataSnapshot).all()))
                self.db.query(DataSnapshot).delete()
                self.db.commit()

    def test_new_failure_rechecks_sts_despite_recent_success(self):
        from app.models import DataSnapshot
        from botocore.exceptions import ClientError
        self.warm()
        self.refresh("123456789012")
        self.db.query(DataSnapshot).filter(DataSnapshot.kind == "instances").update({"next_retry_at": 0})
        self.db.commit()
        views, calls = self.refresh(ClientError({"Error": {"Code": "ExpiredToken"}}, "GetCallerIdentity"))
        self.assertEqual(calls, 1)
        self.assertTrue(all(row["data"] is None for row in views))

    def test_failed_refresh_cools_down_for_sixty_seconds_even_when_forced(self):
        from app.services.snapshots import SnapshotError, read_many, read_snapshot, scope_for
        scope = scope_for(self.user, self.credential, "test", "cooldown")
        calls = []
        async def failing():
            calls.append(1)
            raise SnapshotError("temporary failure")
        now = time.time()
        with patch("app.services.snapshots.time.time", return_value=now):
            asyncio.run(read_snapshot(self.engine, scope, 60, failing, wait=True))
        with patch("app.services.snapshots.time.time", return_value=now + 59):
            asyncio.run(read_snapshot(self.engine, scope, 60, failing, wait=True, force=True))
            asyncio.run(read_many(self.engine, [(scope, 60, failing)], wait=True, force=True))
        self.assertEqual(len(calls), 1)
        with patch("app.services.snapshots.time.time", return_value=now + 60):
            asyncio.run(read_snapshot(self.engine, scope, 60, failing, wait=True, force=True))
        self.assertEqual(len(calls), 2)

    def test_confirmed_invalid_identity_blocks_new_scopes_after_cooldown(self):
        from app.services.snapshots import read_many, read_snapshot, scope_for
        from botocore.exceptions import ClientError
        self.warm()
        self.refresh(ClientError({"Error": {"Code": "ExpiredToken"}}, "GetCallerIdentity"))
        calls = []
        async def load():
            calls.append(1)
            return {"private": "new-scope"}
        scope = scope_for(self.user, self.credential, "instances", "us-west-2")
        with patch("app.services.snapshots.time.time", return_value=time.time() + 61):
            one = asyncio.run(read_snapshot(self.engine, scope, 60, load, wait=True, force=True))
            many = asyncio.run(read_many(self.engine, [(scope, 60, load)], wait=True, force=True))
        self.assertEqual(calls, [])
        self.assertIsNone(one["data"])
        self.assertIsNone(many[0]["data"])

    def delete_during_claim(self, many):
        from app.services import snapshots
        from sqlalchemy.dialects.sqlite import insert
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        scope = snapshots.scope_for(self.user, self.credential, "test", "deleted")
        def racing_insert(model):
            with self.engine.begin() as connection:
                connection.exec_driver_sql("DELETE FROM aws_credentials WHERE id=?", (self.credential.id,))
            return insert(model)
        async def load():
            self.fail("Deleted credentials must not start AWS work")
        with patch.object(snapshots, "insert", side_effect=racing_insert):
            result = asyncio.run(snapshots.read_many(self.engine, [(scope, 60, load)], wait=True))[0] if many else asyncio.run(
                snapshots.read_snapshot(self.engine, scope, 60, load, wait=True))
        self.assertEqual(result["status"], "invalidated")

    def test_deleted_credential_between_check_and_claim_returns_invalidated(self):
        self.delete_during_claim(False)

    def test_deleted_credential_during_batch_claim_returns_invalidated(self):
        self.delete_during_claim(True)

    def test_changed_version_between_check_and_claim_never_starts_loader(self):
        from app.services import snapshots
        from sqlalchemy.dialects.sqlite import insert
        from sqlalchemy.orm import Session
        from app.models import AwsCredential, DataSnapshot, User
        for many in (False, True):
            for field in ("secret_access_key_enc", "account_id", "proxy_url_enc", "token_version"):
                with self.subTest(many=many, field=field):
                    self.db.expire_all()
                    scope = snapshots.scope_for(self.user, self.credential, "test", f"rotate-{many}-{field}")
                    def racing_insert(model):
                        with Session(self.engine) as local:
                            row = local.get(User, self.user.id) if field == "token_version" else local.get(
                                AwsCredential, self.credential.id)
                            setattr(row, field, row.token_version + 1 if field == "token_version" else
                                    ("210987654321" if row.account_id != "210987654321" else "123456789012")
                                    if field == "account_id" else f"changed-{many}-{field}")
                            local.commit()
                        return insert(model)
                    async def load():
                        self.fail("Superseded credentials must not start AWS work")
                    with patch.object(snapshots, "insert", side_effect=racing_insert):
                        result = asyncio.run(snapshots.read_many(self.engine, [(scope, 60, load)], wait=True))[0] if many else asyncio.run(
                            snapshots.read_snapshot(self.engine, scope, 60, load, wait=True))
                    self.assertEqual(result["status"], "invalidated")
                    with Session(self.engine) as local:
                        self.assertIsNone(local.get(DataSnapshot, scope["key"]))

    def test_unrelated_integrity_errors_are_not_suppressed(self):
        from app.services import snapshots
        from sqlalchemy.exc import IntegrityError
        for many in (False, True):
            with self.subTest(many=many):
                scope = snapshots.scope_for(self.user, self.credential, "test", f"invalid-{many}")
                scope["kind"] = None  # A genuine NOT NULL error, with a still-current parent.
                async def load():
                    self.fail("An invalid lease must not start AWS work")
                with self.assertRaises(IntegrityError):
                    asyncio.run(snapshots.read_many(self.engine, [(scope, 60, load)], wait=True)) if many else asyncio.run(
                        snapshots.read_snapshot(self.engine, scope, 60, load, wait=True))

    def test_confirmed_invalid_identity_between_check_and_claim_blocks_both_entries(self):
        from app.services import snapshots
        from app.services.identity_validation import identity_scope_for
        from sqlalchemy.dialects.sqlite import insert
        from app.models import DataSnapshot
        for many in (False, True):
            with self.subTest(many=many):
                self.db.query(DataSnapshot).delete()
                self.db.commit()
                identity_scope = identity_scope_for(self.user, self.credential)
                identity_token = snapshots._read_or_claim(self.engine, identity_scope, True)[1]
                scope = snapshots.scope_for(self.user, self.credential, "test", f"revoked-{many}")
                def racing_insert(model):
                    snapshots._publish(self.engine, identity_scope, identity_token, 1800,
                        error="STS confirmed expired", invalidate=True, invalidate_credential=True)
                    return insert(model)
                async def load():
                    self.fail("Confirmed invalid credentials must not start a loader")
                with patch.object(snapshots, "insert", side_effect=racing_insert):
                    result = asyncio.run(snapshots.read_many(self.engine, [(scope, 60, load)], wait=True))[0] if many else asyncio.run(
                        snapshots.read_snapshot(self.engine, scope, 60, load, wait=True))
                self.assertEqual(result["error_status"], "credential_invalid")
                self.assertIsNone(result["data"])

    def test_invalidated_while_request_in_flight_cannot_republish_payload(self):
        from app.services import snapshots
        from app.services.identity_validation import identity_scope_for
        from app.models import DataSnapshot
        identity_scope = identity_scope_for(self.user, self.credential)
        identity_token = snapshots._read_or_claim(self.engine, identity_scope, False)[1]
        scope = snapshots.scope_for(self.user, self.credential, "test", "late-after-revocation")
        async def load():
            snapshots._publish(self.engine, identity_scope, identity_token, 1800,
                error="STS confirmed expired", invalidate=True, invalidate_credential=True)
            return {"private": "late-result"}
        result = asyncio.run(snapshots.read_snapshot(self.engine, scope, 60, load, wait=True))
        self.assertIsNone(result["data"])
        self.assertEqual(result["error_status"], "credential_invalid")
        self.db.expire_all()
        self.assertIsNone(self.db.get(DataSnapshot, scope["key"]).payload)

    def test_delete_after_claim_before_loader_stops_send(self):
        from app.services import snapshots
        from app.models import AwsCredential
        scope = snapshots.scope_for(self.user, self.credential, "test", "delete-after-claim")
        view, token = snapshots._read_or_claim(self.engine, scope, False)
        self.db.delete(self.credential)
        self.db.commit()
        async def load():
            self.fail("Deleted credentials must not reach the loader")
        result = asyncio.run(snapshots.finish_snapshot(self.engine, scope, 60, load, True, view, token))
        self.assertEqual(result["status"], "invalidated")

    def test_aws_retry_after_longer_than_sixty_seconds_is_preserved(self):
        from app.services import snapshots
        from botocore.exceptions import ClientError
        scope = snapshots.scope_for(self.user, self.credential, "test", "long-cooldown")
        calls = []
        async def load():
            calls.append(1)
            raise snapshots.SnapshotError("AWS throttled") from ClientError({"Error": {"Code": "ThrottlingException"},
                "ResponseMetadata": {"HTTPHeaders": {"retry-after": "120"}}}, "GetInstances")
        now = time.time()
        with patch.object(snapshots.time, "time", return_value=now):
            asyncio.run(snapshots.read_snapshot(self.engine, scope, 60, load, wait=True))
        with patch.object(snapshots.time, "time", return_value=now + 119):
            asyncio.run(snapshots.read_snapshot(self.engine, scope, 60, load, wait=True, force=True))
        self.assertEqual(len(calls), 1)
        with patch.object(snapshots.time, "time", return_value=now + 120):
            asyncio.run(snapshots.read_snapshot(self.engine, scope, 60, load, wait=True, force=True))
        self.assertEqual(len(calls), 2)

    def test_manual_validation_temporary_and_region_errors_keep_independent_results(self):
        from app.services import billing, lightsail, snapshots
        from app.routers.credentials import validate_one
        from fastapi import HTTPException
        from app.models import DataSnapshot
        scope = snapshots.scope_for(self.user, self.credential, "billing-costs", "kept")
        async def load(): return {"amount": "12.50"}
        asyncio.run(snapshots.read_snapshot(self.engine, scope, 1800, load, wait=True))
        for failure in (lightsail.LightsailError("timeout"),
                        lightsail.LightsailError("regional authentication", code="UnrecognizedClientException")):
            with patch.object(lightsail, "validate_credentials", side_effect=failure), \
                    patch.object(billing, "get_sts_identity", return_value="123456789012"):
                with self.assertRaises(HTTPException) as caught:
                    validate_one(self.credential.id, self.user, self.db)
            self.assertEqual(caught.exception.status_code, 400)
            self.db.expire_all()
            self.assertEqual(self.credential.account_id, "123456789012")
            self.assertIsNotNone(self.db.get(DataSnapshot, scope["key"]).payload)
