"""Resource tasks expire both instance snapshot formats and revoke old readers."""
import asyncio
import time
import unittest
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, OperationTask, User
from app.services import query_rounds as queries, snapshots, task_handlers
from app.services.task_queue import TaskContext, queue_for
from task_fakes import FakeLightsail
import test_region_identity as identity_tests


class TaskQueryInvalidationTests(unittest.TestCase):
    setUp = identity_tests.RegionIdentityTests.setUp

    def wait_query(self, round_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            view = queries.query_view(self.engine, self.user.id, round_id)
            if view["status"] in queries.TERMINAL:
                return view
            time.sleep(.01)
        self.fail(f"Query did not finish: {view}")

    def setup_workers(self):
        self.queue = queue_for(self.engine)
        self.addCleanup(self.queue.shutdown, wait=True)
        self.addCleanup(queries.shutdown_queries, self.engine)

    def accept_stop(self, key, *, wait=True):
        with Session(self.engine) as db:
            return self.queue.accept(db, db.get(User, self.user.id),
                db.get(AwsCredential, self.credential.id), "stop",
                {"kind": "stop", "region": "us-east-1", "resource_name": "server",
                 "credential_id": self.credential.id}, key,
                region="us-east-1", resource_name="server", wait=wait)

    def test_stop_after_query_starts_new_round_and_revokes_old_query_lease(self):
        self.setup_workers()
        aws = FakeLightsail(name="server")
        with aws.installed(), patch.object(queries, "fetch_region_opt_status", return_value="UNKNOWN"):
            first_id = queries.submit_query(self.engine, self.user.id, self.credential.id)
            first = self.wait_query(first_id)
            self.assertEqual(first["status"], "succeeded")
            self.assertEqual(first["targets"][0]["data"]["instances"][0]["state"], "running")
            scope = snapshots.scope_for(self.user, self.credential, "query-instances", "us-east-1")
            _view, old_token = snapshots._read_or_claim(self.engine, scope, True)
            self.assertIsNotNone(old_token)

            stopped = self.accept_stop("stop-after-query")
            self.assertEqual(stopped["status"], "succeeded", stopped)
            self.assertEqual(aws.count("StopInstance"), 1)
            self.assertEqual(aws.instances["server"]["state"]["name"], "stopped")
            with Session(self.engine) as db:
                row = db.get(DataSnapshot, scope["key"])
                self.assertEqual(row.expires_at, 0)
                self.assertIsNone(row.lease_token)
                self.assertEqual(row.lease_until, 0)
            self.assertIsNone(snapshots._publish(self.engine, scope, old_token, 60,
                data={"instances": [{"name": "server", "region": "us-east-1", "state": "running"}],
                      "instances_complete": True, "auxiliary_error": None}))
            old_view = queries.query_view(self.engine, self.user.id, first_id)
            self.assertTrue(old_view["targets"][0]["data"]["stale"])

            second_id = queries.current_query(self.engine, self.user.id, self.credential.id)
            self.assertNotEqual(second_id, first_id)
            second = self.wait_query(second_id)
            self.assertEqual(second["status"], "succeeded")
            self.assertEqual(second["targets"][0]["data"]["instances"][0]["state"], "stopped")
            self.assertFalse(second["targets"][0]["data"]["stale"])

    def test_handler_and_finish_each_revoke_both_snapshot_formats(self):
        self.setup_workers()
        scopes = [snapshots.scope_for(self.user, self.credential, kind, "us-east-1")
                  for kind in ("instances", "query-instances")]
        async def initial():
            return [{"name": "server", "state": "running", "region": "us-east-1"}]
        for scope in scopes:
            asyncio.run(snapshots.read_snapshot(self.engine, scope, 60, initial, wait=True))
        with patch.object(self.queue, "schedule"):
            accepted = self.accept_stop("manual-boundaries", wait=False)
        with Session(self.engine) as db:
            task = db.get(OperationTask, accepted["task_id"])
            task.status, task.worker_token, task.lease_until = "running", "f1-worker", time.time() + 300
            db.commit()
        context = TaskContext(self.engine, accepted["task_id"], "f1-worker", {})
        for boundary in (lambda: task_handlers._invalidate(context), lambda: context.finish("succeeded")):
            with self.subTest(boundary=boundary):
                tokens = [snapshots._read_or_claim(self.engine, scope, True)[1] for scope in scopes]
                self.assertTrue(all(tokens))
                boundary()
                with Session(self.engine) as db:
                    rows = db.scalars(select(DataSnapshot).where(
                        DataSnapshot.key.in_([scope["key"] for scope in scopes]))).all()
                    self.assertEqual(len(rows), 2)
                    self.assertTrue(all(row.expires_at == 0 and row.lease_token is None
                                        and row.lease_until == 0 for row in rows))
                for scope, token in zip(scopes, tokens):
                    self.assertIsNone(snapshots._publish(self.engine, scope, token, 60,
                                                        data={"late": "obsolete"}))
