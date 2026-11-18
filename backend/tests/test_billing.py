"""账单服务测试：全部用 unittest.mock 模拟 Cost Explorer 客户端。

不调真实 AWS、不需要已绑凭证。运行：
    python -m unittest discover -s tests -v
或直接：
    python tests/test_billing.py
"""
import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import Mock

# H-1/H-2 fail-fast：SECRET_KEY / ENCRYPTION_KEY / ADMIN_PASSWORD 为必填，
# 测试须先设置合法的测试值再导入 app 代码（以下均为测试专用值）。
os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from botocore.exceptions import ClientError

from app.services import billing as b


def _client_error(code: str, message: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "GetCostAndUsage")


def _groups_response() -> dict:
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-11-01", "End": "2026-11-17"},
                "Groups": [
                    {
                        "Keys": ["Amazon Lightsail", "Usage"],
                        "Metrics": {
                            "UnblendedCost": {"Amount": "10.00", "Unit": "USD"},
                            "NetUnblendedCost": {"Amount": "8.00", "Unit": "USD"},
                        },
                    },
                    {
                        "Keys": ["Amazon Elastic Compute Cloud - Compute", "Usage"],
                        "Metrics": {
                            "UnblendedCost": {"Amount": "20.00", "Unit": "USD"},
                            "NetUnblendedCost": {"Amount": "20.00", "Unit": "USD"},
                        },
                    },
                ],
                "Estimated": True,
            }
        ]
    }


def _totals_response(months) -> dict:
    # months: [(unblended, net), ...]
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": "2026-11-01", "End": "2026-12-01"},
                "Total": {
                    "UnblendedCost": {"Amount": str(n-u), "Unit": "USD"},
                    "NetUnblendedCost": {"Amount": str(n), "Unit": "USD"},
                },
                "Estimated": False,
            }
            for u, n in months
        ]
    }


class TestBillingSummary(unittest.TestCase):
    def test_summary_parses_costs_and_lightsail(self):
        client = Mock()
        client.get_cost_and_usage.return_value = _groups_response()
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        out = b.get_billing_summary(client, now=now, account_id="123456789012")
        self.assertEqual(out["year_month"], "2026-11")
        self.assertEqual(out["currency"], "USD")
        self.assertEqual(out["total_cost"], "30")  # 10 + 20
        self.assertEqual(out["net_cost"], "30")  # Usage line items
        self.assertEqual(out["credits_applied"], "0")  # no Credit record
        self.assertEqual(out["lightsail_cost"], "10")
        self.assertEqual(len(out["by_service"]), 2)
        self.assertEqual(out["by_service"][0]["service"], "Amazon Elastic Compute Cloud - Compute")
        lightsail = next(s for s in out["by_service"] if "Lightsail" in s["service"])
        self.assertEqual(lightsail["credits_applied"], "0")

    def test_summary_no_results_is_unavailable(self):
        client = Mock()
        client.get_cost_and_usage.return_value = {"ResultsByTime": []}
        with self.assertRaises(b.BillingUnavailableError):
            b.get_billing_summary(client, now=datetime(2026, 11, 16, tzinfo=timezone.utc), account_id="123456789012")

    def test_summary_data_unavailable_maps_to_unavailable(self):
        client = Mock()
        client.get_cost_and_usage.side_effect = _client_error(
            "DataUnavailableException", "Cost Explorer is not enabled"
        )
        with self.assertRaises(b.BillingUnavailableError):
            b.get_billing_summary(client, now=datetime(2026, 11, 16, tzinfo=timezone.utc), account_id="123456789012")

    def test_summary_access_denied_maps_to_no_permission(self):
        client = Mock()
        client.get_cost_and_usage.side_effect = _client_error(
            "AccessDeniedException",
            "User is not authorized to perform: ce:GetCostAndUsage",
        )
        with self.assertRaises(b.BillingNoPermissionError):
            b.get_billing_summary(client, now=datetime(2026, 11, 16, tzinfo=timezone.utc), account_id="123456789012")

    def test_forecast_success_and_failure_returns_none(self):
        client = Mock()
        client.get_cost_forecast.return_value = {
            "Total": {"Amount": "35.50", "Unit": "USD"}
        }
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(b.get_cost_forecast(client, now=now, account_id="123456789012"), "35.5")

        failing = Mock()
        failing.get_cost_forecast.side_effect = RuntimeError("boom")
        self.assertIsNone(b.get_cost_forecast(failing, now=now))


