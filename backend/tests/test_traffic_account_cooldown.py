"""Real file-backed traffic tasks honor account/action cooldowns across keys."""
from copy import deepcopy
from datetime import datetime, timezone
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
sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]

from botocore.exceptions import ClientError
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AwsCredential, User
from app.query_models import QueryRegion
from app.security import encrypt_secret
from app.services.collector import sync_user_traffic
from app.services.read_cooldowns import ReadCooldownStore
from app.services.snapshots import scope_for
from app.services.task_queue import DurableTaskQueue, queue_for
from app.services.task_submission import submit
from task_fakes import FakeLightsail, seed_discovered_regions


ACCOUNT = "123456789012"
EAST, WEST = "us-east-1", "eu-west-1"


class AccountAws(FakeLightsail):
    """Observe actual fake dispatch, preserving the production factory/limiter."""
    def __init__(self):
        super().__init__(name="web")
        self.dispatched = []
        self.reject = None
        self.constructed = None

    def factory(self, service, **kwargs):
        client = super().factory(service, **kwargs)
        original = client._make_api_call
        key, region = kwargs["aws_access_key_id"], kwargs["region_name"]
        def invoke(operation, parameters):
            self.dispatched.append((key, region, operation, deepcopy(parameters)))
            if self.reject:
                self.reject(key, region, operation, parameters)
            return original(operation, parameters)
        client._make_api_call = invoke
        if self.constructed:
            self.constructed(key, region)
        return client


class TrafficAccountCooldownTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'traffic.db'}",
                                    connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.queue = queue_for(self.engine)
        self.assertIsInstance(self.queue, DurableTaskQueue)
        self.addCleanup(lambda: self.queue.shutdown(wait=True))
        self.keys = {index: f"AKIAIOSFODNN7EXAMPL{index}" for index in (1, 2, 3, 4)}
        with Session(self.engine) as db:
            owner, other = User(username="owner", password_hash="opaque"), User(username="other", password_hash="opaque")
            db.add_all([owner, other])
            db.flush()
            self.user_id, self.other_id = owner.id, other.id
            for index, user_id, account in ((1, owner.id, ACCOUNT), (2, owner.id, ACCOUNT),
                                             (3, other.id, ACCOUNT), (4, owner.id, "210987654321")):
                db.add(AwsCredential(id=index, user_id=user_id, account_id=account,
                    is_default=index == 1, access_key_id_enc=encrypt_secret(self.keys[index]),
                    secret_access_key_enc=encrypt_secret(f"fake-key-{index}")))
            db.commit()
            seed_discovered_regions(db, owner.id, ACCOUNT, EAST, WEST)
            seed_discovered_regions(db, other.id, ACCOUNT, EAST, WEST)
            seed_discovered_regions(db, owner.id, "210987654321", EAST, WEST)
        self.aws = AccountAws()
        fake = self.aws.installed()
        fake.__enter__()
        self.addCleanup(fake.__exit__, None, None, None)
        self.transport = patch("botocore.endpoint.Endpoint._send",
                               side_effect=AssertionError("AWS transport forbidden")).start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: self.assertEqual(self.transport.call_count, 0))

    def task(self, credential_id, *, scheduled=False, key="intent"):
        with Session(self.engine) as db:
            user = db.get(User, self.other_id if credential_id == 3 else self.user_id)
            if scheduled:
                rows = sync_user_traffic(db, user, credential_id,
                                        idempotency_key=f"scheduler:{key}:{user.id}:{ACCOUNT}")
                state = rows[0]
            else:
                state = submit(db, user, "traffic_sync", credential_id,
                               idempotency_key=f"manual:{key}:{credential_id}")
        return self.wait(state["task_id"], self.other_id if credential_id == 3 else self.user_id)

    def wait(self, task_id, user_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with Session(self.engine) as db:
                state = self.queue.get(db, user_id, task_id)
            if state["status"] not in {"queued", "running"}:
                return state
            time.sleep(.01)
        self.fail(f"Task did not finish: {state}")

    def sent(self, credential_id, *, region=None, operation=None):
        return [row for row in self.aws.dispatched if row[0] == self.keys[credential_id]
                and (region is None or row[1] == region) and (operation is None or row[2] == operation)]

    @staticmethod
    def throttle(operation, code="ThrottlingException", status=429):
        return ClientError({"Error": {"Code": code, "Message": "rejected"},
            "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": {"retry-after": "120"}}}, operation)

    def reject_first(self, operation, *, code="ThrottlingException", status=429):
        def reject(key, region, current, _parameters):
            if key == self.keys[1] and region == EAST and current == operation:
                raise self.throttle(current, code=code, status=status)
        self.aws.reject = reject

    def test_instances_throttle_blocks_same_account_scheduled_key_but_not_region_user_or_account(self):
        self.reject_first("GetInstances")
        first = self.task(1, key="first")
        self.assertEqual(first["status"], "partial", first)
        first_failure = next(error for error in first["result"]["errors"] if error["region"] == EAST)
        self.assertEqual(first_failure["code"], "ThrottlingException")
        remaining = datetime.fromisoformat(first_failure["next_retry_at"]).timestamp() - time.time()
        self.assertGreater(remaining, 115)
        self.aws.reject = None
        second = self.task(2, scheduled=True, key="second")
        self.assertEqual(second["status"], "partial", second)
        self.assertEqual(self.sent(2, region=EAST, operation="GetInstances"), [])
        self.assertEqual(len(self.sent(2, region=WEST, operation="GetInstances")), 1)
        self.assertEqual(second["result"]["count"], 1)
        second_failure = next(error for error in second["result"]["errors"] if error["region"] == EAST)
        self.assertEqual(second_failure["next_retry_at"], first_failure["next_retry_at"])
        self.assertEqual(second_failure["code"], "ThrottlingException")
        self.assertEqual(self.task(3, key="foreign-user")["status"], "succeeded")
        self.assertEqual(self.task(4, scheduled=True, key="foreign-account")["status"], "succeeded")
        self.assertEqual(len(self.sent(3, region=EAST, operation="GetInstances")), 1)
        self.assertEqual(len(self.sent(4, region=EAST, operation="GetInstances")), 1)
        self.assertEqual(self.aws.count("StopInstance"), 0)

    def test_metric_throttle_does_not_short_circuit_instance_action_or_other_region(self):
        self.reject_first("GetInstanceMetricData")
        first = self.task(1, scheduled=True, key="metric-first")
        self.assertEqual(first["status"], "partial", first)
        first_failure = next(error for error in first["result"]["errors"] if error["region"] == EAST)
        self.aws.reject = None
        second = self.task(2, key="metric-second")
        self.assertEqual(second["status"], "partial", second)
        self.assertEqual(len(self.sent(2, region=EAST, operation="GetInstances")), 1)
        self.assertEqual(self.sent(2, region=EAST, operation="GetInstanceMetricData"), [])
        self.assertEqual(len(self.sent(2, region=WEST, operation="GetInstanceMetricData")), 2)
        second_failure = next(error for error in second["result"]["errors"] if error["region"] == EAST)
        self.assertEqual(second_failure["next_retry_at"], first_failure["next_retry_at"])
        self.assertEqual(second["result"]["count"], 1)

    def test_permission_failure_on_one_key_does_not_cooldown_another_key(self):
        self.reject_first("GetInstances", code="AccessDeniedException", status=403)
        first = self.task(1, key="denied-first")
        self.assertEqual(first["status"], "partial", first)
        self.assertEqual(first["result"]["errors"][0]["code"], "AccessDeniedException")
        self.aws.reject = None
        second = self.task(2, scheduled=True, key="denied-second")
        self.assertEqual(second["status"], "succeeded", second)
        self.assertEqual(len(self.sent(2, region=EAST, operation="GetInstances")), 1)
        self.assertEqual(second["result"]["count"], 2)

    def test_new_cooldown_after_task_enters_send_queue_blocks_actual_dispatch(self):
        from app.services import aws_limits
        with Session(self.engine) as db:
            db.execute(delete(QueryRegion).where(QueryRegion.user_id == self.user_id, QueryRegion.region == WEST))
            store = ReadCooldownStore(self.engine, scope_for(db.get(User, self.user_id),
                db.get(AwsCredential, 1), "traffic-read", EAST))
            db.commit()
        entered = threading.Event()
        self.aws.constructed = lambda key, region: entered.set() if key == self.keys[2] and region == EAST else None
        acquired = 0
        try:
            for _ in range(5):
                self.assertTrue(aws_limits._all.acquire(timeout=1))
                acquired += 1
            with Session(self.engine) as db:
                state = submit(db, db.get(User, self.user_id), "traffic_sync", 2,
                               idempotency_key="manual:queued-second")
            self.assertTrue(entered.wait(2), "Traffic did not reach the real shared network-slot queue")
            saved = store.record(self.throttle("GetInstances"), EAST, "lightsail", "GetInstances")
        finally:
            for _ in range(acquired):
                aws_limits._all.release()
        result = self.wait(state["task_id"], self.user_id)
        self.assertEqual(result["status"], "partial", result)
        self.assertEqual(self.sent(2, region=EAST, operation="GetInstances"), [])
        self.assertEqual(result["result"]["count"], 0)
        self.assertEqual(result["result"]["errors"][0]["next_retry_at"], saved["next_retry_at"])
