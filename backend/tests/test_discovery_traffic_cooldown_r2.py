"""R2: discovery and durable traffic share each GetInstances send boundary."""
import threading
import time
import unittest
from unittest.mock import patch

import boto3
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import AwsCredential, DataSnapshot, User
from app.services import aws_limits, lightsail, query_rounds
from app.services.read_cooldowns import ReadCooldownActive, cooldown_context
from app.services.read_throttle import ReadThrottle, read_context, read_scope
import test_traffic_account_cooldown as traffic_tests


class DiscoveryTrafficCooldownR2Tests(unittest.TestCase):
    setUpFixture = traffic_tests.TrafficAccountCooldownTests.setUp
    task = traffic_tests.TrafficAccountCooldownTests.task
    wait = traffic_tests.TrafficAccountCooldownTests.wait
    sent = traffic_tests.TrafficAccountCooldownTests.sent
    throttle = staticmethod(traffic_tests.TrafficAccountCooldownTests.throttle)
    reject_first = traffic_tests.TrafficAccountCooldownTests.reject_first

    def setUp(self):
        self.setUpFixture()
        self.addCleanup(query_rounds.shutdown_queries, self.engine)
        self.http_transport = patch("botocore.httpsession.URLLib3Session.send",
            side_effect=AssertionError("HTTP transport forbidden")).start()
        self.addCleanup(lambda: self.assertEqual(self.http_transport.call_count, 0))

    def query(self, credential_id, *, force=True):
        with Session(self.engine) as db:
            user_id = db.get(AwsCredential, credential_id).user_id
        round_id = query_rounds.submit_query(self.engine, user_id, credential_id,
            mode="refresh", region=traffic_tests.EAST, force=force)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            result = query_rounds.query_view(self.engine, user_id, round_id)
            if result["status"] in query_rounds.TERMINAL:
                return result
            time.sleep(.01)
        self.fail(f"Discovery did not finish: {result}")

    def store(self, credential_id):
        from app.services.read_cooldowns import ReadCooldownStore
        from app.services.snapshots import scope_for
        with Session(self.engine) as db:
            credential = db.get(AwsCredential, credential_id)
            return ReadCooldownStore(self.engine, scope_for(db.get(User, credential.user_id), credential,
                                                           "query-instances", traffic_tests.EAST))

    def test_traffic_throttle_stops_discovery_on_second_key_and_keeps_original_deadline(self):
        self.reject_first("GetInstances")
        first = self.task(1, key="traffic-before-query")
        self.assertEqual(first["status"], "partial", first)
        failure = next(value for value in first["result"]["errors"] if value["region"] == traffic_tests.EAST)
        self.aws.reject = None
        result = self.query(2)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.sent(2, region=traffic_tests.EAST, operation="GetInstances"), [])
        self.assertEqual(result["targets"][0]["next_retry_at"], failure["next_retry_at"])
        self.assertEqual(self.store(2).active(traffic_tests.EAST, "lightsail", "GetInstances")["next_retry_at"],
                         failure["next_retry_at"])

    def test_discovery_throttle_stops_second_key_manual_and_scheduled_traffic(self):
        self.reject_first("GetInstances")
        result = self.query(1)
        self.assertEqual(result["status"], "failed", result)
        failure = self.store(1).active(traffic_tests.EAST, "lightsail", "GetInstances")
        self.assertIsNotNone(failure)
        self.assertEqual(failure["code"], "ThrottlingException")
        self.aws.reject = None
        for scheduled in (False, True):
            with self.subTest(scheduled=scheduled):
                task = self.task(2, scheduled=scheduled, key=f"query-before-traffic-{scheduled}")
                self.assertEqual(task["status"], "partial", task)
                error = next(value for value in task["result"]["errors"] if value["region"] == traffic_tests.EAST)
                self.assertEqual(error["next_retry_at"], failure["next_retry_at"])
        self.assertEqual(self.sent(2, region=traffic_tests.EAST, operation="GetInstances"), [])
        self.assertEqual(len(self.sent(2, region=traffic_tests.WEST, operation="GetInstances")), 2)

    def test_force_discovery_with_less_than_sixty_seconds_remaining_never_extends_account_deadline(self):
        self.reject_first("GetInstances")
        first = self.task(1, key="traffic-cooldown-near-expiry")
        self.assertEqual(first["status"], "partial", first)
        original = next(value for value in first["result"]["errors"] if value["region"] == traffic_tests.EAST)
        saved = self.store(1).active(traffic_tests.EAST, "lightsail", "GetInstances")
        self.aws.reject = None
        # Advance the shared wall clock, rather than weakening the persisted
        # failure: the first key's real task established this 120-second range.
        with patch("app.services.read_cooldowns.time.time", return_value=saved["retry_at"] - 10):
            for _ in range(2):
                result = self.query(2, force=True)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["targets"][0]["next_retry_at"], original["next_retry_at"])
                self.assertEqual(self.store(2).active(traffic_tests.EAST, "lightsail", "GetInstances")["retry_at"],
                                 saved["retry_at"])
        self.assertEqual(self.sent(2, region=traffic_tests.EAST, operation="GetInstances"), [])

    def test_traffic_permission_and_auth_failures_do_not_block_discovery_other_key(self):
        for code in ("AccessDeniedException", "ExpiredToken"):
            with self.subTest(code=code):
                with Session(self.engine) as db:
                    db.execute(delete(DataSnapshot).where(DataSnapshot.credential_id == 1,
                        DataSnapshot.kind.in_(("read-cooldown-key", "query-instances"))))
                    db.commit()
                self.reject_first("GetInstances", code=code, status=403)
                first = self.task(1, key=f"isolated-traffic-{code}")
                self.assertEqual(first["status"], "partial", first)
                self.aws.reject = None
                result = self.query(2)
                self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(self.sent(2, region=traffic_tests.EAST, operation="GetInstances")), 2)

    def test_discovery_permission_and_auth_failures_do_not_block_traffic_other_key(self):
        from app.services import billing
        for code in ("AccessDeniedException", "ExpiredToken"):
            with self.subTest(code=code):
                with Session(self.engine) as db:
                    db.execute(delete(DataSnapshot).where(DataSnapshot.credential_id == 1,
                        DataSnapshot.kind.in_(("read-cooldown-key", "query-instances"))))
                    db.commit()
                self.reject_first("GetInstances", code=code, status=403)
                with patch.object(billing, "get_sts_identity", return_value=traffic_tests.ACCOUNT):
                    result = self.query(1)
                self.assertEqual(result["status"], "failed", result)
                self.aws.reject = None
                task = self.task(2, scheduled=True, key=f"isolated-query-{code}")
                self.assertEqual(task["status"], "succeeded", task)
        self.assertEqual(len(self.sent(2, region=traffic_tests.EAST, operation="GetInstances")), 2)

    def test_next_discovery_page_observes_account_throttle_before_dispatch(self):
        original = self.aws.call
        store = self.store(1)
        saved = []
        def page(operation, parameters):
            if operation == "GetInstances":
                saved.append(store.record(self.throttle(operation), traffic_tests.EAST, "lightsail", operation))
                return {"instances": [{"name": "partial", "state": {"name": "stopped"}}], "nextPageToken": "second"}
            return original(operation, parameters)
        with patch.object(self.aws, "call", side_effect=page):
            result = self.query(2)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(len(self.sent(2, region=traffic_tests.EAST, operation="GetInstances")), 1)
        self.assertIsNone(result["targets"][0]["data"])
        self.assertEqual(result["targets"][0]["next_retry_at"], saved[0]["next_retry_at"])

    def sdk_client(self, send):
        client = boto3.session.Session().client("lightsail", region_name=traffic_tests.EAST,
            aws_access_key_id=self.keys[2], aws_secret_access_key="fake-secret",
            config=Config(connect_timeout=1, read_timeout=1, retries={"mode": "standard", "total_max_attempts": 2}))
        # The instance-local endpoint is an in-process response source. The
        # class Endpoint._send and underlying HTTP session remain deny guards.
        client._endpoint._send = send
        self.addCleanup(client.close)
        return aws_limits.limit_client(client, traffic_tests.EAST)

    def test_sdk_retry_send_rechecks_new_cooldown_without_extending_deadline(self):
        saved, sends = [], []
        store = self.store(1)
        class Raw:
            def stream(self, *_args, **_kwargs):
                yield b'{"__type":"ServiceException","message":"offline retry"}'
        def response(request):
            sends.append(request)
            saved.append(store.record(self.throttle("GetInstances"), traffic_tests.EAST, "lightsail", "GetInstances"))
            return AWSResponse(request.url, 500, {}, Raw())
        client = self.sdk_client(response)
        with patch.object(lightsail, "make_client", return_value=client), \
                patch.object(aws_limits, "read_throttle", ReadThrottle(lambda: 0)), \
                patch("botocore.endpoint.time.sleep"), \
                read_context(read_scope(self.user_id, traffic_tests.ACCOUNT, 2)), cooldown_context(self.store(2)):
            with self.assertRaises(ReadCooldownActive) as caught:
                lightsail.discover_instances_in_region(self.keys[2], "fake", traffic_tests.EAST)
        self.assertEqual(len(sends), 1)
        self.assertEqual(caught.exception.failure["retry_at"], saved[0]["retry_at"])

    def test_sdk_queued_send_checks_cooldown_committed_after_joining_queue(self):
        sends, errors = [], []
        def response(request):
            sends.append(request)
            self.fail("Queued discovery reached endpoint after account cooldown committed")
        client = self.sdk_client(response)
        throttle = ReadThrottle(lambda: 0)
        def run():
            with read_context(read_scope(self.user_id, traffic_tests.ACCOUNT, 2)), cooldown_context(self.store(2)):
                try:
                    lightsail.discover_instances_in_region(self.keys[2], "fake", traffic_tests.EAST)
                except BaseException as exc:
                    errors.append(exc)
        held = 0
        worker = threading.Thread(target=run)
        with patch.object(lightsail, "make_client", return_value=client), patch.object(aws_limits, "read_throttle", throttle):
            try:
                for _ in range(5):
                    self.assertTrue(aws_limits._all.acquire(timeout=1))
                    held += 1
                worker.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    with throttle.condition:
                        if throttle.turns:
                            break
                    time.sleep(.01)
                else:
                    self.fail(f"Discovery never entered actual send queue: {errors}")
                saved = self.store(1).record(self.throttle("GetInstances"), traffic_tests.EAST, "lightsail", "GetInstances")
            finally:
                for _ in range(held):
                    aws_limits._all.release()
                throttle.settings_changed()
                worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(sends, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ReadCooldownActive)
        self.assertEqual(errors[0].failure["retry_at"], saved["retry_at"])