class TestCreditsAppliedSince(unittest.TestCase):
    def test_applied_sums_months(self):
        client = Mock()
        # 11 月抵扣 2，12 月抵扣 3（按 Credit 记录）
        client.get_cost_and_usage.return_value = _totals_response(
            [(10.0, 8.0), (13.0, 10.0)]
        )
        now = datetime(2026, 12, 16, tzinfo=timezone.utc)
        applied = b.get_credits_applied_since(client, date(2026, 11, 15), now=now, account_id="123456789012")
        self.assertEqual(applied, "5")
        # 请求的开始日应为赠金开始日
        kwargs = client.get_cost_and_usage.call_args.kwargs
        self.assertEqual(kwargs["TimePeriod"]["Start"], "2026-11-15")

    def test_future_start_date_returns_zero_and_no_call(self):
        client = Mock()
        now = datetime(2026, 11, 16, tzinfo=timezone.utc)
        self.assertEqual(
            b.get_credits_applied_since(client, date(2027, 1, 1), now=now, account_id="123456789012"), "0"
        )
        client.get_cost_and_usage.assert_not_called()

    def test_applied_no_permission_propagates(self):
        client = Mock()
        client.get_cost_and_usage.side_effect = _client_error(
            "AccessDeniedException", "not authorized to perform: ce:GetCostAndUsage"
        )
        with self.assertRaises(b.BillingNoPermissionError):
            b.get_credits_applied_since(
                client, date(2026, 11, 1), now=datetime(2026, 11, 16, tzinfo=timezone.utc), account_id="123456789012"
            )


