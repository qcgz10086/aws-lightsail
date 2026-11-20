"""独立预测状态、旧账单失败及流量部分结果的离线回归。"""
import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import unittest

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session
from app.models import AwsCredential, User
from app.services import billing, billing_cache
import test_t04_cache as cache_tests
import test_t06_workflows as workflow_tests
from task_fakes import seed_discovered_regions


HISTORY_MESSAGE = "You do not have enough historical data to generate a forecast."


class BillingStatusTests(unittest.TestCase):
    setUp = cache_tests.SnapshotTests.setUp

    def test_forecast_history_failure_has_independent_status(self):
        client = Mock()
        client.get_cost_forecast.side_effect = ClientError(
            {"Error": {"Code": "DataUnavailableException", "Message": HISTORY_MESSAGE}}, "GetCostForecast")
        with self.assertRaises(billing.BillingUnavailableError) as caught:
            billing.get_forecast_result(client, account_id="123456789012")
        self.assertEqual(getattr(caught.exception, "status", None), "insufficient_history")
        self.assertNotIn("启用 Cost Explorer", str(caught.exception))

    def test_history_failure_preserves_actual_costs_and_credits(self):
        client = Mock()
        client.get_cost_forecast.side_effect = ClientError(
            {"Error": {"Code": "DataUnavailableException", "Message": HISTORY_MESSAGE}}, "GetCostForecast")
        costs = {"currencies": [{"currency": "USD", "total_cost": "12.5", "net_cost": "12.5",
                 "credits_applied": "0", "refunds": "0", "discounts": "0", "lightsail_cost": "12.5"}]}
        credits = {"status": "ok", "credits": [{"credit_id": "credit-1", "currency": "USD",
                    "remaining_amount": "20", "estimated_amount": "18"}]}
        with Session(self.engine) as db, \
                patch.object(billing, "get_sts_identity", return_value="123456789012"), \
                patch.object(billing, "make_ce_client", return_value=client), \
                patch.object(billing, "get_billing_summary", return_value=costs), \
                patch.object(billing, "get_aws_credits", return_value=credits):
            result = asyncio.run(billing_cache.cached_account(self.engine, db.get(User, self.user_id),
                                 db.get(AwsCredential, self.scope["credential_id"]), wait=True))
        self.assertEqual(result["forecast"]["status"], "insufficient_history")
        self.assertEqual(result["costs"]["status"], "ok")
        self.assertEqual(result["costs"]["currencies"][0]["net_cost"], "12.5")
        self.assertEqual(result["credits"]["credits"][0]["remaining_amount"], "20")
        from app.services.billing_summary import aggregate
        total = aggregate([result])[0]
        self.assertEqual(total["net_cost"], "12.5")
        self.assertEqual(total["official_remaining"], "20")
        self.assertIsNone(total["forecast_remaining"])

    def test_failed_refresh_with_payload_is_explicit_and_keeps_old_values(self):
        result = billing_cache._section({"data": {"status": "ok", "credits": [{"remaining_amount": "20"}]},
            "status": "stale", "stale": True, "refreshing": False,
            "error": "请求超时", "error_status": "unavailable", "last_success_at": "2026-11-20T07:00:00Z"})
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["error"], "请求超时")
        self.assertEqual(result["credits"][0]["remaining_amount"], "20")
        self.assertTrue(result["cache"]["stale"])

    def test_final_response_rebuilds_payload_status_and_aggregate_together(self):
        from app.services import billing_summary, snapshots
        costs = {"currencies": [{"currency": "USD", "total_cost": "12.5", "net_cost": "12.5",
                 "credits_applied": "0", "refunds": "0", "discounts": "0", "lightsail_cost": "12.5"}]}
        original = billing_summary.revalidate_snapshot_views
        with Session(self.engine) as db, \
                patch.object(billing, "get_sts_identity", return_value="123456789012"), \
                patch.object(billing, "make_ce_client", return_value=Mock()), \
                patch.object(billing, "get_billing_summary", return_value=costs), \
                patch.object(billing, "get_forecast_result", return_value={"amount": "4.2", "currency": "USD"}), \
                patch.object(billing, "get_aws_credits", return_value={"credits": []}):
            user, credential = db.get(User, self.user_id), db.get(AwsCredential, self.scope["credential_id"])
            asyncio.run(billing_cache.cached_account(self.engine, user, credential, wait=True))
            scope = snapshots.scope_for(user, credential, "billing-costs", billing._today_utc().isoformat())
            def final_read(engine, scopes):
                token = snapshots._read_or_claim(engine, scope, True)[1]
                snapshots._publish(engine, scope, token, 1800, error="temporary timeout")
                return original(engine, scopes)
            with patch.object(billing_summary, "revalidate_snapshot_views", side_effect=final_read):
                result = asyncio.run(billing_summary.summary(db, user, cached=True, wait=True))
        self.assertEqual(result["accounts"][0]["costs"]["status"], "unavailable")
        self.assertEqual(result["accounts"][0]["costs"]["currencies"][0]["net_cost"], "12.5")
        self.assertTrue(result["accounts"][0]["costs"]["cache"]["stale"])
        self.assertFalse(result["currencies"][0]["costs_complete"])
        self.assertNotEqual(result["status"], "ok")

    def test_cold_billing_shares_one_sts_check_with_official_credits(self):
        sts = Mock()
        sts.get_caller_identity.return_value = {"Account": "123456789012"}
        credits_client = Mock()
        credits_client.get_credits.return_value = {"credits": []}
        with Session(self.engine) as db, \
                patch.object(billing, "make_sts_client", return_value=sts), \
                patch.object(billing, "make_billing_client", return_value=credits_client), \
                patch.object(billing, "make_ce_client", return_value=Mock()), \
                patch.object(billing, "get_billing_summary", return_value={"currencies": []}), \
                patch.object(billing, "get_forecast_result", return_value={"amount": "4.2", "currency": "USD"}):
            result = asyncio.run(billing_cache.cached_account(self.engine, db.get(User, self.user_id),
                db.get(AwsCredential, self.scope["credential_id"]), wait=True))
        self.assertEqual(result["credits"]["status"], "ok")
        self.assertEqual(sts.get_caller_identity.call_count, 1)


