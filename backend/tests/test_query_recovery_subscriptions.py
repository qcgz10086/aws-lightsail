"""Final-review F3/F4/F5 interleavings with a file DB and zero AWS transport."""
import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, OperationTask, OperationTaskLock, User
from app.query_models import QueryRound
from app.services import query_rounds as queries, snapshots, task_handlers
from app.services.task_queue import DurableTaskQueue, TaskContext, TaskPaused
from app.services.read_throttle import _current
import test_region_identity as identity_tests
from task_fakes import FakeLightsail


class QueryRecoverySubscriptionTests(unittest.TestCase):
    setUp = identity_tests.RegionIdentityTests.setUp

    def workers(self):
        task_handlers.install_handlers()
        self.queue = DurableTaskQueue(self.engine)
        self.addCleanup(self.queue.shutdown, wait=True)
        self.addCleanup(queries.shutdown_queries, self.engine)
        self.aws = FakeLightsail(name="server")
        fake = self.aws.installed()
        fake.__enter__()
        self.addCleanup(fake.__exit__, None, None, None)
        status = patch.object(queries, "fetch_region_opt_status", return_value="UNKNOWN")
        status.start()
        self.addCleanup(status.stop)

    def wait_round(self, round_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            view = queries.query_view(self.engine, self.user.id, round_id)
            if view["status"] in queries.TERMINAL:
                return view
            time.sleep(.02)
        self.fail(f"Round did not finish: {view}")

    def wait_task(self, task_id, states):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            with Session(self.engine) as db:
                view = self.queue.get(db, self.user.id, task_id)
            if view["status"] in states:
                return view
            time.sleep(.02)
        self.fail(f"Task did not reach {states}: {view}")

    def traffic(self):
        with Session(self.engine) as db:
            return self.queue.accept(db, db.get(User, self.user.id),
                db.get(AwsCredential, self.credential.id), "traffic_sync", {},
                "traffic-demand", region=None, resource_name=None, account_lock=True, wait=False)

    def round_for_task(self, task_id):
        with Session(self.engine) as db:
            marker = db.scalar(select(DataSnapshot).where(DataSnapshot.kind == queries.SUBSCRIPTION_KIND,
                DataSnapshot.scope.endswith(f"/task:{task_id}")))
            self.assertIsNotNone(marker)
            return marker.scope.split("/", 1)[0]

    def test_restart_during_catalog_revokes_old_lease_and_requeries_once(self):
        self.workers()
        round_id = queries.submit_query(self.engine, self.user.id, self.credential.id, start=False)
        catalog = snapshots.scope_for(self.user, self.credential, "catalog", "regions")
        _view, old_token = snapshots._read_or_claim(self.engine, catalog, False)
        with Session(self.engine) as db:
            row = db.get(QueryRound, round_id)
            row.status, row.worker_token, row.lease_until = "discovering", "old-process", time.time() + 300
            db.commit()
        queries.resume_queries(self.engine)
        final = self.wait_round(round_id)
        self.assertEqual(final["status"], "succeeded")
        self.assertEqual(self.aws.count("GetRegions"), 1)
        self.assertIsNone(snapshots._publish(self.engine, catalog, old_token, 86400,
            data=[{"name": "eu-west-1"}]))

    def test_followed_catalog_lease_expiry_is_reclaimed_without_restart(self):
        self.workers()
        round_id = queries.submit_query(self.engine, self.user.id, self.credential.id, start=False)
        catalog = snapshots.scope_for(self.user, self.credential, "catalog", "regions")
        snapshots._read_or_claim(self.engine, catalog, False)
        queries.worker_for(self.engine).start(round_id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with Session(self.engine) as db:
                if db.get(QueryRound, round_id).status == "discovering":
                    break
            time.sleep(.01)
        with Session(self.engine) as db:
            db.get(DataSnapshot, catalog["key"]).lease_until = time.time() - 1
            db.commit()
        self.assertEqual(self.wait_round(round_id)["status"], "succeeded")
        self.assertEqual(self.aws.count("GetRegions"), 1)

    def test_restart_does_not_reclaim_cancelled_or_rotated_discovery(self):
        self.workers()
        cancelled = queries.submit_query(self.engine, self.user.id, self.credential.id, start=False)
        queries.cancel_query(self.engine, self.user.id, cancelled)
        changed = queries.submit_query(self.engine, self.user.id, self.credential.id, start=False)
        with Session(self.engine) as db:
            db.get(AwsCredential, self.credential.id).secret_access_key_enc = "rotated"
            db.commit()
        queries.resume_queries(self.engine)
        self.assertEqual(queries.query_view(self.engine, self.user.id, changed)["status"], "invalidated")
        self.assertEqual(self.aws.count("GetRegions"), 0)

    def test_graceful_shutdown_waiting_for_discovery_exits_and_restart_continues(self):
        self.workers()
        entered, release = threading.Event(), threading.Event()
        def catalog(*_args, **_kwargs):
            entered.set()
            release.wait(4)
            return [{"name": "us-east-1"}]
        with patch.object(queries.ls, "get_regions", side_effect=catalog):
            accepted = self.traffic()
            self.assertTrue(entered.wait(3))
            round_id = self.round_for_task(accepted["task_id"])
            self.queue.shutdown()  # Same first step as lifespan, no blocking join.
            release.set()
            queries.shutdown_queries(self.engine)
            paused = self.wait_task(accepted["task_id"], {"queued"})
            deadline = time.monotonic() + 2
            while self.queue._futures and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(self.queue._futures)
            self.assertEqual(queries.query_view(self.engine, self.user.id, round_id)["status"], "queued")
            with Session(self.engine) as db:
                row = db.get(OperationTask, accepted["task_id"])
                self.assertIsNone(row.worker_token)
                self.assertEqual(row.lease_until, 0)
                self.assertIsNotNone(db.scalar(select(OperationTaskLock).where(OperationTaskLock.task_id == row.id)))
        self.queue = DurableTaskQueue(self.engine)
        self.addCleanup(self.queue.shutdown, wait=True)
        queries.resume_queries(self.engine)
        self.queue.recover()
        result = self.wait_task(accepted["task_id"], {"succeeded", "failed", "partial"})
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(self.aws.count("StopInstance"), 0)

    def subscription_cancel(self, other_page):
        self.workers()
        entered, release = threading.Event(), threading.Event()
        later_reads = []
        def region(_a, _s, name, **_kw):
            entered.set()
            release.wait(4)
            # Represents the next SDK page or optional read after a request
            # already in flight, using the inherited actual-send checks.
            for check in _current.get().checks:
                check()
            later_reads.append(name)
            return {"instances": [], "instances_complete": True, "auxiliary_error": None}
        with patch.object(queries.ls, "discover_instances_in_region", side_effect=region):
            accepted = self.traffic()
            self.assertTrue(entered.wait(3))
            round_id = self.round_for_task(accepted["task_id"])
            if other_page:
                self.assertEqual(queries.submit_query(self.engine, self.user.id, self.credential.id), round_id)
            with Session(self.engine) as db:
                self.queue.cancel(db, self.user.id, accepted["task_id"])
            self.wait_task(accepted["task_id"], {"cancelled"})
            release.set()
            result = self.wait_round(round_id)
        self.assertEqual(result["status"], "succeeded" if other_page else "cancelled")
        self.assertEqual(later_reads, ["us-east-1"] if other_page else [])

    def test_cancel_sole_traffic_subscription_stops_later_discovery_sends(self):
        self.subscription_cancel(False)

    def test_cancel_traffic_leaves_page_subscriber_and_shared_discovery_running(self):
        self.subscription_cancel(True)

    def test_pause_with_mutation_evidence_keeps_reconciliation_boundary(self):
        self.workers()
        with patch.object(self.queue, "schedule"):
            accepted = self.traffic()
        with Session(self.engine) as db:
            row = db.get(OperationTask, accepted["task_id"])
            row.status, row.worker_token, row.lease_until = "running", "owner", time.time() + 300
            db.commit()
        context = TaskContext(self.engine, accepted["task_id"], "owner", {})
        sequence = context.begin_step("auto_stop:us-east-1:server", {"mutation": True})
        context.finish_step(sequence, {"operation_ids": ["sent"]}, status="succeeded")
        context.pause()
        with Session(self.engine) as db:
            self.assertEqual(db.get(OperationTask, accepted["task_id"]).status, "reconciliation_required")
            self.assertIsNotNone(db.scalar(select(OperationTaskLock).where(
                OperationTaskLock.task_id == accepted["task_id"])))

    def test_auxiliary_throttle_is_visible_and_only_static_ip_read_cools_down(self):
        from botocore.exceptions import ClientError
        self.workers()
        original = self.aws.call
        def call(operation, parameters):
            if operation == "GetStaticIps":
                self.aws.calls.append((operation, parameters))
                raise ClientError({"Error": {"Code": "ThrottlingException"}, "ResponseMetadata": {
                    "HTTPStatusCode": 429, "HTTPHeaders": {"retry-after": "120"}}}, operation)
            return original(operation, parameters)
        with patch.object(self.aws, "call", side_effect=call):
            first = self.wait_round(queries.submit_query(self.engine, self.user.id, self.credential.id, force=True))
            second = self.wait_round(queries.submit_query(self.engine, self.user.id, self.credential.id,
                                                         mode="global", force=True))
        self.assertEqual(first["status"], "partial")
        self.assertEqual(second["status"], "partial")
        for value in (first, second):
            target = value["targets"][0]
            self.assertEqual(target["status"], "succeeded")
            self.assertEqual(target["error_code"], "ThrottlingException")
            self.assertTrue(target["next_retry_at"])
            self.assertEqual(target["data"]["instances"][0]["name"], "server")
        self.assertEqual(first["targets"][0]["next_retry_at"], second["targets"][0]["next_retry_at"])
        self.assertEqual(self.aws.count("GetInstances"), 2)
        self.assertEqual(self.aws.count("GetStaticIps"), 1)

    def test_paused_read_only_traffic_can_be_cancelled_before_restart_worker_claims(self):
        self.workers()
        with patch.object(self.queue, "schedule"):
            accepted = self.traffic()
        with Session(self.engine) as db:
            row = db.get(OperationTask, accepted["task_id"])
            row.status, row.worker_token, row.lease_until, row.attempt = "running", "owner", time.time() + 300, 1
            db.commit()
        context = TaskContext(self.engine, accepted["task_id"], "owner", {})
        context.pause()
        with Session(self.engine) as db:
            view = self.queue.get(db, self.user.id, accepted["task_id"])
            self.assertTrue(view["can_cancel"])
            result = self.queue.cancel(db, self.user.id, accepted["task_id"])
            self.assertEqual(result["status"], "cancelled")
            self.assertIsNone(db.scalar(select(OperationTaskLock).where(
                OperationTaskLock.task_id == accepted["task_id"])))

    def test_permission_and_identity_retry_messages_are_stable_and_redacted(self):
        from app.services.task_queue import TaskKnownFailure
        for code, label in (("AccessDeniedException", "权限"), ("ExpiredToken", "身份")):
            with self.subTest(code=code):
                failure = TaskKnownFailure("private secret must never be reflected")
                failure.read_failure = {"code": code, "reason": "当前读取权限或身份认证未通过",
                    "shared": False, "next_retry_at": "2026-11-20T15:00:00+00:00"}
                result = task_handlers._traffic_failure(failure, "us-east-1", "instances")
                self.assertIn(label, result["retry_condition"])
                self.assertNotIn("private", str(result))
                self.assertNotIn("secret", str(result))