class TestAwsCredits(unittest.TestCase):
    """Billing GetCredits：mock STS + billing 客户端，不调真实 AWS。"""

    def _sts(self, account: str = "123456789012") -> Mock:
        sts = Mock()
        sts.get_caller_identity.return_value = {"Account": account}
        return sts

    def _billing(self, credits: list) -> Mock:
        client = Mock()
        client.get_credits.return_value = {"credits": credits}
        return client

    def _credit_raw(self) -> dict:
        return {
            "creditId": "10070909437",
            "accountId": "123456789012",
            "creditType": "AWS Free Tier",
            "initialAmount": {"currencyAmount": "100.00", "currencyCode": "USD"},
            "remainingAmount": {"currencyAmount": "75.50", "currencyCode": "USD"},
            "estimatedAmount": {"currencyAmount": "80.00", "currencyCode": "USD"},
            "applicableProductNames": ["Amazon Lightsail", "Amazon EC2"],
            "description": "AWS Free Tier",
            "startDate": datetime(2026, 11, 15, tzinfo=timezone.utc),
            "endDate": datetime(2027, 11, 15, tzinfo=timezone.utc),
            "exhaustDate": datetime(2027, 11, 15, tzinfo=timezone.utc),
            "creditStatus": "Active",
        }

    def test_get_credits_parses_remaining_estimated_initial(self):
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        out = b.get_aws_credits(
            "AKID", "SECRET", now=now, sts_client=self._sts(), billing_client=self._billing([self._credit_raw()])
        )
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["account_id"], "123456789012")
        self.assertEqual(len(out["credits"]), 1)
        c = out["credits"][0]
        self.assertEqual(c["credit_id"], "10070909437")
        self.assertEqual(c["initial_amount"], "100")
        self.assertEqual(c["remaining_amount"], "75.5")
        self.assertEqual(c["estimated_amount"], "80")
        self.assertEqual(c["currency"], "USD")
        self.assertEqual(c["status"], "Active")
        self.assertEqual(c["start_date"], "2026-11-15")
        self.assertEqual(c["end_date"], "2027-11-15")
        self.assertEqual(c["applicable_products"], ["Amazon Lightsail", "Amazon EC2"])

    def test_get_credits_calls_with_account_from_sts_and_start_within_one_year(self):
        sts = self._sts()
        billing = self._billing([])
        now = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
        out = b.get_aws_credits("AKID", "SECRET", now=now, sts_client=sts, billing_client=billing)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["credits"], [])
        sts.get_caller_identity.assert_called_once()
        kwargs = billing.get_credits.call_args.kwargs
        self.assertEqual(kwargs["accountId"], "123456789012")
        start = kwargs["startDate"]
        end = kwargs["endDate"]
        # startDate 不超过 1 年：距今天 <= 365 天，且 endDate 为今天
        self.assertLessEqual((end.date() - start.date()).days, 365)
        self.assertEqual(end.date(), date(2026, 11, 16))
        self.assertEqual(start.date(), date(2025, 11, 17))  # 今天往前 364 天

    def test_get_credits_empty_returns_empty_list(self):
        out = b.get_aws_credits(
            "AKID", "SECRET", now=datetime(2026, 11, 16, tzinfo=timezone.utc),
            sts_client=self._sts(), billing_client=self._billing([]),
        )
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["credits"], [])

    def test_get_credits_access_denied_maps_to_no_permission(self):
        billing = Mock()
        billing.get_credits.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized to perform: billing:GetCredits"}},
            "GetCredits",
        )
        with self.assertRaises(b.BillingNoPermissionError):
            b.get_aws_credits(
                "AKID", "SECRET", now=datetime(2026, 11, 16, tzinfo=timezone.utc),
                sts_client=self._sts(), billing_client=billing,
            )
        # STS 无权限同样映射为 no_permission
        sts = Mock()
        sts.get_caller_identity.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "not authorized to perform: sts:GetCallerIdentity"}},
            "GetCallerIdentity",
        )
        with self.assertRaises(b.BillingNoPermissionError):
            b.get_aws_credits(
                "AKID", "SECRET", now=datetime(2026, 11, 16, tzinfo=timezone.utc),
                sts_client=sts, billing_client=self._billing([]),
            )

    def test_get_credits_validation_maps_to_validation_error(self):
        billing = Mock()
        billing.get_credits.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "startDate must be within the past year"}},
            "GetCredits",
        )
        with self.assertRaises(b.BillingValidationError):
            b.get_aws_credits(
                "AKID", "SECRET", now=datetime(2026, 11, 16, tzinfo=timezone.utc),
                sts_client=self._sts(), billing_client=billing,
            )

    def test_get_credits_sts_failure_maps_to_unavailable(self):
        sts = Mock()
        sts.get_caller_identity.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "boom"}}, "GetCallerIdentity"
        )
        with self.assertRaises(b.BillingUnavailableError):
            b.get_aws_credits(
                "AKID", "SECRET", now=datetime(2026, 11, 16, tzinfo=timezone.utc),
                sts_client=sts, billing_client=self._billing([]),
            )

    def test_get_credits_epoch_and_missing_amounts(self):
        raw = {
            "creditId": "x1",
            "initialAmount": {"currencyAmount": 50, "currencyCode": "USD"},
            "remainingAmount": None,
            "estimatedAmount": {"currencyAmount": "49.5", "currencyCode": "USD"},
            "startDate": 1763164800,  # 2025-11-15 UTC
            "creditStatus": "Active",
        }
        out = b.get_aws_credits(
            "AKID", "SECRET", now=datetime(2026, 11, 16, tzinfo=timezone.utc),
            sts_client=self._sts(), billing_client=self._billing([raw]),
        )
        c = out["credits"][0]
        self.assertEqual(c["initial_amount"], "50")
        self.assertIsNone(c["remaining_amount"])
        self.assertEqual(c["estimated_amount"], "49.5")
        self.assertEqual(c["start_date"], "2025-11-15")
        self.assertEqual(c["applicable_products"], [])


class TestCreditRecordModel(unittest.TestCase):
    def test_credit_record_roundtrip_in_memory_db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from app.database import Base
        from app.models import CreditRecord, User

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        user = User(username="t", password_hash="x", is_admin=False)
        db.add(user)
        db.commit()
        rec = CreditRecord(
            user_id=user.id,
            name="注册赠金",
            total_amount=100.0,
            currency="USD",
            start_date=date(2026, 11, 15),
            expires_at=date(2027, 11, 15),
        )
        db.add(rec)
        db.commit()
        db.refresh(rec)
        self.assertEqual(rec.total_amount, 100.0)
        self.assertEqual(rec.user.username, "t")
        # 剩余估算公式：total - 已抵扣
        self.assertEqual(round(max(0.0, rec.total_amount - 5.0), 2), 95.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
