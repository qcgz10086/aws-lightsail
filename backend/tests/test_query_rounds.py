"""Offline discovery, cancellation, scope/version fences and restart recovery."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import AwsCredential, DataSnapshot, User
from app.query_models import QueryAccount, QueryRegion, QueryRound, QueryTarget
from app.services import query_rounds as queries
from app.services import snapshots
import test_region_identity as identity_tests


class QueryRoundTests(unittest.TestCase):
    setUp = identity_tests.RegionIdentityTests.setUp

    def setup_queries(self, instances=None, failures=(), disabled=()):
        self.addCleanup(queries.shutdown_queries, self.engine)
        self.calls = []
        values = instances if instances is not None else {"us-east-1": [{"name": "stopped", "state": "stopped"}],
                    "eu-west-1": [], "ap-southeast-1": [{"name": "running", "state": "running"}]}
        def load(_access, _secret, region, **_kwargs):
            self.calls.append(region)
            if region in failures:
                raise TimeoutError("simulated network outage")
            return {"instances": [{**item, "region": region} for item in values.get(region, [])],
                    "instances_complete": True, "auxiliary_error": None}
        patches = [patch.object(queries.ls, "get_regions", return_value=[{"name": name} for name in values]),
            patch.object(queries.ls, "discover_instances_in_region", side_effect=load),
            patch.object(queries, "fetch_region_opt_status", side_effect=lambda _a, _s, _p, region:
                         "DISABLED" if region in disabled else "UNKNOWN")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def wait(self, round_id):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            value = queries.query_view(self.engine, self.user.id, round_id)
            if value["status"] in queries.TERMINAL:
                return value
            time.sleep(.02)
        self.fail(f"Round did not finish: {value}")

    def submit(self, **kwargs):
        return queries.submit_query(self.engine, self.user.id, self.credential.id, **kwargs)

    def test_first_discovery_then_fixed_active_regions_and_no_poll_requery(self):
        self.setup_queries()
        first = self.wait(self.submit())
        self.assertEqual(first["mode"], "global")
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["total"], 3)
        self.assertEqual(set(self.calls), {"us-east-1", "eu-west-1", "ap-southeast-1"})
        self.db.expire_all()
        plan = queries.region_plan(self.db, self.user, self.credential)
        self.assertEqual(plan["regions"], ["ap-southeast-1", "us-east-1"])
        self.calls.clear()
        second = self.wait(self.submit(force=True))
        self.assertEqual(second["mode"], "refresh")
        self.assertEqual(second["total"], 2)
        self.assertEqual(set(self.calls), {"us-east-1", "ap-southeast-1"})
        with patch("app.services.query_rounds.time.time", return_value=time.time() + 120):
            for _ in range(4):
                self.assertEqual(queries.query_view(self.engine, self.user.id, second["id"])["status"], "succeeded")
        self.assertEqual(len(self.calls), 2)

    def test_partial_first_discovery_does_not_retry_unknown_regions_automatically(self):
        self.setup_queries(failures={"eu-west-1"})
        first = self.wait(self.submit())
        self.assertEqual(first["status"], "partial")
        self.db.expire_all()
        plan = queries.region_plan(self.db, self.user, self.credential)
        self.assertEqual(plan["discovery_state"], "partial")
        self.assertEqual(plan["undiscovered"], 1)
        self.calls.clear()
        second = self.wait(self.submit(force=True))
        self.assertEqual(second["total"], 2)
        self.assertNotIn("eu-west-1", self.calls)

    def test_successful_empty_removes_region_failure_preserves_membership(self):
        self.setup_queries()
        self.wait(self.submit())
        def now_empty(_a, _s, region, **_kw):
            if region == "us-east-1":
                raise TimeoutError("temporary outage")
            return {"instances": [], "instances_complete": True, "auxiliary_error": None}
        with patch.object(queries.ls, "discover_instances_in_region", side_effect=now_empty):
            result = self.wait(self.submit(force=True))
        self.db.expire_all()
        self.assertEqual(queries.region_plan(self.db, self.user, self.credential)["regions"], ["us-east-1"])
        failed = next(target for target in result["targets"] if target["region"] == "us-east-1")
        self.assertTrue(failed["data"]["stale"])
        self.assertEqual(failed["data"]["instances"][0]["name"], "stopped")

    def test_no_active_regions_pause_until_manual_global_discovery(self):
        self.setup_queries(instances={"us-east-1": [], "eu-west-1": []})
        self.wait(self.submit())
        self.calls.clear()
        self.assertEqual(self.wait(self.submit(force=True))["total"], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.wait(self.submit(mode="global", region="us-east-1", force=True))["total"], 2)
        self.assertEqual(set(self.calls), {"us-east-1", "eu-west-1"})

    def test_unknown_region_status_reads_but_disabled_region_is_skipped(self):
        self.setup_queries(disabled={"eu-west-1"})
        first = self.wait(self.submit())
        self.assertEqual(first["status"], "succeeded")
        self.assertNotIn("eu-west-1", self.calls)
        self.assertEqual(next(item for item in first["targets"] if item["region"] == "eu-west-1")["status"], "disabled")

    def test_auxiliary_failure_preserves_confirmed_instances_without_proving_empty(self):
        self.setup_queries()
        def partial(_a, _s, region, **_kw):
            return {"instances": [{"name": "stopped", "region": region}] if region == "us-east-1" else [],
                    "instances_complete": True, "auxiliary_error": "静态 IP 附属信息未完整读取"}
        with patch.object(queries.ls, "discover_instances_in_region", side_effect=partial):
            first = self.wait(self.submit())
        self.assertTrue(all(target["auxiliary_error"] for target in first["targets"]))
        self.db.expire_all()
        plan = queries.region_plan(self.db, self.user, self.credential)
        self.assertEqual(plan["regions"], ["us-east-1"])
        self.assertEqual(plan["undiscovered"], 2)

    def test_same_account_shares_discovery_but_isolates_user_and_current_access(self):
        self.setup_queries()
        self.wait(self.submit())
        from app.security import encrypt_secret
        with Session(self.engine) as db:
            second = AwsCredential(user_id=self.user.id, account_id=self.credential.account_id,
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN8EXAMPLE"), secret_access_key_enc=encrypt_secret("fake"))
            other = User(username="isolated", password_hash="opaque")
            db.add_all([second, other])
            db.flush()
            foreign = AwsCredential(user_id=other.id, account_id=self.credential.account_id,
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN9EXAMPLE"), secret_access_key_enc=encrypt_secret("fake"))
            db.add(foreign)
            db.commit()
            self.assertEqual(queries.region_plan(db, db.get(User, self.user.id), second)["regions"],
                             ["ap-southeast-1", "us-east-1"])
            self.assertEqual(queries.region_plan(db, other, foreign)["state"], "pending")
            second_id = second.id
        round_id = queries.submit_query(self.engine, self.user.id, second_id, force=True)
        self.assertEqual(self.wait(round_id)["total"], 2)

    def test_cancel_before_send_and_after_send_keeps_only_finished_work(self):
        self.setup_queries()
        round_id = self.submit(start=False)
        self.assertEqual(queries.cancel_query(self.engine, self.user.id, round_id)["status"], "cancelled")
        queries.worker_for(self.engine).start(round_id)
        self.assertEqual(self.wait(round_id)["status"], "cancelled")
        self.assertEqual(self.calls, [])
        started, release = threading.Event(), threading.Event()
        def slow(_a, _s, region, **_kw):
            started.set()
            release.wait(3)
            return {"instances": [{"name": "sent", "region": region}], "instances_complete": True,
                    "auxiliary_error": None}
        with patch.object(queries.ls, "discover_instances_in_region", side_effect=slow):
            round_id = self.submit(mode="global", force=True)
            self.assertTrue(started.wait(3))
            self.assertEqual(queries.cancel_query(self.engine, self.user.id, round_id)["status"], "cancelling")
            release.set()
            final = self.wait(round_id)
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(final["succeeded"], 1)
        self.assertEqual(final["cancelled"], 2)
        self.assertEqual(queries.current_query(self.engine, self.user.id, self.credential.id), round_id)

    def test_restart_recovers_only_unfinished_targets_and_fences_old_worker(self):
        self.setup_queries()
        first = self.wait(self.submit())
        round_id = self.submit(force=True, start=False)
        with Session(self.engine) as db:
            row = db.get(QueryRound, round_id)
            row.status, row.worker_token, row.lease_until = "running", "old-worker", time.time() + 300
            targets = db.scalars(select(QueryTarget).where(QueryTarget.round_id == round_id).order_by(QueryTarget.region)).all()
            targets[0].status, targets[0].result_json = "succeeded", json.dumps(first["targets"][0]["data"])
            targets[1].status = "running"
            old_target_id = targets[1].id
            db.commit()
        self.calls.clear()
        queries.resume_queries(self.engine)
        self.assertEqual(self.wait(round_id)["status"], "succeeded")
        self.assertEqual(self.calls, ["us-east-1"])
        with self.assertRaises(queries.QueryStopped):
            queries._save_target(self.engine, round_id, "old-worker", old_target_id,
                {"data": {"instances": [{"name": "late"}]}})

    def test_version_change_stops_send_and_hides_completed_results(self):
        self.setup_queries()
        first = self.wait(self.submit())
        pending = self.submit(force=True, start=False)
        with Session(self.engine) as db:
            db.get(AwsCredential, self.credential.id).secret_access_key_enc = "rotated"
            db.commit()
        self.calls.clear()
        queries.worker_for(self.engine).start(pending)
        self.assertEqual(self.wait(pending)["status"], "invalidated")
        self.assertEqual(self.calls, [])
        old = queries.query_view(self.engine, self.user.id, first["id"])
        self.assertTrue(all(target["data"] is None for target in old["targets"]))

    def test_snapshot_gate_rechecks_before_each_page_after_version_rotation(self):
        from app.services.read_throttle import _current
        scope = snapshots.scope_for(self.user, self.credential, "test", "guarded-pages")
        calls = []
        async def loader():
            calls.append("page-1")
            with Session(self.engine) as db:
                db.get(AwsCredential, self.credential.id).proxy_url_enc = "changed"
                db.commit()
            for check in _current.get().checks:
                check()
            calls.append("page-2")
            return {"private": "obsolete"}
        result = asyncio.run(snapshots.read_snapshot(self.engine, scope, 60, loader, wait=True))
        self.assertEqual(calls, ["page-1"])
        self.assertEqual(result["status"], "invalidated")

    def test_deletion_cleans_last_account_regions_and_invalidates_rounds(self):
        self.setup_queries()
        first = self.wait(self.submit())
        pending = self.submit(force=True, start=False)
        with Session(self.engine) as db:
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            credential = db.get(AwsCredential, self.credential.id)
            queries.cleanup_credential_queries(db, self.user.id, credential)
            db.delete(credential)
            db.commit()
            self.assertEqual(db.scalars(select(QueryRegion)).all(), [])
            self.assertEqual(db.scalars(select(QueryAccount)).all(), [])
        self.assertEqual(queries.query_view(self.engine, self.user.id, pending)["status"], "invalidated")
        self.assertTrue(all(target["data"] is None for target in queries.query_view(
            self.engine, self.user.id, first["id"])["targets"]))

    def test_cancelling_one_subscriber_does_not_stop_another_round(self):
        self.setup_queries()
        self.wait(self.submit())
        started, release = threading.Event(), threading.Event()
        calls = []
        def load(_a, _s, region, **_kw):
            calls.append(region)
            if region == "us-east-1":
                started.set()
                release.wait(4)
                from app.services.read_throttle import _current
                for check in _current.get().checks:
                    check()
            return {"instances": [{"name": "shared", "region": region}], "instances_complete": True,
                    "auxiliary_error": None}
        with patch.object(queries.ls, "discover_instances_in_region", side_effect=load):
            first = self.submit(region="us-east-1", force=True)
            self.assertTrue(started.wait(3))
            second = self.submit(mode="global", force=True)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                value = queries.query_view(self.engine, self.user.id, second)
                if any(target["region"] == "us-east-1" and target["status"] == "running" for target in value["targets"]):
                    break
                time.sleep(.02)
            else:
                self.fail("Second round did not subscribe to shared scope")
            queries.cancel_query(self.engine, self.user.id, first)
            release.set()
            self.assertEqual(self.wait(first)["status"], "cancelled")
            self.assertEqual(self.wait(second)["status"], "succeeded")
        self.assertEqual(calls.count("us-east-1"), 1)

    def test_actual_pagination_retains_instances_when_only_static_ip_fails(self):
        from unittest.mock import Mock
        from app.services import lightsail
        client = Mock()
        client.get_instances.side_effect = [{"instances": [{"name": "stopped", "state": {"name": "stopped"}}],
            "nextPageToken": "page2"}, {"instances": [{"name": "running", "state": {"name": "running"}}]}]
        client.get_static_ips.side_effect = TimeoutError("unavailable")
        with patch.object(lightsail, "make_client", return_value=client):
            result = lightsail.discover_instances_in_region("fake", "fake", "us-east-1")
        self.assertEqual([row["name"] for row in result["instances"]], ["stopped", "running"])
        self.assertTrue(result["instances_complete"])
        self.assertTrue(result["auxiliary_error"])
        self.assertEqual(client.get_instances.call_args_list[1].kwargs, {"pageToken": "page2"})

    def test_repeated_or_failed_instance_page_never_proves_empty(self):
        from unittest.mock import Mock
        from app.services import lightsail
        client = Mock()
        for pages in ([{"instances": [], "nextPageToken": "same"}, {"instances": [], "nextPageToken": "same"}],
                      [{"instances": [], "nextPageToken": "later"}, TimeoutError("unavailable")]):
            with self.subTest(pages=pages):
                client.get_instances.side_effect = pages
                with patch.object(lightsail, "make_client", return_value=client):
                    with self.assertRaises(lightsail.LightsailError):
                        lightsail.discover_instances_in_region("fake", "fake", "us-east-1")

    def test_http_progress_is_local_and_foreign_cancel_is_hidden(self):
        self.setup_queries()
        from fastapi.testclient import TestClient
        from app.main import app
        from app.database import get_db
        from app.deps import get_current_user
        previous = dict(app.dependency_overrides)
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(previous)))
        def db_session():
            with Session(self.engine) as db:
                yield db
        app.dependency_overrides[get_db] = db_session
        app.dependency_overrides[get_current_user] = lambda: self.user
        client = TestClient(app)
        response = client.post("/api/v2/queries", json={"mode": "global", "credential_id": self.credential.id,
                                                       "region": "us-east-1"})
        self.assertEqual(response.status_code, 202, response.text)
        round_id = response.json()["round_ids"][0]
        self.assertEqual(self.wait(round_id)["total"], 3)
        self.calls.clear()
        for _ in range(3):
            result = client.get("/api/v2/query-instances", params={"round_ids": round_id})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(len(result.json()["instances"]), 2)
            self.assertEqual(result.json()["rounds"][0]["id"], round_id)
            self.assertFalse(result.json()["refreshing"])
        self.assertEqual(self.calls, [])
        other = User(id=999, username="other", password_hash="opaque")
        app.dependency_overrides[get_current_user] = lambda: other
        denied = client.post(f"/api/v2/queries/{round_id}/cancel")
        self.assertEqual(denied.status_code, 404, denied.text)

    def test_graceful_shutdown_preserves_remaining_targets_for_restart(self):
        self.setup_queries()
        self.wait(self.submit())
        started, release = threading.Event(), threading.Event()
        def slow(_a, _s, region, **_kwargs):
            started.set()
            release.wait(3)
            return {"instances": [{"name": "sent", "region": region}], "instances_complete": True,
                    "auxiliary_error": None}
        with patch.object(queries.ls, "discover_instances_in_region", side_effect=slow):
            round_id = self.submit(force=True)
            self.assertTrue(started.wait(3))
            closer = threading.Thread(target=queries.shutdown_queries, args=(self.engine,))
            closer.start()
            deadline = time.monotonic() + 2
            while not queries.worker_for(self.engine).closed and time.monotonic() < deadline:
                time.sleep(.01)
            release.set()
            closer.join(4)
            self.assertFalse(closer.is_alive())
        saved = queries.query_view(self.engine, self.user.id, round_id)
        self.assertEqual(saved["status"], "queued")
        self.assertTrue(all(target["status"] == "queued" for target in saved["targets"]))
        queries.resume_queries(self.engine)
        self.assertEqual(self.wait(round_id)["status"], "succeeded")

    def test_new_visit_refreshes_invalidated_cache_but_fixed_progress_does_not(self):
        self.setup_queries()
        first = self.wait(self.submit())
        with Session(self.engine) as db:
            for snapshot in db.scalars(select(DataSnapshot).where(DataSnapshot.kind == "query-instances")):
                snapshot.expires_at = 0
            db.commit()
        self.calls.clear()
        fixed = queries.query_view(self.engine, self.user.id, first["id"])
        self.assertTrue(all(target["data"]["stale"] for target in fixed["targets"]))
        self.assertEqual(self.calls, [])
        next_id = queries.current_query(self.engine, self.user.id, self.credential.id)
        self.assertNotEqual(next_id, first["id"])
        self.assertEqual(self.wait(next_id)["status"], "succeeded")
        self.assertEqual(set(self.calls), {"us-east-1", "ap-southeast-1"})
