"""Persistent refresh and concurrency regressions; no real AWS transport."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.database import Base
        from app.models import User, AwsCredential
        from app.security import encrypt_secret
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name).as_posix()}/app.db", connect_args={"check_same_thread": False})
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        with Session(self.engine) as db:
            user = User(username="cache-test", password_hash="opaque")
            db.add(user); db.flush()
            self.user_id = user.id
            cred = AwsCredential(user_id=user.id, account_id="123456789012", is_default=True,
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"), secret_access_key_enc=encrypt_secret("fake-secret"))
            db.add(cred); db.commit()
            from app.services.snapshots import scope_for
            self.scope = scope_for(user, cred, "test", "region:us-east-1")
        from app.services.snapshots import wait_for_refresh
        self.addCleanup(lambda: wait_for_refresh(self.scope["key"]))

    def test_hot_snapshot_and_concurrent_refresh_share_one_loader(self):
        from app.services.snapshots import read_snapshot
        calls = []
        async def load():
            calls.append(1)
            await asyncio.sleep(0.05)
            return {"value": "exact"}
        def read():
            return asyncio.run(read_snapshot(self.engine, self.scope, 60, load, wait=True))
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: read(), range(8)))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(row["data"] == {"value": "exact"} for row in results))
        self.assertEqual(read()["status"], "fresh")
        self.assertEqual(len(calls), 1)

    def test_transient_failure_preserves_old_success_permission_failure_drops_it(self):
        from app.services.snapshots import read_snapshot, SnapshotError
        async def ok(): return {"value": 3}
        first = asyncio.run(read_snapshot(self.engine, self.scope, 60, ok, wait=True))
        async def bad(): raise SnapshotError("temporary failure")
        stale = asyncio.run(read_snapshot(self.engine, self.scope, 60, bad, wait=True, force=True))
        self.assertEqual(stale["data"], first["data"])
        self.assertEqual(stale["last_success_at"], first["last_success_at"])
        self.assertEqual(stale["status"], "stale")
        async def denied(): raise SnapshotError("access denied", invalidate=True)
        with patch("app.services.snapshots.time.time", return_value=time.time() + 61):
            blocked = asyncio.run(read_snapshot(self.engine, self.scope, 60, denied, wait=True, force=True))
        self.assertIsNone(blocked["data"])
        self.assertEqual(blocked["status"], "error")

    def test_cold_get_returns_promptly_and_failed_load_finishes(self):
        from app.services.snapshots import read_snapshot, SnapshotError
        gate = threading.Event()
        async def bad():
            await asyncio.to_thread(gate.wait, 1)
            raise SnapshotError("cold failure")
        start = time.perf_counter()
        pending = asyncio.run(read_snapshot(self.engine, self.scope, 60, bad))
        self.assertLess(time.perf_counter() - start, 0.2)
        self.assertEqual(pending["status"], "refreshing")
        gate.set()
        result = asyncio.run(read_snapshot(self.engine, self.scope, 60, bad, wait=True))
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["refreshing"])

    def test_changed_secret_during_refresh_cannot_publish_old_result(self):
        from app.models import AwsCredential
        from sqlalchemy.orm import Session
        from app.services.snapshots import read_snapshot
        gate = threading.Event()
        async def load():
            await asyncio.to_thread(gate.wait, 2)
            return {"secret_scope": "old"}
        asyncio.run(read_snapshot(self.engine, self.scope, 60, load))
        with Session(self.engine) as db:
            cred = db.get(AwsCredential, self.scope["credential_id"])
            cred.secret_access_key_enc = "changed-ciphertext"
            db.commit()
        gate.set()
        result = asyncio.run(read_snapshot(self.engine, self.scope, 60, load, wait=True))
        self.assertIsNone(result["data"])
        self.assertEqual(result["status"], "invalidated")

    def test_initial_dependency_follows_fast_result_but_bounds_slow_work(self):
        from app.services.snapshots import read_snapshot, initial_dependency
        async def quick():
            await asyncio.sleep(0.02)
            return {'ready': True}
        async def follow():
            view = await read_snapshot(self.engine, self.scope, 60, quick)
            return await initial_dependency(self.engine, self.scope, view)
        self.assertEqual(asyncio.run(follow())['data'], {'ready': True})
        gate = threading.Event()
        async def slow():
            await asyncio.to_thread(gate.wait, 2)
            return {'ready': False}
        async def bounded():
            view = await read_snapshot(self.engine, self.scope, 60, slow, force=True)
            # An existing snapshot never waits, even if it is refreshing.
            started = time.perf_counter()
            previous = await initial_dependency(self.engine, self.scope, view)
            self.assertLess(time.perf_counter() - started, 0.05)
            self.assertEqual(previous['data'], {'ready': True})
            cold = {**view, 'data': None}
            started = time.perf_counter()
            # Use a scope without a previously committed value for the bound.
            other = {**self.scope, 'key': 'c' * 64, 'scope': 'slow-dependency'}
            cold = await read_snapshot(self.engine, other, 60, slow)
            result = await initial_dependency(self.engine, other, cold, budget=0.05)
            self.assertLess(time.perf_counter() - started, 0.2)
            self.assertTrue(result['refreshing'])
            self.assertIsNone(result['data'])
        try:
            asyncio.run(bounded())
        finally:
            gate.set()
            from app.services.snapshots import wait_for_refresh
            wait_for_refresh('c' * 64)

    def test_initial_dependency_rechecks_credential_identity(self):
        from app.models import AwsCredential
        from sqlalchemy.orm import Session
        from app.services.snapshots import read_snapshot, initial_dependency
        gate = threading.Event()
        async def load():
            await asyncio.to_thread(gate.wait, 2)
            return {'obsolete': True}
        async def follow():
            view = await read_snapshot(self.engine, self.scope, 60, load)
            with Session(self.engine) as db:
                db.get(AwsCredential, self.scope['credential_id']).account_id = '999999999999'
                db.commit()
            result = await initial_dependency(self.engine, self.scope, view)
            self.assertEqual(result['status'], 'invalidated')
            self.assertIsNone(result['data'])
        try:
            asyncio.run(follow())
        finally:
            gate.set()

    def test_payload_budget_evicts_old_values_and_rejects_oversized_payload(self):
        from sqlalchemy.orm import Session
        from sqlalchemy import func, select
        from app.models import DataSnapshot
        from app.services.snapshots import read_snapshot
        second = {**self.scope, "key": "b" * 64, "scope": "second"}
        async def first(): return {"text": "a" * 70}
        async def later(): return {"text": "b" * 70}
        with patch("app.services.snapshots.MAX_PAYLOAD_BYTES", 120):
            asyncio.run(read_snapshot(self.engine, self.scope, 60, first, wait=True))
            asyncio.run(read_snapshot(self.engine, second, 60, later, wait=True))
            with Session(self.engine) as db:
                self.assertLessEqual(db.scalar(select(func.sum(DataSnapshot.payload_bytes))), 120)
                self.assertIsNone(db.get(DataSnapshot, self.scope["key"]).payload)
            async def huge(): return {"text": "c" * 200}
            result = asyncio.run(read_snapshot(self.engine, second, 60, huge, wait=True, force=True))
            self.assertEqual(result["status"], "stale")
            self.assertIn("64 MiB", result["error"])

    def test_expired_refresh_lease_can_recover_after_restart_and_cache_is_persistent(self):
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot
        from app.services.snapshots import read_snapshot
        async def ok(): return {"value": "persisted"}
        asyncio.run(read_snapshot(self.engine, self.scope, 60, ok, wait=True))
        self.engine.dispose()
        hot = asyncio.run(read_snapshot(self.engine, self.scope, 60, ok, wait=True))
        self.assertEqual(hot["data"], {"value": "persisted"})
        with Session(self.engine) as db:
            row = db.get(DataSnapshot, self.scope["key"])
            row.expires_at = 0; row.lease_token = "dead-process"; row.lease_until = time.time() - 1
            db.commit()
        recovered = asyncio.run(read_snapshot(self.engine, self.scope, 60, ok, wait=True))
        self.assertEqual(recovered["status"], "fresh")

    def test_different_user_account_and_proxy_versions_have_distinct_keys(self):
        from sqlalchemy.orm import Session
        from app.models import AwsCredential, User
        from app.services.snapshots import scope_for
        from app.security import encrypt_secret
        with Session(self.engine) as db:
            user = db.get(User, self.user_id)
            credential = db.get(AwsCredential, self.scope["credential_id"])
            original = scope_for(user, credential, "test", "region:us-east-1")
            credential.proxy_url_enc = encrypt_secret("http://proxy.example.invalid:8080")
            changed_proxy = scope_for(user, credential, "test", "region:us-east-1")
            credential.account_id = "210987654321"
            changed_account = scope_for(user, credential, "test", "region:us-east-1")
            user.token_version += 1
            changed_user_version = scope_for(user, credential, "test", "region:us-east-1")
            self.assertEqual(len({row["key"] for row in (original, changed_proxy, changed_account, changed_user_version)}), 4)

    def test_ready_scopes_share_publish_transaction_without_waiting_for_slow_scope(self):
        from sqlalchemy import event
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot
        from app.services.snapshots import read_many
        scopes = [{**self.scope, "key": str(index) * 64, "scope": f"batch-{index}"} for index in range(3)]
        writes = []
        def count(_connection, _cursor, statement, *_args):
            if statement == "BEGIN IMMEDIATE":
                writes.append(statement)
        event.listen(self.engine, "before_cursor_execute", count)
        self.addCleanup(lambda: event.remove(self.engine, "before_cursor_execute", count))
        async def scenario():
            slow_gate = asyncio.Event()
            fast_ready = asyncio.Event()
            fast_gate = asyncio.Event()
            fast_started = 0
            async def fast():
                nonlocal fast_started
                fast_started += 1
                if fast_started == 2:
                    fast_ready.set()
                await fast_gate.wait()
                return {"value": "ready"}
            async def slow():
                await slow_gate.wait()
                return {"value": "later"}
            task = asyncio.create_task(read_many(self.engine,
                [(scope, 60, fast if index < 2 else slow) for index, scope in enumerate(scopes)], wait=True))
            try:
                await asyncio.wait_for(fast_ready.wait(), timeout=5)
                fast_gate.set()
                for _ in range(200):
                    with Session(self.engine) as db:
                        if all((row := db.get(DataSnapshot, scope["key"])) is not None and row.payload
                               for scope in scopes[:2]):
                            self.assertIsNone(db.get(DataSnapshot, scopes[2]["key"]).payload)
                            self.assertEqual(len(writes), 1)
                            break
                    await asyncio.sleep(0.005)
                else:
                    self.fail("Ready scopes were blocked by slow scope")
            finally:
                fast_gate.set()
                slow_gate.set()
                result = await task
            self.assertEqual([row["data"]["value"] for row in result], ["ready", "ready", "later"])
        asyncio.run(scenario())

    def test_publish_batch_rechecks_each_lease_and_keeps_payload_budget(self):
        from sqlalchemy import select, func
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot, AwsCredential
        from app.services.snapshots import _read_or_claim, _publish_batch, _publish_ready, read_snapshot
        scopes = [{**self.scope, "key": str(index + 3) * 64, "scope": f"publish-{index}"} for index in range(3)]
        tokens = [_read_or_claim(self.engine, scope, False)[1] for scope in scopes]
        with Session(self.engine) as db:
            revoked = db.get(DataSnapshot, scopes[1]["key"])
            revoked.lease_token = None; revoked.lease_until = 0
            db.commit()
        with patch("app.services.snapshots.MAX_PAYLOAD_BYTES", 120):
            _publish_batch(self.engine, [(scope, token, 60, {"data": {"text": "a" * 70}})
                                         for scope, token in zip(scopes, tokens)])
            with Session(self.engine) as db:
                self.assertLessEqual(db.scalar(select(func.sum(DataSnapshot.payload_bytes))), 120)
                self.assertIsNone(db.get(DataSnapshot, scopes[1]["key"]).payload)
                self.assertEqual(sum(row.payload is not None for row in db.query(DataSnapshot).all()), 1)
        async def good(): return {"value": "valid"}
        asyncio.run(read_snapshot(self.engine, scopes[0], 60, good, wait=True, force=True))
        token = _read_or_claim(self.engine, scopes[0], True)[1]
        async def malformed():
            await _publish_ready(self.engine, scopes[0], token, 60, data={"value": object()})
        asyncio.run(malformed())
        with Session(self.engine) as db:
            row = db.get(DataSnapshot, scopes[0]["key"])
            self.assertIn('"valid"', row.payload)
            self.assertTrue(row.error)
        with patch("app.services.snapshots.time.time", return_value=time.time() + 61):
            tokens = [_read_or_claim(self.engine, scope, True)[1] for scope in scopes]
        with Session(self.engine) as db:
            db.get(AwsCredential, self.scope["credential_id"]).secret_access_key_enc = "rotated"
            db.commit()
        result = _publish_batch(self.engine, [(scope, token, 60, {"data": {"value": "old"}})
                                             for scope, token in zip(scopes, tokens)])
        self.assertEqual(result, [None, None, None])
        with Session(self.engine) as db:
            self.assertEqual(db.query(DataSnapshot).count(), 0)

    def test_batch_region_permission_failure_does_not_clear_successful_region(self):
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot
        from app.services.snapshots import _read_or_claim, _publish_batch
        scopes = [{**self.scope, "key": str(index + 5) * 64, "scope": region}
                  for index, region in enumerate(("us-east-1", "eu-west-1"))]
        tokens = [_read_or_claim(self.engine, scope, False)[1] for scope in scopes]
        outputs = _publish_batch(self.engine, [
            (scopes[0], tokens[0], 60, {"data": {"instances": ["healthy"]}}),
            (scopes[1], tokens[1], 60, {"error": "AccessDenied", "invalidate": True,
                                        "error_status": "no_permission"}),
        ])
        self.assertIsNotNone(outputs[0])
        self.assertIsNone(outputs[1])
        with Session(self.engine) as db:
            healthy, denied = (db.get(DataSnapshot, scope["key"]) for scope in scopes)
            self.assertEqual(healthy.scope, "us-east-1")
            self.assertEqual(healthy.payload_bytes, len(healthy.payload.encode()))
            self.assertIn("healthy", healthy.payload)
            self.assertIsNone(healthy.error)
            self.assertIsNone(denied.payload)
            self.assertEqual(denied.error_status, "no_permission")
            self.assertEqual(denied.error, "AccessDenied")

    def test_batch_credential_failure_clears_and_suppresses_sibling_results(self):
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot
        from app.services.snapshots import _read_or_claim, _publish_batch
        for index, results in enumerate((
            ({"data": {"instances": ["stale"]}},
             {"error": "ExpiredToken", "invalidate": True, "invalidate_credential": True}),
            ({"error": "ExpiredToken", "invalidate": True, "invalidate_credential": True},
             {"data": {"instances": ["stale"]}}),
        )):
            scopes = [{**self.scope, "key": f"wide-{index}-{region}".ljust(64, "x"), "scope": region}
                      for region in ("us-east-1", "eu-west-1")]
            tokens = [_read_or_claim(self.engine, scope, False)[1] for scope in scopes]
            outputs = _publish_batch(self.engine, [
                (scope, token, 60, result)
                for scope, token, result in zip(scopes, tokens, results)
            ])
            self.assertEqual(outputs, [None, None])
            with Session(self.engine) as db:
                self.assertTrue(all(db.get(DataSnapshot, scope["key"]).payload is None for scope in scopes))

    def test_revoked_auth_lease_cannot_suppress_valid_sibling_publish(self):
        from sqlalchemy.orm import Session
        from app.models import DataSnapshot
        from app.services.snapshots import _read_or_claim, _publish_batch
        scopes = [{**self.scope, "key": f"revoked-{region}".ljust(64, "x"), "scope": region}
                  for region in ("us-east-1", "eu-west-1")]
        tokens = [_read_or_claim(self.engine, scope, False)[1] for scope in scopes]
        with Session(self.engine) as db:
            row = db.get(DataSnapshot, scopes[0]["key"])
            row.lease_token = "new-owner-token"
            row.lease_until = time.time() + 60
            db.commit()
        outputs = _publish_batch(self.engine, [
            (scopes[0], tokens[0], 60, {"error": "ExpiredToken", "invalidate": True,
                                        "invalidate_credential": True}),
            (scopes[1], tokens[1], 60, {"data": {"instances": ["owned-by-current-lease"]}}),
        ])
        self.assertIsNone(outputs[0])
        self.assertIsNotNone(outputs[1])
        with Session(self.engine) as db:
            stale, valid = (db.get(DataSnapshot, scope["key"]) for scope in scopes)
            self.assertEqual(stale.lease_token, "new-owner-token")
            self.assertIsNone(stale.error)
            self.assertIn("owned-by-current-lease", valid.payload)


class AwsLimitTests(unittest.TestCase):
    def test_all_clients_share_five_calls_two_mutations_and_one_same_instance(self):
        from app.services.aws_limits import call_with_limits
        active, peak = 0, 0
        guard = threading.Lock()
        def operation(_name, _params):
            nonlocal active, peak
            with guard:
                active += 1; peak = max(peak, active)
            time.sleep(0.025)
            with guard: active -= 1
        for mutation, same, cap in ((False, False, 5), (True, False, 2), (True, True, 1)):
            active, peak = 0, 0
            with ThreadPoolExecutor(max_workers=16) as pool:
                list(pool.map(lambda index: call_with_limits(operation,
                    "StopInstance" if mutation else "GetInstances",
                    {"instanceName": "same" if same else str(index)}, "region"), range(16)))
            self.assertLessEqual(peak, cap)
            self.assertEqual(peak, cap)


class CacheRouteTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.database import Base, get_db
        from app.deps import get_current_user
        from app.main import app
        from app.models import AwsCredential, User
        from app.security import encrypt_secret
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name).as_posix()}/app.db", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.addCleanup(self.db.close)
        self.user = User(username="t04-route", password_hash="opaque")
        other = User(username="t04-foreign", password_hash="opaque")
        self.db.add_all([self.user, other]); self.db.flush()
        def make(user, account):
            cred = AwsCredential(user_id=user.id, account_id=account,
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"), secret_access_key_enc=encrypt_secret("fixed-fake-secret"))
            self.db.add(cred); self.db.flush(); return cred
        self.credential = make(self.user, "123456789012")
        self.foreign = make(other, "123456789012")
        self.db.commit()
        previous = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(previous)))
        self.client = TestClient(app)
        self.addCleanup(self.finish_workers)

    def finish_workers(self):
        from app.models import DataSnapshot
        from app.services.snapshots import wait_for_refresh
        from sqlalchemy.orm import Session
        with Session(self.engine) as db:
            keys = [row.key for row in db.query(DataSnapshot).all()]
        for key in keys:
            wait_for_refresh(key)

    def test_instance_hot_cache_avoids_aws_and_slow_region_does_not_hide_success(self):
        from app.services import lightsail as ls
        gate = threading.Event()
        def instances(_ak, _sk, region, **_kwargs):
            if region == "us-west-2": gate.wait(3)
            if region == "eu-west-1": raise ls.LightsailError("region failed")
            return [{"name": "server-" + region, "region": region, "state": "running"}]
        with patch.object(ls, "get_regions", return_value=[{"name": "us-east-1"}, {"name": "us-west-2"}, {"name": "eu-west-1"}]), \
             patch.object(ls, "list_instances_in_region", side_effect=instances) as calls:
            # Warm only the region directory; instance payloads are still cold.
            self.client.get(f"/api/catalog/regions?credential_id={self.credential.id}")
            started = time.perf_counter()
            response = self.client.get("/api/v2/instances")
            self.assertLess(time.perf_counter() - started, 1.0)
            for _ in range(40):
                response = self.client.get("/api/v2/instances")
                if response.json()["instances"]:
                    break
                time.sleep(0.01)
            self.assertEqual([row["name"] for row in response.json()["instances"]], ["server-us-east-1"])
            self.assertTrue(response.json()["refreshing"])
            gate.set(); self.finish_workers()
            response = self.client.get("/api/v2/instances")
            self.assertEqual(len(response.json()["instances"]), 2)
            before = calls.call_count
            hot = self.client.get("/api/v2/instances")
            self.assertEqual(hot.status_code, 200, hot.text)
            self.assertEqual(calls.call_count, before)

    def test_actual_instances_route_rechecks_views_after_late_credential_invalidation(self):
        from app.services import billing
        from botocore.exceptions import ClientError
        from app.models import DataSnapshot
        from app.services import lightsail as ls
        from sqlalchemy import update
        from sqlalchemy.orm import Session
        from botocore.endpoint import Endpoint
        regions = [{"name": "us-east-1"}, {"name": "eu-west-1"}]
        with patch.object(ls, "get_regions", return_value=regions), \
             patch.object(ls, "list_instances_in_region", side_effect=lambda _ak, _sk, region, **_kw: [
                 {"name": "warm-" + region, "region": region, "state": "running"}]), \
             patch.object(Endpoint, "_send", side_effect=AssertionError("AWS transport forbidden")):
            catalog = self.client.get(f"/api/catalog/regions?credential_id={self.credential.id}")
            self.assertEqual(catalog.status_code, 200, catalog.text)
            warm = self.client.get("/api/v2/instances?wait=true")
            self.assertEqual(warm.status_code, 200, warm.text)
            self.assertEqual(len(warm.json()["instances"]), 2)

            self.db.execute(update(DataSnapshot).where(DataSnapshot.kind == "instances").values(expires_at=0))
            self.db.commit()
            def load(_ak, _sk, region, **_kwargs):
                if region == "us-east-1":
                    return [{"name": "fresh-us-east-1", "region": region, "state": "running"}]
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with Session(self.engine) as db:
                        row = db.query(DataSnapshot).filter(DataSnapshot.kind == "instances",
                            DataSnapshot.scope == "us-east-1").first()
                        if row and row.payload and "fresh-us-east-1" in row.payload and row.expires_at > time.time():
                            break
                    time.sleep(0.005)
                else:
                    raise AssertionError("fast region was not committed before the credential failure")
                raise ls.LightsailError("expired credential", code="ExpiredToken")
            with patch.object(ls, "get_regions", return_value=regions), \
                 patch.object(ls, "list_instances_in_region", side_effect=load), \
                 patch.object(billing, "get_sts_identity", side_effect=ClientError(
                     {"Error": {"Code": "ExpiredToken"}}, "GetCallerIdentity")), \
                 patch.object(Endpoint, "_send", side_effect=AssertionError("AWS transport forbidden")) as transport:
                response = self.client.get("/api/v2/instances?wait=true")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("fresh-us-east-1", [row["name"] for row in response.json()["instances"]])
            self.assertTrue(all(scope.get("error_status") == "credential_invalid"
                                for scope in response.json()["scopes"]), response.json())
            self.assertFalse(response.json()["refreshing"])
            self.assertEqual(transport.call_count, 0)
            self.db.expire_all()
            rows = self.db.query(DataSnapshot).filter(DataSnapshot.credential_id == self.credential.id).all()
            self.assertTrue(rows)
            self.assertTrue(all(row.payload is None and row.last_success is None for row in rows))

    def test_outer_credential_gather_revalidation_keeps_healthy_credential(self):
        from app.services import billing
        from botocore.exceptions import ClientError
        from app.models import AwsCredential, DataSnapshot
        from app.security import encrypt_secret
        from app.services import lightsail as ls
        from sqlalchemy import update
        from sqlalchemy.orm import Session
        from botocore.endpoint import Endpoint
        other = AwsCredential(user_id=self.user.id, account_id="210987654321",
            access_key_id_enc=encrypt_secret("AKIAIOSFODNN7OTHER"),
            secret_access_key_enc=encrypt_secret("other-fake-secret"))
        self.db.add(other); self.db.flush()
        self.db.commit(); self.db.refresh(other)
        regions = [{"name": "us-east-1"}, {"name": "eu-west-1"}]
        with patch.object(ls, "get_regions", return_value=regions), \
             patch.object(ls, "list_instances_in_region", side_effect=lambda _ak, _sk, region, **_kw: [
                 {"name": "warm-" + region, "region": region, "state": "running"}]), \
             patch.object(Endpoint, "_send", side_effect=AssertionError("AWS transport forbidden")):
            self.client.get(f"/api/catalog/regions?credential_id={self.credential.id}")
            self.client.get(f"/api/catalog/regions?credential_id={other.id}")
            warm = self.client.get("/api/v2/instances?wait=true")
            self.assertEqual(warm.status_code, 200, warm.text)
            self.assertEqual(len(warm.json()["instances"]), 4)
            self.db.execute(update(DataSnapshot).where(DataSnapshot.credential_id == self.credential.id,
                DataSnapshot.kind == "instances").values(expires_at=0)); self.db.commit()
            def load(ak, _sk, region, **_kwargs):
                if ak == "AKIAIOSFODNN7EXAMPLE" and region == "us-east-1":
                    return [{"name": "fresh-primary", "region": region, "state": "running"}]
                if ak == "AKIAIOSFODNN7EXAMPLE" and region == "eu-west-1":
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with Session(self.engine) as db:
                            row = db.query(DataSnapshot).filter(DataSnapshot.credential_id == self.credential.id,
                                DataSnapshot.kind == "instances", DataSnapshot.scope == "us-east-1").first()
                            if row and row.payload and "fresh-primary" in row.payload and row.expires_at > time.time():
                                break
                        time.sleep(0.005)
                    else:
                        raise AssertionError("primary fast scope was not committed")
                    raise ls.LightsailError("expired primary credential", code="ExpiredToken")
                return [{"name": "healthy-secondary-" + region, "region": region, "state": "running"}]
            with patch.object(ls, "get_regions", return_value=regions), \
                 patch.object(ls, "list_instances_in_region", side_effect=load), \
                 patch.object(billing, "get_sts_identity", side_effect=ClientError(
                     {"Error": {"Code": "ExpiredToken"}}, "GetCallerIdentity")), \
                 patch.object(Endpoint, "_send", side_effect=AssertionError("AWS transport forbidden")) as transport:
                response = self.client.get("/api/v2/instances?wait=true")
            self.assertEqual(response.status_code, 200, response.text)
            instances = response.json()["instances"]
            self.assertEqual({row["name"] for row in instances},
                             {"warm-us-east-1", "warm-eu-west-1"})
            self.assertEqual({row["credential_id"] for row in instances}, {other.id})
            self.assertEqual({scope["credential_id"] for scope in response.json()["scopes"]
                              if scope.get("error_status") == "credential_invalid"}, {self.credential.id})
            self.assertEqual(transport.call_count, 0)
            self.db.expire_all()
            primary = self.db.query(DataSnapshot).filter(DataSnapshot.credential_id == self.credential.id).all()
            secondary = self.db.query(DataSnapshot).filter(DataSnapshot.credential_id == other.id).all()
            self.assertTrue(all(row.payload is None for row in primary))
            self.assertEqual(len([row for row in secondary if row.payload is not None]), 3)

    def test_billing_hot_cache_and_independent_failure_preserve_last_success(self):
        from app.services import billing as b
        from app.models import DataSnapshot
        from sqlalchemy import update
        costs = {"year_month": "2026-11", "by_service": [], "currencies": [{"currency": "USD",
            "total_cost": "10", "net_cost": "8", "credits_applied": "2", "refunds": "0", "discounts": "0", "lightsail_cost": "10"}]}
        with patch.object(b, "get_sts_identity", return_value="123456789012") as identity, \
             patch.object(b, "make_ce_client"), patch.object(b, "get_billing_summary", return_value=costs) as cost_calls, \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}) as forecast, \
             patch.object(b, "get_aws_credits", return_value={"credits": []}) as credits:
            cold = self.client.get("/api/v2/billing/summary?wait=true")
            self.assertEqual(cold.status_code, 200, cold.text)
            counts = (identity.call_count, cost_calls.call_count, forecast.call_count, credits.call_count)
            hot = self.client.get("/api/v2/billing/summary")
            self.assertEqual(hot.json()["currencies"][0]["net_cost"], "8")
            self.assertEqual((identity.call_count, cost_calls.call_count, forecast.call_count, credits.call_count), counts)
            old_success = hot.json()["accounts"][0]["costs"]["cache"]["last_success_at"]
            self.db.execute(update(DataSnapshot).where(DataSnapshot.kind == "billing-costs").values(expires_at=0)); self.db.commit()
            cost_calls.side_effect = b.BillingUnavailableError("temporary")
            stale = self.client.get("/api/v2/billing/summary")
            self.finish_workers()
            stale = self.client.get("/api/v2/billing/summary").json()["accounts"][0]
            self.assertEqual(stale["costs"]["cache"]["status"], "stale")
            self.assertEqual(stale["costs"]["cache"]["last_success_at"], old_success)
            self.assertEqual(stale["forecast"]["status"], "ok")
            self.assertEqual(stale["credits"]["status"], "ok")
        forbidden = self.client.post("/api/v2/refresh", json={"kind": "billing", "credential_id": self.foreign.id})
        self.assertEqual(forbidden.status_code, 404, forbidden.text)

    def test_cost_explorer_expired_token_with_valid_sts_preserves_other_sections(self):
        from botocore.exceptions import ClientError
        from sqlalchemy.orm import Session
        from app.services import billing as b
        from app.services.billing_cache import cached_account
        from app.models import DataSnapshot
        costs = {"year_month": "2026-11", "by_service": [], "currencies": [{"currency": "USD",
            "total_cost": "10", "net_cost": "8", "credits_applied": "2", "refunds": "0", "discounts": "0", "lightsail_cost": "10"}]}
        with patch.object(b, "get_sts_identity", return_value="123456789012"), \
             patch.object(b, "make_ce_client"), patch.object(b, "get_billing_summary", return_value=costs), \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}), \
             patch.object(b, "get_aws_credits", return_value={"credits": []}):
            self.client.get("/api/v2/billing/summary?wait=true")
        client = type("CEClient", (), {"get_cost_and_usage": lambda _self, **_kwargs: (_ for _ in ()).throw(
            ClientError({"Error": {"Code": "ExpiredToken", "Message": "expired"}}, "GetCostAndUsage"))})()
        with patch.object(b, "get_sts_identity", return_value="123456789012"), \
             patch.object(b, "make_ce_client", return_value=client), \
             patch.object(b, "get_aws_credits", return_value={"credits": []}), \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}):
            result = asyncio.run(cached_account(self.engine, self.user, self.credential, wait=True, force=True))
        self.db.expire_all()
        rows = self.db.query(DataSnapshot).filter(DataSnapshot.credential_id == self.credential.id).all()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row.payload is not None for row in rows if row.kind != "billing-costs"))
        self.assertIsNone(next(row for row in rows if row.kind == "billing-costs").payload)
        self.assertNotEqual(result["costs"]["status"], "ok")
        self.assertEqual(result["forecast"]["status"], "ok")
        self.assertEqual(result["credits"]["status"], "ok")

    def test_billing_summary_outer_account_gather_revalidates_invalidated_account(self):
        from app.models import AwsCredential, DataSnapshot
        from app.security import encrypt_secret
        from app.services import billing as b
        from app.services import billing_cache
        from sqlalchemy.orm import Session
        from botocore.endpoint import Endpoint
        completed = threading.Event()
        other = AwsCredential(user_id=self.user.id, account_id="210987654321",
            access_key_id_enc=encrypt_secret("AKIAIOSFODNN7OTHER"),
            secret_access_key_enc=encrypt_secret("other-fake-secret"))
        self.db.add(other); self.db.commit(); self.db.refresh(other)
        original = billing_cache.cached_account
        first_credential_id = self.credential.id
        async def observed(engine, user, credential, *args, **kwargs):
            result = await original(engine, user, credential, *args, **kwargs)
            if credential.id == first_credential_id:
                completed.set()
            return result
        def sts(_ak, _sk, _proxy=None): return "123456789012" if _ak == "AKIAIOSFODNN7EXAMPLE" else "210987654321"
        def load_costs(_client, account_id=None, **_kwargs):
            value = "8" if account_id == "123456789012" else "4"
            if account_id == "210987654321":
                if not completed.wait(5): raise AssertionError("primary cached_account did not complete")
                from app.services.snapshots import SnapshotError, read_snapshot, scope_for
                async def expired_identity():
                    raise SnapshotError("ExpiredToken", invalidate=True, invalidate_credential=True)
                identity_scope = scope_for(self.user, self.credential, "billing-identity",
                                           b._today_utc().isoformat())
                asyncio.run(read_snapshot(self.engine, identity_scope, 1800, expired_identity,
                                          wait=True, force=True))
            return {"year_month": "2026-11", "by_service": [], "currencies": [{"currency": "USD",
                "total_cost": value, "net_cost": value, "credits_applied": "0", "refunds": "0",
                "discounts": "0", "lightsail_cost": value}]}
        with patch.object(billing_cache, "cached_account", side_effect=observed), \
             patch.object(b, "get_sts_identity", side_effect=sts), \
             patch.object(b, "make_ce_client", side_effect=lambda *_args: object()), \
             patch.object(b, "get_billing_summary", side_effect=load_costs), \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}), \
             patch.object(b, "get_aws_credits", return_value={"credits": []}), \
             patch.object(Endpoint, "_send", side_effect=AssertionError("AWS transport forbidden")) as transport:
            response = self.client.get("/api/v2/billing/summary?wait=true")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(completed.is_set())
        self.assertEqual({account["account_id"] for account in body["accounts"]
                          if account["costs"]["status"] == "ok"}, {"210987654321"}, body)
        self.assertEqual([currency["net_cost"] for currency in body["currencies"]
                          if currency["net_cost"] is not None], ["4"])
        self.assertEqual(transport.call_count, 0)

    def test_invalid_region_creates_no_scope_or_aws_call_and_valid_region_reuses_cache(self):
        from app.models import DataSnapshot
        from app.services import lightsail as ls
        for region in ("moon-1", "x" * 100, "https://127.0.0.1"):
            with patch.object(ls, "get_regions") as directory, patch.object(ls, "list_instances_in_region") as listing:
                read = self.client.get("/api/v2/instances", params={"region": region})
                refresh = self.client.post("/api/v2/refresh", json={"kind": "instances", "region": region})
                self.assertIn(read.status_code, (400, 422), read.text)
                self.assertIn(refresh.status_code, (400, 422), refresh.text)
                directory.assert_not_called(); listing.assert_not_called()
                self.assertEqual(self.db.query(DataSnapshot).count(), 0)
        with patch.object(ls, "get_regions", return_value=[{"name": "us-east-1"}]), \
             patch.object(ls, "list_instances_in_region", return_value=[{
                 "name": "fixed", "region": "us-east-1", "state": "running"}]) as listing:
            bad_scope = self.client.get("/api/v2/instances?region=eu-west-1&wait=true")
            self.assertEqual(bad_scope.status_code, 400, bad_scope.text)
            listing.assert_not_called()
            response = self.client.get("/api/v2/instances?region=us-east-1&wait=true")
            self.assertEqual(response.status_code, 200, response.text)
            hot = self.client.get("/api/v2/instances?region=us-east-1")
            self.assertEqual(hot.json()["instances"][0]["name"], "fixed")
            listing.assert_called_once()
            scopes = self.db.query(DataSnapshot.scope).filter(DataSnapshot.kind == "instances").all()
            self.assertEqual(scopes, [("us-east-1",)])

    def test_temporary_identity_failure_preserves_all_stale_billing_sections(self):
        from app.services import billing as b
        from app.models import DataSnapshot
        from sqlalchemy import update
        gate = threading.Event()
        self.addCleanup(gate.set)
        costs = {"year_month": "2026-11", "by_service": [], "currencies": [{"currency": "USD",
            "total_cost": "10", "net_cost": "8", "credits_applied": "2", "refunds": "0", "discounts": "0", "lightsail_cost": "10"}]}
        def outage(*_args):
            gate.wait(3)
            raise b.BillingUnavailableError("temporary STS outage")
        with patch.object(b, "get_sts_identity", return_value="123456789012") as identity, \
             patch.object(b, "make_ce_client"), patch.object(b, "get_billing_summary", return_value=costs) as cost_calls, \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}) as forecast, \
             patch.object(b, "get_aws_credits", return_value={"credits": []}) as credits:
            warm = self.client.get("/api/v2/billing/summary?wait=true").json()
            success = {name: warm["accounts"][0][name]["cache"]["last_success_at"]
                       for name in ("costs", "forecast", "credits")}
            self.db.execute(update(DataSnapshot).values(expires_at=0)); self.db.commit()
            identity.side_effect = outage
            pending = self.client.get("/api/v2/billing/summary").json()
            self.assertTrue(pending["refreshing"])
            gate.set(); self.finish_workers()
            after = self.client.get("/api/v2/billing/summary").json()
            self.assertEqual(after["currencies"][0]["net_cost"], "8")
            self.assertFalse(after["refreshing"])
            for name in ("costs", "forecast", "credits"):
                cache = after["accounts"][0][name]["cache"]
                self.assertTrue(cache["stale"])
                self.assertEqual(cache["last_success_at"], success[name])
                self.assertTrue(cache["error"])
            self.assertEqual((cost_calls.call_count, forecast.call_count, credits.call_count), (1, 1, 1))

    def test_raw_sts_authentication_failure_invalidates_all_credential_snapshots(self):
        from app.services import billing as b
        from app.models import DataSnapshot
        from sqlalchemy import update
        from botocore.exceptions import ClientError
        costs = {"year_month": "2026-11", "by_service": [], "currencies": [{"currency": "USD",
            "total_cost": "10", "net_cost": "8", "credits_applied": "2", "refunds": "0", "discounts": "0", "lightsail_cost": "10"}]}
        with patch.object(b, "get_sts_identity", return_value="123456789012") as identity, \
             patch.object(b, "make_ce_client"), patch.object(b, "get_billing_summary", return_value=costs) as cost_calls, \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}), \
             patch.object(b, "get_aws_credits", return_value={"credits": []}):
            self.client.get("/api/v2/billing/summary?wait=true")
            # Only identity is expired: confirmed authentication failure must also
            # invalidate still-fresh sections, independent of their TTL.
            self.db.execute(update(DataSnapshot).where(DataSnapshot.kind == "billing-identity").values(expires_at=0))
            self.db.commit()
            identity.side_effect = ClientError({"Error": {"Code": "ExpiredToken", "Message": "expired"}}, "GetCallerIdentity")
            self.client.get("/api/v2/billing/summary?wait=true")
            self.db.expire_all()
            self.assertTrue(all(row.payload is None for row in self.db.query(DataSnapshot).all()))
            after = self.client.get("/api/v2/billing/summary").json()
            self.assertEqual(after["currencies"], [])
            self.assertEqual(cost_calls.call_count, 1)

    def test_sts_access_denied_preserves_confirmed_billing_and_regions(self):
        from app.services import billing as b
        from app.services import lightsail as ls
        from app.models import DataSnapshot
        from sqlalchemy import update
        from botocore.exceptions import ClientError
        costs = {"year_month": "2026-11", "by_service": [], "currencies": [{"currency": "USD",
            "total_cost": "10", "net_cost": "8", "credits_applied": "2", "refunds": "0", "discounts": "0", "lightsail_cost": "10"}]}
        with patch.object(b, "get_sts_identity", return_value="123456789012") as identity, \
             patch.object(b, "make_ce_client"), patch.object(b, "get_billing_summary", return_value=costs), \
             patch.object(b, "get_forecast_result", return_value={"amount": "5", "currency": "USD"}), \
             patch.object(b, "get_aws_credits", return_value={"credits": []}), \
             patch.object(ls, "get_regions", return_value=[{"name": "us-east-1"}, {"name": "eu-west-1"}]), \
             patch.object(ls, "list_instances_in_region", side_effect=lambda _ak, _sk, region, **_kw: [
                 {"name": "kept-" + region, "region": region, "state": "running"}]):
            warm = self.client.get("/api/v2/billing/summary?wait=true").json()
            self.assertEqual(warm["currencies"][0]["net_cost"], "8")
            instances = self.client.get("/api/v2/instances?wait=true")
            self.assertEqual(instances.status_code, 200, instances.text)
            self.assertEqual(len(instances.json()["instances"]), 2)
            self.db.execute(update(DataSnapshot).where(DataSnapshot.kind == "billing-identity").values(expires_at=0))
            self.db.commit()
            identity.side_effect = ClientError({"Error": {"Code": "AccessDenied",
                "Message": "not authorized to perform sts:GetCallerIdentity"}}, "GetCallerIdentity")
            refreshed = self.client.get("/api/v2/billing/summary?wait=true")
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            self.assertEqual(refreshed.json()["currencies"][0]["net_cost"], "8")
            self.assertTrue(refreshed.json()["stale"])
            self.db.expire_all()
            rows = self.db.query(DataSnapshot).filter(DataSnapshot.credential_id == self.credential.id).all()
            self.assertEqual(len(rows), 7)
            identity_row = next(row for row in rows if row.kind == "billing-identity")
            self.assertIsNone(identity_row.payload)
            self.assertEqual(identity_row.error_status, "no_permission")
            preserved = [row for row in rows if row.kind != "billing-identity"]
            self.assertEqual(len(preserved), 6)
            self.assertTrue(all(row.payload is not None and row.last_success is not None for row in preserved))
            self.assertEqual({row.scope for row in rows if row.kind == "instances"}, {"us-east-1", "eu-west-1"})
            self.assertEqual(self.client.get("/api/v2/instances").json()["refreshing"], False)

    def test_successful_stop_expires_snapshot_and_prevents_older_refresh_publish(self):
        from app.models import DataSnapshot
        from app.services.snapshots import read_snapshot, scope_for
        scope = scope_for(self.user, self.credential, "instances", "us-east-1")
        async def initial(): return [{"name": "fixed", "region": "us-east-1", "state": "running"}]
        asyncio.run(read_snapshot(self.engine, scope, 60, initial, wait=True))
        gate = threading.Event()
        async def old():
            await asyncio.to_thread(gate.wait, 3)
            return [{"name": "fixed", "region": "us-east-1", "state": "running"}]
        asyncio.run(read_snapshot(self.engine, scope, 60, old, force=True))
        from task_fakes import FakeLightsail
        with FakeLightsail(name="fixed").installed():
            response = self.client.post(f"/api/instances/us-east-1/fixed/stop?credential_id={self.credential.id}")
        self.assertEqual(response.status_code, 200, response.text)
        gate.set(); self.finish_workers()
        self.db.expire_all()
        row = self.db.get(DataSnapshot, scope["key"])
        self.assertEqual(row.expires_at, 0)
        self.assertIsNone(row.lease_token)


class SnapshotMigrationTests(unittest.TestCase):
    def test_real_base_v3_upgrade_rollback_and_restore(self):
        import io
        import sqlite3
        import subprocess
        import tarfile
        from contextlib import closing
        from sqlalchemy import create_engine
        from app.database import Base
        from app import migrations
        from app.sqlite_backup import restore_database, file_sha256
        base = "1dd5d56c3bc637aeec9439c64f316f7f8f391438"
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = subprocess.check_output(["git", "archive", base, "backend"], cwd=root)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(folder, filter="data")
            path = folder / "v3.db"
            env = {**os.environ, "DATABASE_URL": f"sqlite:///{path.as_posix()}",
                   "PYTHONPATH": str(folder / "backend"), "PYTHONDONTWRITEBYTECODE": "1"}
            seeded = subprocess.run([sys.executable, "-c", "from app.database import init_db,engine;init_db();engine.dispose()"],
                                    cwd=folder, env=env, capture_output=True, text=True)
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            before = file_sha256(path)
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            original = migrations.apply_v4
            def fail(conn, metadata):
                original(conn, metadata)
                raise RuntimeError("injected after snapshot DDL")
            with patch.object(migrations, "apply_v4", side_effect=fail), self.assertRaises(migrations.MigrationError):
                migrations.upgrade_database(engine, Base.metadata)
            self.assertEqual(file_sha256(path), before)
            migrations.upgrade_database(engine, Base.metadata)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,), (2,), (3,), (4,), (5,), (6,)])
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                backup, sha = conn.execute("SELECT backup_path,backup_sha256 FROM schema_migrations WHERE version=4").fetchone()
            restored = folder / "restored.db"
            restore_database(backup, restored, sha, offline=True)
            with closing(sqlite3.connect(restored)) as conn:
                self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone(), (3,))
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='data_snapshots'").fetchone())
            migrations.upgrade_database(engine, Base.metadata)
            engine.dispose()
