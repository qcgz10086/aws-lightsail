"""R3: compatibility instance routes obey the same account cooldown and guards."""
import asyncio
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

import boto3
from botocore.config import Config
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.models import AwsCredential, DataSnapshot, User
from app.routers import cache_v2, instances
from app.security import encrypt_secret
from app.services import aws_limits, instance_cache, lightsail, snapshots
from app.services.read_cooldowns import ReadCooldownStore
from app.services.read_throttle import ReadThrottle
import test_traffic_account_cooldown as traffic_tests


class CompatibilityInstanceCooldownTests(unittest.TestCase):
    task = traffic_tests.TrafficAccountCooldownTests.task
    wait = traffic_tests.TrafficAccountCooldownTests.wait
    sent = traffic_tests.TrafficAccountCooldownTests.sent
    throttle = staticmethod(traffic_tests.TrafficAccountCooldownTests.throttle)
    reject_first = traffic_tests.TrafficAccountCooldownTests.reject_first

    def setUp(self):
        traffic_tests.TrafficAccountCooldownTests.setUp(self)
        self.http_transport = patch("botocore.httpsession.URLLib3Session.send",
            side_effect=AssertionError("AWS HTTP transport forbidden")).start()
        self.addCleanup(lambda: self.assertEqual(self.http_transport.call_count, 0))
        application = FastAPI()
        application.include_router(cache_v2.router, prefix="/api")
        application.include_router(instances.router, prefix="/api")

        def database():
            with Session(self.engine) as db:
                yield db

        def authenticated(db=Depends(get_db)):
            return db.get(User, self.user_id)

        application.dependency_overrides[get_db] = database
        application.dependency_overrides[get_current_user] = authenticated
        self.client = TestClient(application)
        self.addCleanup(self.client.close)
        self.addCleanup(self.finish_refreshes)

    def finish_refreshes(self):
        with Session(self.engine) as db:
            keys = db.scalars(select(DataSnapshot.key)).all()
        for key in keys:
            snapshots.wait_for_refresh(key)

    def scope(self, credential_id, kind="instances"):
        with Session(self.engine) as db:
            credential = db.get(AwsCredential, credential_id)
            return snapshots.scope_for(db.get(User, credential.user_id), credential, kind, traffic_tests.EAST)

    def store(self, credential_id):
        return ReadCooldownStore(self.engine, self.scope(credential_id))

    def warm_catalog(self, credential_id):
        with Session(self.engine) as db:
            credential = db.get(AwsCredential, credential_id)
            scope = snapshots.scope_for(db.get(User, credential.user_id), credential, "catalog", "regions")
        async def catalog():
            return [{"name": traffic_tests.EAST}, {"name": traffic_tests.WEST}]
        asyncio.run(snapshots.read_snapshot(self.engine, scope, 86400, catalog, wait=True))

    def compatibility(self, credential_id):
        with Session(self.engine) as db:
            credential = db.get(AwsCredential, credential_id)
            return asyncio.run(instance_cache.instance_scopes(self.engine, db.get(User, credential.user_id),
                [credential], wait=True, force=True, region=traffic_tests.EAST))

    def refresh(self, credential_id):
        response = self.client.post("/api/v2/refresh", json={"kind": "instances",
            "credential_id": credential_id, "region": traffic_tests.EAST})
        self.assertEqual(response.status_code, 202, response.text)
        self.finish_refreshes()
        read = self.client.get("/api/v2/instances", params={"credential_id": credential_id,
            "region": traffic_tests.EAST, "wait": "true"})
        self.assertEqual(read.status_code, 200, read.text)
        return read.json()

    def test_traffic_cooldown_blocks_dual_key_force_and_both_compatibility_routes(self):
        self.reject_first("GetInstances")
        traffic = self.task(1, key="r3-traffic-before-compat")
        self.assertEqual(traffic["status"], "partial", traffic)
        failure = next(error for error in traffic["result"]["errors"] if error["region"] == traffic_tests.EAST)
        saved = self.store(1).active(traffic_tests.EAST, "lightsail", "GetInstances")
        self.aws.reject = None
        self.warm_catalog(2)
        result = self.refresh(2)  # Cold /v2/refresh must install the shared gate too.
        self.assertEqual(result["instances"], [])
        self.assertEqual(result["scopes"][0]["next_retry_at"], failure["next_retry_at"])
        legacy = self.client.get("/api/instances", params={"credential_id": 2})
        self.assertEqual(legacy.status_code, 200, legacy.text)  # The other region remains readable.
        self.assertTrue(legacy.json())
        with patch("app.services.read_cooldowns.time.time", return_value=saved["retry_at"] - 10):
            for _ in range(2):
                forced = self.refresh(2)
                self.assertEqual(forced["scopes"][0]["next_retry_at"], failure["next_retry_at"])
            direct = self.compatibility(2)
            self.assertEqual(direct[0]["next_retry_at"], failure["next_retry_at"])
        self.assertEqual(self.sent(2, region=traffic_tests.EAST, operation="GetInstances"), [])
        self.assertEqual(self.store(2).active(traffic_tests.EAST, "lightsail", "GetInstances")["retry_at"],
                         saved["retry_at"])

    def test_compatibility_throttle_is_shared_back_to_second_key_traffic(self):
        self.warm_catalog(1)
        self.reject_first("GetInstances")
        result = self.refresh(1)
        self.assertEqual(result["instances"], [])
        self.assertTrue(result["scopes"][0]["error"])
        deadline = result["scopes"][0]["next_retry_at"]
        self.aws.reject = None
        traffic = self.task(2, key="r3-compat-before-traffic")
        self.assertEqual(traffic["status"], "partial", traffic)
        error = next(value for value in traffic["result"]["errors"] if value["region"] == traffic_tests.EAST)
        self.assertEqual(error["next_retry_at"], deadline)
        self.assertEqual(error["code"], "ThrottlingException")
        self.assertEqual(self.sent(2, region=traffic_tests.EAST, operation="GetInstances"), [])

    def test_permission_denial_clears_own_stale_payload_without_blocking_second_key(self):
        self.warm_catalog(1)
        self.warm_catalog(2)
        warm = self.refresh(1)
        self.assertTrue(warm["instances"])
        with Session(self.engine) as db:
            db.execute(update(DataSnapshot).where(DataSnapshot.credential_id == 1,
                DataSnapshot.kind == "instances").values(expires_at=0))
            db.commit()
        self.reject_first("GetInstances", code="AccessDeniedException", status=403)
        denied = self.refresh(1)
        self.assertEqual(denied["instances"], [])
        self.assertEqual(denied["scopes"][0]["error_status"], "no_permission")
        self.aws.reject = None
        allowed = self.refresh(2)
        self.assertTrue(allowed["instances"])
        self.assertTrue(all(item["credential_id"] == 2 for item in allowed["instances"]))
        self.assertEqual(len(self.sent(2, region=traffic_tests.EAST, operation="GetInstances")), 1)

    def test_compatibility_next_page_observes_new_account_cooldown(self):
        self.warm_catalog(2)
        original, saved = self.aws.call, []
        def page(operation, parameters):
            if operation == "GetInstances":
                saved.append(self.store(1).record(self.throttle(operation), traffic_tests.EAST,
                                                 "lightsail", operation))
                return {"instances": [{"name": "partial"}], "nextPageToken": "second"}
            return original(operation, parameters)
        with patch.object(self.aws, "call", side_effect=page):
            result = self.compatibility(2)
        self.assertEqual(len(self.sent(2, region=traffic_tests.EAST, operation="GetInstances")), 1)
        self.assertIsNone(result[0]["data"])
        self.assertEqual(result[0]["next_retry_at"], saved[0]["next_retry_at"])

    def queued_compatibility(self, change):
        self.warm_catalog(2)
        sends, errors, results = [], [], []
        client = boto3.session.Session().client("lightsail", region_name=traffic_tests.EAST,
            aws_access_key_id=self.keys[2], aws_secret_access_key="fake-secret",
            config=Config(retries={"mode": "standard", "total_max_attempts": 2}))
        client._endpoint._send = lambda request: sends.append(request)
        self.addCleanup(client.close)
        client = aws_limits.limit_client(client, traffic_tests.EAST)
        throttle = ReadThrottle(lambda: 0)
        def read():
            try:
                results.extend(self.compatibility(2))
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=read)
        held = 0
        with patch.object(lightsail, "make_client", return_value=client), \
                patch.object(aws_limits, "read_throttle", throttle):
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
                    self.fail(f"Compatibility read never entered the SDK send queue: {errors}")
                changed = change()
            finally:
                for _ in range(held):
                    aws_limits._all.release()
                throttle.settings_changed()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sends, [])
        self.assertEqual(len(results), 1)
        return results[0], changed

    def test_compatibility_actual_send_checks_cooldown_committed_after_queueing(self):
        result, saved = self.queued_compatibility(lambda: self.store(1).record(self.throttle("GetInstances"),
            traffic_tests.EAST, "lightsail", "GetInstances"))
        self.assertIsNone(result["data"])
        self.assertEqual(result["next_retry_at"], saved["next_retry_at"])

    def test_compatibility_actual_send_rejects_rotated_credential_without_cooldown(self):
        def rotate():
            with Session(self.engine) as db:
                db.get(AwsCredential, 2).secret_access_key_enc = encrypt_secret("rotated-r3")
                db.commit()
        result, _changed = self.queued_compatibility(rotate)
        self.assertEqual(result["status"], "invalidated")
        self.assertIsNone(result["data"])
        self.assertIsNone(self.store(2).active(traffic_tests.EAST, "lightsail", "GetInstances"))
