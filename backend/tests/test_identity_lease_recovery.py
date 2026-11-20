"""STS followers safely take over leases that expire after following starts."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
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

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AwsCredential, DataSnapshot, User
from app.security import encrypt_secret
from app.services import billing, identity_validation, snapshots
from app.services.read_throttle import read_context


class IdentityLeaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine("sqlite:///" + (Path(temporary.name) / "identity.db").as_posix(),
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        with Session(self.engine, expire_on_commit=False) as db:
            user = User(username="identity-recovery", password_hash="opaque")
            db.add(user)
            db.flush()
            credential = AwsCredential(user_id=user.id, account_id="123456789012",
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
                secret_access_key_enc=encrypt_secret("offline-secret"))
            db.add(credential)
            db.commit()
            self.user_id, self.credential_id = user.id, credential.id
            self.scope = identity_validation.identity_scope_for(user, credential)
            self.outer_scope = snapshots.scope_for(user, credential, "query-instances", "us-east-1")
        self.endpoint = patch("botocore.endpoint.Endpoint._send",
                              side_effect=AssertionError("AWS endpoint forbidden")).start()
        self.http = patch("botocore.httpsession.URLLib3Session.send",
                          side_effect=AssertionError("AWS HTTP transport forbidden")).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: self.assertEqual(self.endpoint.call_count, 0))
        self.addCleanup(lambda: self.assertEqual(self.http.call_count, 0))
        self.old_token = snapshots._read_or_claim(self.engine, self.scope, False)[1]

    def expire(self, mutate=None):
        with Session(self.engine) as db:
            row = db.get(DataSnapshot, self.scope["key"])
            row.lease_until = time.time() - 1
            if mutate is not None:
                mutate(db, row)
            db.commit()

    def follow_then(self, mutation, *, check=None, inherited=None, followers=1):
        followed = threading.Event()
        guard = threading.Lock()
        following_count = 0
        original = snapshots._read_or_claim
        def read_or_claim(engine, scope, force):
            nonlocal following_count
            view, token = original(engine, scope, force)
            if scope["key"] == self.scope["key"] and token is None and view.get("refreshing"):
                with guard:
                    following_count += 1
                    if following_count >= followers:
                        followed.set()
            return view, token
        def run():
            context = read_context((self.user_id, "account", "123456789012"), inherited) if inherited else nullcontext()
            with context:
                return asyncio.run(identity_validation.verify_identity(self.engine, self.scope,
                    "offline", "offline", None, "123456789012", wait=True, check=check))
        with ThreadPoolExecutor(max_workers=followers) as pool, \
                patch.object(snapshots, "_read_or_claim", side_effect=read_or_claim), \
                patch.object(billing, "get_sts_identity", return_value="123456789012") as sts:
            pending = [pool.submit(run) for _ in range(followers)]
            self.assertTrue(followed.wait(3), "STS waiters did not follow the existing active lease")
            mutation()
            outcomes = []
            for result in pending:
                try:
                    outcomes.append(result.result(timeout=5))
                except snapshots.SnapshotReadStopped as exc:
                    outcomes.append(exc)
            return outcomes, sts.call_count

    def test_lease_expires_after_following_reclaims_once_and_old_token_cannot_publish(self):
        results, calls = self.follow_then(self.expire)
        self.assertEqual(calls, 1)
        self.assertEqual(results[0]["data"], {"account_id": "123456789012"})
        self.assertEqual(results[0]["status"], "fresh")
        self.assertIsNone(snapshots._publish(self.engine, self.scope, self.old_token, 1800,
                                          data={"account_id": "210987654321"}))
        row, current = snapshots._row_current(self.engine, self.scope)
        self.assertTrue(current)
        self.assertIn("123456789012", row.payload)

    def test_concurrent_followers_share_one_protected_sts_replacement(self):
        results, calls = self.follow_then(self.expire, followers=4)
        self.assertEqual(calls, 1)
        self.assertTrue(all(result["data"] == {"account_id": "123456789012"} for result in results))

    def test_expired_lease_with_user_or_credential_version_change_is_not_reclaimed(self):
        for kind in ("credential", "user"):
            with self.subTest(kind=kind):
                if kind == "user":
                    # Start a new version-bound scope and orphaned lease.
                    with Session(self.engine) as db:
                        user, credential = db.get(User, self.user_id), db.get(AwsCredential, self.credential_id)
                        self.scope = identity_validation.identity_scope_for(user, credential)
                    self.old_token = snapshots._read_or_claim(self.engine, self.scope, False)[1]
                def mutate(db, _row):
                    if kind == "credential":
                        db.get(AwsCredential, self.credential_id).secret_access_key_enc = "rotated"
                    else:
                        db.get(User, self.user_id).token_version += 1
                results, calls = self.follow_then(lambda: self.expire(mutate))
                self.assertEqual(calls, 0)
                self.assertEqual(results[0]["status"], "invalidated")

    def test_confirmed_invalid_identity_never_restarts_sts(self):
        def invalidate():
            snapshots._publish(self.engine, self.scope, self.old_token, 1800, error="STS expired",
                               invalidate=True, invalidate_credential=True)
        results, calls = self.follow_then(invalidate)
        self.assertEqual(calls, 0)
        self.assertIsNone(results[0]["data"])
        self.assertEqual(results[0]["error_status"], "credential_invalid")

    def test_temporary_identity_failure_keeps_cooldown_without_new_attempt(self):
        results, calls = self.follow_then(lambda: snapshots._publish(self.engine, self.scope,
            self.old_token, 1800, error="STS unavailable", retry_after=120))
        self.assertEqual(calls, 0)
        self.assertEqual(results[0]["error"], "STS unavailable")
        self.assertTrue(results[0]["next_retry_at"])

    def test_cancelled_subscription_does_not_reclaim_or_write_failure_cooldown(self):
        cancelled = threading.Event()
        def check():
            if cancelled.is_set():
                raise snapshots.SnapshotReadStopped("subscription cancelled", status="cancelled")
        def cancel():
            cancelled.set()
            self.expire()
        results, calls = self.follow_then(cancel, check=check)
        self.assertEqual(calls, 0)
        self.assertIsInstance(results[0], snapshots.SnapshotReadStopped)
        row, _current = snapshots._row_current(self.engine, self.scope)
        self.assertEqual(row.lease_token, self.old_token)
        self.assertEqual(row.next_retry_at, 0)
        self.assertIsNone(row.error)

    def test_superseded_inherited_refresh_lease_blocks_identity_reclaim(self):
        outer_token = snapshots._read_or_claim(self.engine, self.outer_scope, False)[1]
        def inherited():
            snapshots.check_snapshot_read(self.engine, self.outer_scope, outer_token)
        def supersede():
            def mutate(db, _row):
                db.get(DataSnapshot, self.outer_scope["key"]).lease_token = "new-owner"
            self.expire(mutate)
        results, calls = self.follow_then(supersede, inherited=inherited)
        self.assertEqual(calls, 0)
        self.assertIsInstance(results[0], snapshots.SnapshotReadStopped)
        row, _current = snapshots._row_current(self.engine, self.scope)
        self.assertEqual(row.next_retry_at, 0)
        self.assertIsNone(row.error)

    def test_nonwaiting_identity_read_only_follows_existing_lease(self):
        with patch.object(billing, "get_sts_identity") as sts:
            result = asyncio.run(identity_validation.verify_identity(self.engine, self.scope,
                "offline", "offline", None, "123456789012", wait=False))
        self.assertTrue(result["refreshing"])
        sts.assert_not_called()
        row, _current = snapshots._row_current(self.engine, self.scope)
        self.assertEqual(row.lease_token, self.old_token)

    def forced_followers_with_slow_observer(self, pause_at):
        followed, eligible, release = threading.Event(), threading.Event(), threading.Event()
        names, attempts, observed = {}, {}, set()
        lock = threading.Lock()
        original_claim = snapshots._read_or_claim
        original_row = identity_validation._row_current
        original_states = snapshots._scope_states

        def claim(engine, scope, force):
            name = names[threading.get_ident()]
            attempts[name] = attempts.get(name, 0) + 1
            view, token = original_claim(engine, scope, force)
            if attempts[name] == 1 and token is None and view.get("refreshing"):
                with lock:
                    observed.add(name)
                    if len(observed) == 2:
                        followed.set()
            return view, token

        def row(engine, scope):
            name = names[threading.get_ident()]
            if name == "fast":
                self.assertTrue(eligible.wait(5), "slow follower never captured the old state")
            result = original_row(engine, scope)
            if name == "slow" and pause_at == "orphan":
                self.assertEqual(result[0].lease_token, self.old_token)
                self.assertIsNone(result[0].payload)
                eligible.set()
                self.assertTrue(release.wait(5))
            return result

        def states(db, scopes):
            result = original_states(db, scopes)
            name = names.get(threading.get_ident())
            if name == "slow" and pause_at == "claim" and attempts[name] == 2 and not eligible.is_set():
                self.assertEqual(result[1][self.scope["key"]].lease_token, self.old_token)
                eligible.set()
                self.assertTrue(release.wait(5))
            return result

        def run(name):
            names[threading.get_ident()] = name
            return asyncio.run(identity_validation.verify_identity(self.engine, self.scope,
                "offline", "offline", None, "123456789012", wait=True, force=True))

        with patch.object(snapshots, "_read_or_claim", side_effect=claim), \
                patch.object(identity_validation, "_row_current", side_effect=row), \
                patch.object(snapshots, "_scope_states", side_effect=states), \
                patch.object(billing, "get_sts_identity", return_value="123456789012") as sts, \
                ThreadPoolExecutor(max_workers=2) as pool:
            fast, slow = pool.submit(run, "fast"), pool.submit(run, "slow")
            try:
                self.assertTrue(followed.wait(3), "both force readers must first follow the live old lease")
                self.expire()
                first = fast.result(timeout=6)
                self.assertEqual(sts.call_count, 1)
            finally:
                release.set()
            second = slow.result(timeout=6)
            self.assertEqual(sts.call_count, 1)
        self.assertEqual(first["data"], {"account_id": "123456789012"})
        self.assertEqual(second["data"], first["data"])
        self.assertIsNone(snapshots._publish(self.engine, self.scope, self.old_token, 1800,
                                          data={"account_id": "210987654321"}))

    def test_forced_slow_orphan_observer_reuses_fast_followers_published_identity(self):
        self.forced_followers_with_slow_observer("orphan")

    def test_forced_slow_claim_uses_latest_payload_in_atomic_lease_condition(self):
        self.forced_followers_with_slow_observer("claim")
