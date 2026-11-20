"""Durable workflows under stateful side effects, response loss, and restart."""
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import Session

from task_fakes import FakeLightsail, seed_discovered_regions
import test_t06_task_queue as queue_tests
import unittest
from app.models import AwsCredential, OperationTask, OperationTaskLock, OperationTaskStep, User
from app.security import encrypt_secret
from app.services import task_handlers as handlers
from app.services.task_queue import TaskContext, TaskLeaseLost, TaskNotFound, TaskConflict


class WorkflowTests(unittest.TestCase):
    tearDown = queue_tests.DurableTaskQueueTests.tearDown
    def setUp(self):
        queue_tests.DurableTaskQueueTests.setUp(self)
        handlers.install_handlers()
        self.aws = FakeLightsail(name="web")
        self.fake = self.aws.installed()
        self.fake.__enter__()
        self.addCleanup(self.fake.__exit__, None, None, None)
        resolver = patch("app.proxy_config._resolve_host_ips", return_value=["8.8.8.8"])
        resolver.start()
        self.addCleanup(resolver.stop)

    def submit(self, kind="stop", payload=None, key="intent", wait=True):
        if payload is None:
            payload = {"kind": kind, "region": "us-east-1", "resource_name": "web", "credential_id": 1}
        with Session(self.engine) as db:
            return self.queue.accept(db, db.get(User, self.user_id), db.get(AwsCredential, 1), kind,
                payload, key, region="us-east-1", resource_name="web", wait=wait)

    def state(self, task_id):
        with Session(self.engine) as db:
            return self.queue.get(db, self.user_id, task_id)

    def locks(self, task_id):
        with Session(self.engine) as db:
            return len(db.scalars(select(OperationTaskLock).where(OperationTaskLock.task_id == task_id)).all())

    def resume(self, task_id):
        with Session(self.engine) as db:
            self.queue.reconcile(db, self.user_id, task_id)
        with patch.object(self.queue, "schedule"), Session(self.engine) as db:
            self.queue.retry(db, self.user_id, task_id)
        self.queue._execute(task_id)
        return self.state(task_id)

    def test_stop_lost_response_reconciles_without_replay(self):
        self.aws.lose.add("StopInstance")
        result = self.submit()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(self.locks(result["task_id"]), 1)
        self.assertEqual(self.resume(result["task_id"])["status"], "succeeded")
        self.assertEqual(self.aws.count("StopInstance"), 1)
        self.assertEqual(self.locks(result["task_id"]), 0)

    def test_reboot_running_does_not_prove_lost_response(self):
        self.aws.lose.add("RebootInstance")
        result = self.submit("reboot")
        with Session(self.engine) as db:
            result = self.queue.reconcile(db, self.user_id, result["task_id"])
            with self.assertRaises(TaskConflict):
                self.queue.retry(db, self.user_id, result["task_id"])
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(self.aws.count("RebootInstance"), 1)
        self.assertEqual(self.locks(result["task_id"]), 1)

    def test_prewrite_recreated_resource_is_rejected(self):
        def replace(operation, parameters):
            if operation == "GetInstance" and self.aws.count(operation) == 3:
                self.aws.instances["web"]["arn"] = "replacement"
        self.aws.before = replace
        result = self.submit()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(self.aws.count("StopInstance"), 0)
        self.assertEqual(self.locks(result["task_id"]), 1)

    def test_lost_stop_does_not_claim_same_name_replacement(self):
        self.aws.lose.add("StopInstance")
        result = self.submit()
        self.aws.instances["web"]["arn"] = "replacement"
        with Session(self.engine) as db:
            result = self.queue.reconcile(db, self.user_id, result["task_id"])
        self.assertFalse(result["can_resume"])
        self.assertEqual(self.locks(result["task_id"]), 1)

    def test_create_lost_allocate_second_precision_and_proxy(self):
        self.aws.instances.clear()
        self.aws.lose.add("AllocateStaticIp")
        with Session(self.engine) as db:
            db.get(AwsCredential, 1).proxy_url_enc = encrypt_secret("http://alice:fake-password@proxy.example.com:8080")
            db.commit()
        result = self.submit("create", {"region": "us-east-1", "instance_name": "web",
            "blueprint_id": "ubuntu_22_04", "bundle_id": "nano_3_0", "allocate_static_ip": True,
            "password": "SomePassword123!", "open_all_ports": True})
        self.assertEqual(result["status"], "reconciliation_required")
        result = self.resume(result["task_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(self.aws.count("CreateInstances"), 1)
        self.assertEqual(self.aws.count("AllocateStaticIp"), 1)
        self.assertTrue(all(c["config"].proxies == {"http": "http://alice:fake-password@proxy.example.com:8080",
            "https": "http://alice:fake-password@proxy.example.com:8080"} for c in self.aws.clients))
        for (operation, _parameters), config in zip(self.aws.calls, self.aws.call_configs):
            expected_attempts = 1 if operation[:1] not in {"G", "L", "D", "H"} else 2
            self.assertEqual(config.retries["total_max_attempts"], expected_attempts, operation)
        serialized = json.dumps(result)
        self.assertNotIn("SomePassword", serialized)
        self.assertNotIn("fake-password", serialized)

    def test_attach_started_keeps_old_ip_and_lock(self):
        self.aws.ips["old"] = self.aws.static_ip("old", "web")
        self.aws.pending.add("AttachStaticIp")
        result = self.submit("change_ip")
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertIn("old", self.aws.ips)
        self.assertEqual(self.aws.count("ReleaseStaticIp"), 0)
        self.assertEqual(self.locks(result["task_id"]), 1)
        pending = next(op for op in self.aws.operations.values() if not op["isTerminal"])
        self.assertTrue(any(step["operation_id"] == pending["id"] for step in result["steps"]))

    def test_lost_attach_resumes_cleanup_only(self):
        self.aws.ips["old"] = self.aws.static_ip("old", "web")
        self.aws.lose.add("AttachStaticIp")
        result = self.submit("change_ip")
        self.assertIn("old", self.aws.ips)
        result = self.resume(result["task_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertNotIn("old", self.aws.ips)
        self.assertEqual(self.aws.count("AttachStaticIp"), 1)

    def test_delete_pending_never_cleans_ips(self):
        self.aws.ips["attached"] = self.aws.static_ip("attached", "web")
        self.aws.pending.add("DeleteInstance")
        result = self.submit("delete")
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(self.aws.count("ReleaseStaticIp"), 0)

    def test_delete_lost_response_only_releases_recorded_association(self):
        self.aws.ips["attached"] = self.aws.static_ip("attached", "web")
        self.aws.ips["web-sip-unrelated"] = self.aws.static_ip("web-sip-unrelated", "someone-else")
        self.aws.lose.add("DeleteInstance")
        result = self.submit("delete")
        result = self.resume(result["task_id"])
        self.assertEqual(result["status"], "succeeded", result)
        self.assertNotIn("attached", self.aws.ips)
        self.assertIn("web-sip-unrelated", self.aws.ips)
        self.assertEqual(self.aws.count("DeleteInstance"), 1)

    def test_old_lease_cannot_write_result_or_release_lock(self):
        with patch.object(self.queue, "schedule"):
            result = self.submit(wait=False)
        with Session(self.engine) as db:
            task = db.get(OperationTask, result["task_id"])
            task.status, task.worker_token, task.lease_until = "running", "token", time.time() + 60
            db.commit()
        context = TaskContext(self.engine, result["task_id"], "token", {})
        seq = context.begin_step("mutation", {"mutation": True})
        with Session(self.engine) as db:
            db.get(OperationTask, result["task_id"]).lease_until = time.time() - 1
            db.commit()
        for operation in (lambda: context.record_operation(seq, {"operations": [{"id": "late"}]}),
                          lambda: context.finish("succeeded")):
            with self.assertRaises(TaskLeaseLost): operation()
        self.assertEqual(self.locks(result["task_id"]), 1)
        self.queue.recover()
        self.assertEqual(self.state(result["task_id"])["status"], "reconciliation_required")
        self.assertEqual(self.aws.count("StopInstance"), 0)

    def test_changed_credential_at_transport_gate_prevents_write(self):
        from app.services import aws_limits
        original = aws_limits.call_with_limits
        def change(fn, operation, parameters, region="", before_call=None):
            if operation == "StopInstance":
                with Session(self.engine) as db:
                    db.get(AwsCredential, 1).secret_access_key_enc = encrypt_secret("replacement")
                    db.commit()
            return original(fn, operation, parameters, region, before_call)
        with patch.object(aws_limits, "call_with_limits", side_effect=change):
            result = self.submit()
        self.assertEqual(result["status"], "reconciliation_required")
        self.assertEqual(self.aws.count("StopInstance"), 0)

    def test_interrupted_read_plan_can_resume(self):
        with patch.object(self.queue, "schedule"):
            result = self.submit(wait=False)
        with Session(self.engine) as db:
            task = db.get(OperationTask, result["task_id"])
            task.status, task.worker_token, task.lease_until = "running", "token", time.time() + 60
            db.commit()
        context = TaskContext(self.engine, result["task_id"], "token", {})
        context.begin_step("resource_identity", {"mutation": False})
        self.queue.recover()
        self.assertEqual(self.resume(result["task_id"])["status"], "succeeded")

    def test_owner_scope_and_retention_never_remove_unresolved(self):
        self.aws.lose.add("RebootInstance")
        result = self.submit("reboot")
        with Session(self.engine) as db:
            with self.assertRaises(TaskNotFound): self.queue.get(db, self.user_id + 1, result["task_id"])
            with self.assertRaises(TaskNotFound): self.queue.cancel(db, self.user_id + 1, result["task_id"])
            task = db.get(OperationTask, result["task_id"])
            task.finished_at = datetime.now(timezone.utc) - timedelta(days=31)
            db.commit()
        self.assertEqual(self.queue.purge_terminal(), 0)
        self.assertEqual(self.locks(result["task_id"]), 1)

    def test_credential_rotation_before_effect_releases_lock_as_known_failure(self):
        with patch.object(self.queue, "schedule"):
            result = self.submit(wait=False)
        with Session(self.engine) as db:
            db.get(AwsCredential, 1).secret_access_key_enc = encrypt_secret("rotated")
            db.commit()
        self.queue._execute(result["task_id"])
        self.assertEqual(self.state(result["task_id"])["status"], "failed")
        self.assertEqual(self.locks(result["task_id"]), 0)
        self.assertEqual(self.aws.calls, [])

    def test_explicit_resolution_preserves_unknown_evidence_and_requires_owner(self):
        self.aws.lose.add("RebootInstance")
        result = self.submit("reboot")
        with Session(self.engine) as db:
            with self.assertRaises(TaskNotFound):
                self.queue.resolve(db, self.user_id + 1, result["task_id"], result["task_id"])
            db.rollback()
            with self.assertRaises(TaskConflict):
                self.queue.resolve(db, self.user_id, result["task_id"], "wrong")
            db.rollback()
            resolved = self.queue.resolve(db, self.user_id, result["task_id"], result["task_id"])
        self.assertTrue(resolved["result"]["manual_resolution"])
        self.assertFalse(resolved["result"]["aws_outcome_confirmed"])
        self.assertTrue(any(step["status"] == "unknown" for step in resolved["steps"]))
        self.assertEqual(self.locks(result["task_id"]), 0)

    def test_firewall_failure_can_resume_without_repeating_create(self):
        self.aws.instances.clear()
        self.aws.fail.add("PutInstancePublicPorts")
        result = self.submit("create", {"region": "us-east-1", "instance_name": "web",
            "blueprint_id": "ubuntu_22_04", "bundle_id": "nano_3_0", "allocate_static_ip": False,
            "open_all_ports": True})
        self.assertEqual(result["status"], "reconciliation_required")
        self.aws.fail.clear()
        self.assertEqual(self.resume(result["task_id"])["status"], "succeeded")
        self.assertEqual(self.aws.count("CreateInstances"), 1)
        self.assertEqual(self.aws.count("PutInstancePublicPorts"), 2)

    def test_create_ip_compensated_failure_reports_existing_instance_as_partial(self):
        from app.models import InstanceSetting
        self.aws.instances.clear()
        self.aws.fail.add("AttachStaticIp")
        result = self.submit("create", {"region": "us-east-1", "instance_name": "web",
            "blueprint_id": "ubuntu_22_04", "bundle_id": "nano_3_0", "allocate_static_ip": True})
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(result["result"]["name"], "web")
        self.assertTrue(result["result"]["step_errors"])
        self.assertIn("web", self.aws.instances)
        self.assertEqual(self.aws.ips, {})
        self.assertEqual(self.locks(result["task_id"]), 0)
        with Session(self.engine) as db:
            self.assertIsNotNone(db.scalar(select(InstanceSetting).where(InstanceSetting.instance_name == "web")))

    def test_resumed_queued_task_cannot_be_cancelled_and_release_effect_locks(self):
        self.aws.lose.add("StopInstance")
        result = self.submit()
        with Session(self.engine) as db:
            self.queue.reconcile(db, self.user_id, result["task_id"])
        with patch.object(self.queue, "schedule"), Session(self.engine) as db:
            self.queue.retry(db, self.user_id, result["task_id"])
        with Session(self.engine) as db:
            with self.assertRaises(TaskConflict):
                self.queue.cancel(db, self.user_id, result["task_id"])
        self.assertEqual(self.locks(result["task_id"]), 1)

    def test_old_traffic_window_cannot_overwrite_newer_cumulative_observation(self):
        from app.models import TrafficUsage
        now = datetime.now(timezone.utc)
        with Session(self.engine) as db:
            seed_discovered_regions(db, self.user_id, "123456789012", "us-east-1")
            user = db.get(User, self.user_id)
            user.auto_stop_on_limit_default, user.monthly_limit_gb = True, 1
            db.add(TrafficUsage(user_id=self.user_id, credential_id=1, account_id="123456789012",
                region="us-east-1", instance_name="web", year_month=now.strftime("%Y-%m"),
                network_in_bytes=900, network_out_bytes=900, last_synced_at=now))
            db.commit()
        self.aws.metric = 2 * 1024**3
        end = now - timedelta(seconds=1)
        result = self.submit("traffic_sync", {"window_start": now.replace(day=1, hour=0, minute=0, second=0,
            microsecond=0).isoformat(), "window_end": end.isoformat(), "year_month": now.strftime("%Y-%m")})
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["count"], 0)
        self.assertEqual(self.aws.count("StopInstance"), 0)
        with Session(self.engine) as db:
            row = db.scalar(select(TrafficUsage))
            self.assertEqual((row.network_in_bytes, row.network_out_bytes), (900, 900))

    def test_policy_disabled_during_prewrite_read_stops_auto_shutdown(self):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as db:
            seed_discovered_regions(db, self.user_id, "123456789012", "us-east-1")
            user = db.get(User, self.user_id)
            user.auto_stop_on_limit_default, user.monthly_limit_gb = True, 1
            db.commit()
        def disable(operation, parameters):
            if operation == "GetInstance" and self.aws.count(operation) == 2:
                with Session(self.engine) as db:
                    db.get(User, self.user_id).auto_stop_on_limit_default = False
                    db.commit()
        self.aws.before = disable
        result = self.submit("traffic_sync", {"window_start": now.replace(day=1, hour=0, minute=0, second=0,
            microsecond=0).isoformat(), "window_end": now.isoformat(), "year_month": now.strftime("%Y-%m")})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.aws.count("StopInstance"), 0)