class TrafficStatusTests(unittest.TestCase):
    setUp = workflow_tests.WorkflowTests.setUp
    tearDown = workflow_tests.WorkflowTests.tearDown
    submit = workflow_tests.WorkflowTests.submit

    def test_partial_traffic_retains_success_count_and_safe_failure_details(self):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as db:
            seed_discovered_regions(db, self.user_id, "123456789012", "ap-south-1", "us-east-1")
        original = self.aws.call
        attempts = []
        def call(operation, parameters):
            if operation == "GetInstances":
                attempts.append(1)
            if operation == "GetInstances" and len(attempts) == 1:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message":
                    "access denied for AKIAIOSFODNN7EXAMPLE https://admin:private-password@proxy.local"}}, operation)
            return original(operation, parameters)
        with patch.object(self.aws, "call", side_effect=call):
            result = self.submit("traffic_sync", {"window_start": now.replace(day=1, hour=0, minute=0,
                second=0, microsecond=0).isoformat(), "window_end": now.isoformat(), "year_month": now.strftime("%Y-%m")})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["result"]["count"], 1)
        self.assertEqual(result["result"].get("successful_regions"), 1)
        self.assertEqual(result["result"].get("failed_regions"), 1)
        error = result["result"]["errors"][0]
        self.assertEqual(error["region"], "ap-south-1")
        self.assertEqual(error.get("code"), "AccessDeniedException")
        self.assertTrue(error.get("reason"))
        self.assertIn("权限", error.get("retry_condition", ""))
        self.assertNotIn("private-password", json.dumps(result))
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", json.dumps(result))

    def test_metric_failure_identifies_exact_step_and_retry_condition(self):
        now = datetime.now(timezone.utc)
        with Session(self.engine) as db:
            seed_discovered_regions(db, self.user_id, "123456789012", "us-east-1")
        original = self.aws.call
        def call(operation, parameters):
            if operation == "GetInstanceMetricData" and parameters["metricName"] == "NetworkOut":
                raise TimeoutError("private-password network unavailable")
            return original(operation, parameters)
        with patch.object(self.aws, "call", side_effect=call):
            result = self.submit("traffic_sync", {"window_start": now.replace(day=1, hour=0, minute=0,
                second=0, microsecond=0).isoformat(), "window_end": now.isoformat(), "year_month": now.strftime("%Y-%m")})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["result"]["count"], 0)
        error = result["result"]["errors"][0]
        self.assertEqual(error["step"], "NetworkOut")
        self.assertEqual(error.get("code"), "TimeoutError")
        self.assertTrue(error.get("retry_condition"))
        self.assertNotIn("private-password", json.dumps(result))
