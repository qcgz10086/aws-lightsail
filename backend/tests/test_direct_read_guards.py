"""Direct read entry points recheck authorization after real SDK queue waits."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.awsrequest import AWSResponse
from botocore.endpoint import Endpoint
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AwsCredential, DataSnapshot, User
from app.routers import credentials, instances, metrics
from app.security import encrypt_secret
from app.schemas import CredentialCreate, CredentialUpdate
from app.services import aws_limits
from app.services.credential_reads import CredentialReadGuard
from app.services.read_throttle import ReadThrottle, _current


class DirectReadGuardTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        engine = create_engine("sqlite:///" + (Path(temporary.name) / "guard.db").as_posix(),
                               connect_args={"check_same_thread": False})
        self.addCleanup(engine.dispose)
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as db:
            user = User(username="direct-read", password_hash="opaque")
            db.add(user)
            db.flush()
            credential = AwsCredential(user_id=user.id, account_id="123456789012", is_default=True,
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
                secret_access_key_enc=encrypt_secret("offline-secret"))
            db.add(credential)
            db.commit()
            return engine, user.id, credential.id

    def wait_queued(self, throttle, worker, errors):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            with throttle.condition:
                if throttle.turns:
                    return
            if not worker.is_alive():
                break
            time.sleep(.005)
        self.fail(f"Read never entered its transport queue: {errors}")

    def queue_then_change(self, route, mutation):
        engine, user_id, credential_id = self.fixture()
        sends, errors, results = [], [], []
        throttle = ReadThrottle(lambda: 0)
        def simulated(_endpoint, request):
            sends.append((_current.get().scope, len(_current.get().checks)))
            self.fail("Revoked direct read reached offline transport")
        def run():
            with Session(engine, expire_on_commit=False) as db:
                user = db.get(User, user_id)
                list(user.credentials)
                try:
                    if route == "metrics":
                        results.append(asyncio.run(metrics.metrics("us-east-1", "server", period="day",
                            credential_id=credential_id, user=user, db=db)))
                    elif route == "instance":
                        results.append(asyncio.run(instances.get_instance("us-east-1", "server",
                            credential_id=credential_id, user=user, db=db)))
                    elif route == "quotas":
                        results.append(credentials.refresh_quotas(credential_id, user, db))
                    elif route == "status":
                        results.append(credentials.region_opt_status(credential_id, "us-east-1", user, db))
                    elif route == "create":
                        results.append(credentials.create_credential(CredentialCreate(
                            access_key_id="AKIAIOSFODNN8EXAMPLE", secret_access_key="proposed-secret-value"), user, db))
                    elif route == "update":
                        results.append(credentials.update_credential(credential_id, CredentialUpdate(
                            secret_access_key="proposed-secret-value"), user, db))
                    else:
                        results.append(credentials.validate_one(credential_id, user, db))
                except BaseException as exc:
                    errors.append(exc)
        with ExitStack() as stack:
            stack.enter_context(patch.object(aws_limits, "read_throttle", throttle))
            stack.enter_context(patch.object(Endpoint, "_send", new=simulated))
            transport = stack.enter_context(patch("botocore.httpsession.URLLib3Session.send",
                side_effect=AssertionError("AWS transport forbidden")))
            if route == "identity":
                stack.enter_context(patch.object(credentials.ls, "validate_credentials"))
            for _ in range(5):
                aws_limits._all.acquire()
            slots_held = True
            worker = threading.Thread(target=run)
            try:
                worker.start()
                self.wait_queued(throttle, worker, errors)
                with throttle.condition:
                    expected_scope = ((user_id, "credential", "proposed") if route == "create" else
                        (user_id, "credential", credential_id) if route == "update" else
                        (user_id, "account", "123456789012"))
                    self.assertEqual(set(throttle.turns), {expected_scope})
                with Session(engine) as db:
                    user, credential = db.get(User, user_id), db.get(AwsCredential, credential_id)
                    if mutation == "delete":
                        db.delete(credential)
                    elif mutation == "secret":
                        credential.secret_access_key_enc = encrypt_secret("rotated-secret")
                    elif mutation == "user_version":
                        user.token_version += 1
                    else:
                        foreign = User(username="foreign", password_hash="opaque")
                        db.add(foreign)
                        db.flush()
                        credential.user_id = foreign.id
                    db.commit()
                for _ in range(5):
                    aws_limits._all.release()
                slots_held = False
                throttle.settings_changed()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(sends, [])
                self.assertEqual(results, [])
                self.assertEqual(len(errors), 1, errors)
                self.assertIsInstance(errors[0], HTTPException)
                self.assertEqual(errors[0].status_code, 409)
                self.assertEqual(transport.call_count, 0)
            finally:
                if slots_held:
                    for _ in range(5):
                        aws_limits._all.release()
                    throttle.settings_changed()
                worker.join(5)

    def test_metrics_actual_queue_blocks_delete_rotation_and_user_revocation(self):
        for mutation in ("delete", "secret", "user_version", "owner"):
            with self.subTest(mutation=mutation):
                self.queue_then_change("metrics", mutation)

    def test_instance_actual_queue_blocks_revoked_credentials(self):
        self.queue_then_change("instance", "delete")

    def test_quota_actual_queue_blocks_rotation_without_local_publish(self):
        for mutation in ("delete", "secret", "user_version"):
            with self.subTest(mutation=mutation):
                self.queue_then_change("quotas", mutation)

    def test_region_status_actual_queue_does_not_downgrade_revocation_to_unknown(self):
        for mutation in ("delete", "secret", "user_version"):
            with self.subTest(mutation=mutation):
                self.queue_then_change("status", mutation)

    def test_manual_sts_actual_queue_rechecks_authorization(self):
        for mutation in ("delete", "secret", "user_version"):
            with self.subTest(mutation=mutation):
                self.queue_then_change("identity", mutation)

    def test_new_credential_validation_queue_checks_current_user_authorization(self):
        self.queue_then_change("create", "user_version")

    def test_replacement_key_validation_queue_preserves_original_version_fence(self):
        for mutation in ("delete", "secret", "user_version"):
            with self.subTest(mutation=mutation):
                self.queue_then_change("update", mutation)

    def test_metrics_sends_share_confirmed_account_context(self):
        engine, user_id, credential_id = self.fixture()
        sends = []
        class Raw:
            def stream(self, *_args, **_kwargs):
                yield b'{"metricData":[]}'
        def simulated(_endpoint, request):
            sends.append((_current.get().scope, len(_current.get().checks)))
            return AWSResponse(request.url, 200, {}, Raw())
        with Session(engine) as db, patch.object(aws_limits, "read_throttle", ReadThrottle(lambda: 0)), \
                patch.object(Endpoint, "_send", new=simulated), \
                patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("AWS transport forbidden")) as network:
            user = db.get(User, user_id)
            result = asyncio.run(metrics.metrics("us-east-1", "server", period="day", credential_id=credential_id,
                                                 user=user, db=db))
        self.assertEqual(result.points, [])
        self.assertEqual(sends, [((user_id, "account", "123456789012"), 1)] * 2)
        self.assertEqual(network.call_count, 0)

    def test_sdk_retry_rechecks_user_version_and_never_sends_second_attempt(self):
        engine, user_id, credential_id = self.fixture()
        sends = []
        class Raw:
            def stream(self, *_args, **_kwargs):
                yield b'{"__type":"ServiceException","message":"offline retry"}'
        def simulated(_endpoint, request):
            sends.append(_current.get().scope)
            with Session(engine) as other:
                other.get(User, user_id).token_version += 1
                other.commit()
            return AWSResponse(request.url, 500, {}, Raw())
        with Session(engine) as db, patch.object(aws_limits, "read_throttle", ReadThrottle(lambda: 0)), \
                patch.object(Endpoint, "_send", new=simulated), patch("botocore.endpoint.time.sleep"), \
                patch("botocore.httpsession.URLLib3Session.send", side_effect=AssertionError("AWS transport forbidden")) as network:
            user = db.get(User, user_id)
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(metrics.metrics("us-east-1", "server", period="day", credential_id=credential_id,
                                            user=user, db=db))
            self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(sends, [(user_id, "account", "123456789012")])
        self.assertEqual(network.call_count, 0)

    def test_manual_validation_can_clear_revocation_but_cannot_overwrite_rotated_version(self):
        from app.services.snapshots import scope_for
        engine, user_id, credential_id = self.fixture()
        with Session(engine) as db:
            user, credential = db.get(User, user_id), db.get(AwsCredential, credential_id)
            scope = scope_for(user, credential, "billing-identity", "revoked")
            db.add(DataSnapshot(**scope, error="STS invalid", error_status="credential_invalid"))
            db.commit()
            with patch.object(credentials.ls, "validate_credentials"), \
                    patch.object(credentials.billing_svc, "get_sts_identity", return_value="123456789012"), \
                    patch.object(credentials, "_apply_quota_to_cred"):
                result = credentials.validate_one(credential_id, user, db)
            self.assertEqual(result.account_id, "123456789012")
            self.assertEqual(db.scalars(select(DataSnapshot)).all(), [])
        original = CredentialReadGuard.reserve_write
        rotated = encrypt_secret("another-request-new-secret")
        def race(guard, db):
            with Session(engine) as other:
                other.get(AwsCredential, credential_id).secret_access_key_enc = rotated
                other.commit()
            return original(guard, db)
        with Session(engine) as db:
            user = db.get(User, user_id)
            with patch.object(credentials.ls, "validate_credentials"), \
                    patch.object(credentials.billing_svc, "get_sts_identity", return_value="210987654321"), \
                    patch.object(credentials, "_apply_quota_to_cred"), \
                    patch.object(CredentialReadGuard, "reserve_write", new=race):
                with self.assertRaises(HTTPException) as caught:
                    credentials.validate_one(credential_id, user, db)
                self.assertEqual(caught.exception.status_code, 409)
            db.rollback()
        with Session(engine) as db:
            credential = db.get(AwsCredential, credential_id)
            self.assertEqual(credential.secret_access_key_enc, rotated)
            self.assertEqual(credential.account_id, "123456789012")
