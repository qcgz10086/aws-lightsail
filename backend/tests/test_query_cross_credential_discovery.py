"""Same-account discovery shares scope metadata, never another key's payload."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, OperationTask, User
from app.query_models import QueryRound, QueryTarget
from app.security import encrypt_secret
from app.services import query_rounds as queries, snapshots
from app.services.read_throttle import _current as current_read_context
from app.services.task_queue import DurableTaskQueue, credential_version
import test_region_identity as identity_tests


class CrossCredentialDiscoveryTests(unittest.TestCase):
    def setUp(self):
        identity_tests.RegionIdentityTests.setUp(self)
        self.http_transport = patch("botocore.httpsession.URLLib3Session.send",
            side_effect=AssertionError("AWS HTTP transport forbidden")).start()
        self.addCleanup(lambda: self.assertEqual(self.http_transport.call_count, 0))
        self.credential.is_default = True
        self.credential.account_label = "key A"
        self.second = AwsCredential(user_id=self.user.id, account_id=self.credential.account_id,
            access_key_id_enc=encrypt_secret("AKIAIOSFODNN8EXAMPLE"),
            secret_access_key_enc=encrypt_secret("second-fake-secret"),
            account_label="key B", is_default=False)
        self.db.add(self.second)
        self.db.commit()
        self.first_id, self.second_id = self.credential.id, self.second.id
        self.first_key, self.second_key = "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN8EXAMPLE"
        self.queue = DurableTaskQueue(self.engine)
        self.addCleanup(self.queue.shutdown, wait=True)
        self.catalog_calls, self.instance_calls = [], []
        self.call_lock = threading.Lock()
        self.catalog_started, self.catalog_release = threading.Event(), threading.Event()

    def install_reads(self, *, denied_second=False, catalog_failure=False):
        def catalog(access_key, _secret, **_kwargs):
            with self.call_lock:
                self.catalog_calls.append(access_key)
            self.catalog_started.set()
            if not self.catalog_release.wait(5):
                raise TimeoutError("test did not release catalog")
            # Model the next actual read after queueing/first response. The
            # complete product context must still authorize its subscribers.
            for check in current_read_context.get().checks:
                check()
            if catalog_failure:
                raise TimeoutError("catalog unavailable")
            return [{"name": "us-east-1"}, {"name": "eu-west-1"}]

        def instances(access_key, _secret, region, **_kwargs):
            for check in current_read_context.get().checks:
                check()
            with self.call_lock:
                self.instance_calls.append((access_key, region))
            if denied_second and access_key == self.second_key:
                raise ClientError({"Error": {"Code": "AccessDeniedException",
                    "Message": "key B cannot list instances"}}, "GetInstances")
            prefix = "private-a" if access_key == self.first_key else "private-b"
            return {"instances": [{"name": f"{prefix}-{region}", "region": region, "state": "running"}],
                    "instances_complete": True, "auxiliary_error": None}

        for item in (patch.object(queries.ls, "get_regions", side_effect=catalog),
                     patch.object(queries.ls, "discover_instances_in_region", side_effect=instances),
                     patch.object(queries, "fetch_region_opt_status", return_value="UNKNOWN")):
            item.start()
            self.addCleanup(item.stop)
        # Release blocked fakes before joining workers, and join before patches
        # disappear. This also keeps assertion failures from leaking threads.
        self.addCleanup(queries.shutdown_queries, self.engine)
        self.addCleanup(self.catalog_release.set)

    def make_task(self, credential_id):
        task_id = uuid.uuid4().hex
        with Session(self.engine) as db:
            credential = db.get(AwsCredential, credential_id)
            db.add(OperationTask(id=task_id, user_id=self.user.id, credential_id=credential_id,
                account_scope=credential.account_id, account_id=credential.account_id,
                credential_version=credential_version(credential), kind="traffic_sync",
                idempotency_key=task_id, request_fingerprint="f" * 64,
                encrypted_payload=encrypt_secret("{}"), status="queued", attempt=0))
            db.commit()
        return task_id

    def cancel_task(self, task_id):
        with Session(self.engine) as db:
            return self.queue.cancel(db, self.user.id, task_id)

    def submit(self, credential_id, *, subscriber="page"):
        return queries.submit_query(self.engine, self.user.id, credential_id,
                                    mode="global", force=True, subscriber=subscriber)

    def wait(self, round_id):
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            view = queries.query_view(self.engine, self.user.id, round_id)
            if view["status"] in queries.TERMINAL:
                return view
            time.sleep(.02)
        self.fail(f"Discovery did not finish: {view}")

    def assert_parent(self, follower_id, leader_id):
        with Session(self.engine) as db:
            parent = db.scalar(select(DataSnapshot).where(DataSnapshot.kind == "query-discovery-parent",
                DataSnapshot.scope == follower_id))
            self.assertIsNotNone(parent)
            self.assertEqual(parent.error, leader_id)
            self.assertEqual(parent.credential_id, self.second_id)
            self.assertIsNone(parent.payload)

    def test_task_leader_page_follower_survives_default_key_switch_without_payload_reuse(self):
        self.install_reads()
        first_task = self.make_task(self.first_id)
        leader_id = self.submit(self.first_id, subscriber=f"task:{first_task}")
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id)
        self.assertNotEqual(follower_id, leader_id)
        self.assert_parent(follower_id, leader_id)
        with Session(self.engine) as db:
            db.get(AwsCredential, self.first_id).is_default = False
            db.get(AwsCredential, self.second_id).is_default = True
            db.commit()
        self.assertEqual(queries.current_query(self.engine, self.user.id, self.second_id), follower_id)
        self.assertNotEqual(queries.query_view(self.engine, self.user.id, leader_id)["status"], "invalidated")
        self.cancel_task(first_task)
        self.assertIn(queries.query_view(self.engine, self.user.id, leader_id)["status"], queries.ACTIVE)
        self.catalog_release.set()
        follower = self.wait(follower_id)
        self.assertEqual(follower["status"], "succeeded")
        self.assertEqual(follower["credential_id"], self.second_id)
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertEqual({target["region"] for target in follower["targets"]}, {"us-east-1", "eu-west-1"})
        self.assertTrue(all(target["data"]["instances"][0]["name"].startswith("private-b-")
                            for target in follower["targets"]))
        self.assertEqual(queries.current_query(self.engine, self.user.id, self.second_id), follower_id)

        # The response assembler rejects an A id for a B filter. It must be able
        # to use the returned B id and preserve B's label and policy ownership.
        from app.routers.cache_v2 import query_instances
        with Session(self.engine) as db:
            response = query_instances(credential_id=self.second_id, region=None, round_ids=follower_id,
                                       user=db.get(User, self.user.id), db=db)
        body = json.loads(response.body)
        self.assertEqual(body["rounds"][0]["id"], follower_id)
        self.assertTrue(body["instances"])
        self.assertTrue(all(item["credential_id"] == self.second_id and item["account_label"] == "key B"
                            and item["name"].startswith("private-b-") for item in body["instances"]))

    def test_task_only_follower_keeps_leader_alive_then_last_cancel_revokes_upstream(self):
        self.install_reads()
        first_task, second_task = self.make_task(self.first_id), self.make_task(self.second_id)
        leader_id = self.submit(self.first_id, subscriber=f"task:{first_task}")
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id, subscriber=f"task:{second_task}")
        self.assert_parent(follower_id, leader_id)
        self.cancel_task(first_task)
        self.assertIn(queries.query_view(self.engine, self.user.id, leader_id)["status"], queries.ACTIVE)
        self.cancel_task(second_task)
        self.catalog_release.set()
        self.assertEqual(self.wait(follower_id)["status"], "cancelled")
        self.assertEqual(self.wait(leader_id)["status"], "cancelled")
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertEqual(self.instance_calls, [])

    def test_second_key_permission_denial_never_displays_leader_instances(self):
        self.install_reads(denied_second=True)
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id)
        self.assert_parent(follower_id, leader_id)
        self.catalog_release.set()
        leader, follower = self.wait(leader_id), self.wait(follower_id)
        self.assertEqual(leader["status"], "succeeded")
        self.assertEqual(follower["status"], "failed")
        self.assertTrue(all(target["data"] is None for target in follower["targets"]))
        self.assertTrue(all(target["data"]["instances"][0]["name"].startswith("private-a-")
                            for target in leader["targets"]))
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertEqual({region for key, region in self.instance_calls if key == self.second_key},
                         {"us-east-1", "eu-west-1"})

    def test_leader_catalog_failure_fails_follower_without_second_catalog_scan(self):
        self.install_reads(catalog_failure=True)
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id)
        self.assert_parent(follower_id, leader_id)
        self.catalog_release.set()
        self.assertEqual(self.wait(leader_id)["status"], "failed")
        follower = self.wait(follower_id)
        self.assertEqual(follower["status"], "failed")
        self.assertFalse(follower["targets_fixed"])
        self.assertEqual(follower["targets"], [])
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertEqual(self.instance_calls, [])

    def test_cancelled_follower_does_not_cancel_other_leader_page_subscriber(self):
        self.install_reads()
        second_task = self.make_task(self.second_id)
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id, subscriber=f"task:{second_task}")
        self.cancel_task(second_task)
        self.catalog_release.set()
        self.assertEqual(self.wait(follower_id)["status"], "cancelled")
        self.assertEqual(self.wait(leader_id)["status"], "succeeded")
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertTrue(self.instance_calls)
        self.assertTrue(all(key == self.first_key for key, _region in self.instance_calls))

    def test_explicit_second_key_global_scan_ignores_single_region_filter(self):
        self.install_reads()
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = queries.submit_query(self.engine, self.user.id, self.second_id,
            mode="global", region="us-east-1", force=True)
        self.assert_parent(follower_id, leader_id)
        self.catalog_release.set()
        follower = self.wait(follower_id)
        self.assertEqual(follower["status"], "succeeded")
        self.assertIsNone(follower["region_filter"])
        self.assertEqual({target["region"] for target in follower["targets"]},
                         {"us-east-1", "eu-west-1"})
        self.assertEqual({region for key, region in self.instance_calls if key == self.second_key},
                         {"us-east-1", "eu-west-1"})
        self.assertEqual(self.catalog_calls, [self.first_key])

    def test_second_key_rotation_invalidates_only_its_follower(self):
        self.install_reads()
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id)
        self.assert_parent(follower_id, leader_id)
        with Session(self.engine) as db:
            db.get(AwsCredential, self.second_id).secret_access_key_enc = encrypt_secret("rotated-b-secret")
            db.commit()
        self.assertEqual(self.wait(follower_id)["status"], "invalidated")
        self.assertNotEqual(queries.query_view(self.engine, self.user.id, leader_id)["status"], "invalidated")
        self.catalog_release.set()
        self.assertEqual(self.wait(leader_id)["status"], "succeeded")
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertTrue(all(key == self.first_key for key, _region in self.instance_calls))

    def test_recovering_expired_follower_preserves_live_leader_catalog_lease(self):
        self.install_reads()
        leader_id = queries.submit_query(self.engine, self.user.id, self.first_id,
                                         mode="global", start=False)
        follower_id = queries.submit_query(self.engine, self.user.id, self.second_id,
                                           mode="global", start=False)
        self.assert_parent(follower_id, leader_id)
        with Session(self.engine) as db:
            user, first = db.get(User, self.user.id), db.get(AwsCredential, self.first_id)
            catalog_scope = snapshots.scope_for(user, first, "catalog", "regions")
            leader = db.get(QueryRound, leader_id)
            leader.status, leader.worker_token, leader.lease_until = "discovering", "live-parent", time.time() + 300
            follower = db.get(QueryRound, follower_id)
            follower.status, follower.worker_token, follower.lease_until = "discovering", "dead-follower", time.time() - 1
            db.commit()
        _view, catalog_token = snapshots._read_or_claim(self.engine, catalog_scope, False)
        self.assertIsNotNone(catalog_token)
        worker = queries.worker_for(self.engine)
        with patch.object(worker, "start"):
            queries.resume_queries(self.engine, recover=False)
        with Session(self.engine) as db:
            catalog = db.get(DataSnapshot, catalog_scope["key"])
            self.assertEqual(catalog.lease_token, catalog_token)
            self.assertGreater(catalog.lease_until, time.time())
            self.assertEqual(db.get(QueryRound, leader_id).worker_token, "live-parent")
            follower = db.get(QueryRound, follower_id)
            self.assertEqual(follower.status, "queued")
            self.assertIsNone(follower.worker_token)
            # Now model the parent process also expiring. Its own recovery may
            # fence the old catalog; the follower-only recovery above may not.
            leader = db.get(QueryRound, leader_id)
            leader.lease_until = 0
            db.commit()
        self.catalog_release.set()
        queries.resume_queries(self.engine, recover=False)
        self.assertEqual(self.wait(leader_id)["status"], "succeeded")
        self.assertEqual(self.wait(follower_id)["status"], "succeeded")
        self.assertEqual(self.catalog_calls, [self.first_key])

    def test_cancel_leader_page_before_catalog_keeps_authorized_follower(self):
        self.install_reads()
        leader_id = queries.submit_query(self.engine, self.user.id, self.first_id, start=False)
        follower_id = queries.submit_query(self.engine, self.user.id, self.second_id, start=False)
        self.assert_parent(follower_id, leader_id)
        queries.cancel_query(self.engine, self.user.id, leader_id)
        self.catalog_release.set()
        worker = queries.worker_for(self.engine)
        worker.start(leader_id)
        worker.start(follower_id)
        follower = self.wait(follower_id)
        self.assertEqual(follower["status"], "succeeded")
        self.assertEqual(self.wait(leader_id)["status"], "cancelled")
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertEqual({region for key, region in self.instance_calls if key == self.second_key},
                         {"us-east-1", "eu-west-1"})
        self.assertTrue(all(key == self.second_key for key, _region in self.instance_calls))
        self.assertTrue(all(target["data"]["instances"][0]["name"].startswith("private-b-")
                            for target in follower["targets"]))

    def test_cancel_inflight_leader_page_preserves_follower_permission_checks(self):
        self.install_reads(denied_second=True)
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id)
        queries.cancel_query(self.engine, self.user.id, leader_id)
        self.catalog_release.set()
        follower = self.wait(follower_id)
        self.assertEqual(follower["status"], "failed")
        self.assertEqual(self.wait(leader_id)["status"], "cancelled")
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertTrue(all(target["data"] is None for target in follower["targets"]))
        self.assertEqual(len(self.instance_calls), 2)
        self.assertTrue(all(key == self.second_key for key, _region in self.instance_calls))

    def test_cancel_leader_page_then_last_follower_page_cascades_before_next_send(self):
        self.install_reads()
        leader_id = self.submit(self.first_id)
        self.assertTrue(self.catalog_started.wait(3))
        follower_id = self.submit(self.second_id)
        queries.cancel_query(self.engine, self.user.id, leader_id)
        self.assertIn(queries.query_view(self.engine, self.user.id, leader_id)["status"], queries.ACTIVE)
        queries.cancel_query(self.engine, self.user.id, follower_id)
        self.catalog_release.set()
        self.assertEqual(self.wait(follower_id)["status"], "cancelled")
        self.assertEqual(self.wait(leader_id)["status"], "cancelled")
        self.assertEqual(self.catalog_calls, [self.first_key])
        self.assertEqual(self.instance_calls, [])

    def test_rotated_follower_cannot_keep_cancelled_leader_page_alive(self):
        self.install_reads()
        leader_id = queries.submit_query(self.engine, self.user.id, self.first_id, start=False)
        follower_id = queries.submit_query(self.engine, self.user.id, self.second_id, start=False)
        with Session(self.engine) as db:
            db.get(AwsCredential, self.second_id).secret_access_key_enc = encrypt_secret("changed-b")
            db.commit()
        result = queries.cancel_query(self.engine, self.user.id, leader_id)
        self.assertEqual(result["status"], "cancelled")
        self.catalog_release.set()
        queries.worker_for(self.engine).start(leader_id)
        queries.worker_for(self.engine).start(follower_id)
        self.assertEqual(self.wait(follower_id)["status"], "invalidated")
        self.assertEqual(self.catalog_calls, [])
        self.assertEqual(self.instance_calls, [])
