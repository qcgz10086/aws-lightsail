"""T03 financial regressions use fixed decimal strings and mock AWS only."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services import billing as b

NOW = datetime(2026, 11, 19, 12, tzinfo=timezone.utc)
ACCOUNT = "123456789012"


def group(service, record_type, amount, currency="USD", net="0"):
    return {"Keys": [service, record_type], "Metrics": {
        "UnblendedCost": {"Amount": amount, "Unit": currency},
        "NetUnblendedCost": {"Amount": net, "Unit": currency}}}


def page(*groups, token=None):
    result = {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-01", "End": "2026-11-20"},
                                "Groups": list(groups), "Estimated": True}]}
    if token:
        result["NextPageToken"] = token
    return result


class BillingAccuracyTests(unittest.TestCase):
    def test_credit_reversal_refund_and_discount_are_separate_decimal_records(self):
        client = Mock()
        client.get_cost_and_usage.return_value = page(
            group("Amazon Lightsail", "Usage", "100.123456789", net="1"),
            group("Amazon Lightsail", "Credit", "-20.10"),
            group("Amazon Lightsail", "Credit", "2.10"),
            group("Amazon Lightsail", "Refund", "-3.2"),
            group("Amazon Lightsail", "Discount", "-4.3"))
        result = b.get_billing_summary(client, NOW, account_id=ACCOUNT)
        usd = result["currencies"][0]
        self.assertEqual(usd["total_cost"], "100.123456789")
        self.assertEqual(usd["credits_applied"], "18")
        self.assertEqual(usd["refunds"], "3.2")
        self.assertEqual(usd["discounts"], "4.3")
        self.assertEqual(usd["net_cost"], "74.623456789")
        args = client.get_cost_and_usage.call_args.kwargs
        self.assertEqual(args["Filter"], {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [ACCOUNT]}})
        self.assertIn({"Type": "DIMENSION", "Key": "RECORD_TYPE"}, args["GroupBy"])

    def test_pages_and_periods_are_all_counted_without_combining_currencies(self):
        client = Mock()
        client.get_cost_and_usage.side_effect = [
            page(group("A", "Usage", "0.1"), token="next"),
            {"ResultsByTime": [page(group("A", "Usage", "0.2"))["ResultsByTime"][0],
                               page(group("B", "Usage", "7", "EUR"))["ResultsByTime"][0]]}]
        result = b.get_billing_summary(client, NOW, account_id=ACCOUNT)
        totals = {row["currency"]: row for row in result["currencies"]}
        self.assertEqual(totals["USD"]["net_cost"], "0.3")
        self.assertEqual(totals["EUR"]["net_cost"], "7")
        self.assertIsNone(result["total_cost"])
        self.assertEqual(client.get_cost_and_usage.call_args.kwargs["NextPageToken"], "next")

    def test_missing_invalid_or_nonfinite_money_never_becomes_zero(self):
        for value in (None, "", "invalid", "NaN", "Infinity"):
            with self.subTest(value=value):
                client = Mock()
                client.get_cost_and_usage.return_value = page(group("A", "Usage", value))
                with self.assertRaises(b.BillingUnavailableError):
                    b.get_billing_summary(client, NOW, account_id=ACCOUNT)

    def test_unknown_identity_makes_no_organization_wide_request(self):
        client = Mock()
        with self.assertRaises(b.BillingValidationError):
            b.get_billing_summary(client, NOW)
        client.get_cost_and_usage.assert_not_called()

    def test_repeated_page_token_is_rejected_instead_of_counted_twice(self):
        client = Mock()
        client.get_cost_and_usage.return_value = page(group("A", "Usage", "1"), token="repeat")
        with self.assertRaises(b.BillingUnavailableError):
            b.get_billing_summary(client, NOW, account_id=ACCOUNT)
        self.assertLessEqual(client.get_cost_and_usage.call_count, 2)

    def test_getcredits_explicit_self_scope_and_forecast_scope(self):
        sts, credits, ce = Mock(), Mock(), Mock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}
        credits.get_credits.return_value = {"credits": []}
        b.get_aws_credits("fixed-test-key", "fixed-test-secret", NOW, sts, credits)
        self.assertIs(credits.get_credits.call_args.kwargs["payerAccountFlag"], False)
        ce.get_cost_forecast.return_value = {"Total": {"Amount": "9.123456789", "Unit": "EUR"},
            "ForecastResultsByTime": [{"TimePeriod": {"Start": date(2026, 11, day).isoformat(),
                "End": (date(2026, 11, day) + timedelta(days=1)).isoformat()},
                "MeanValue": "9" if day == 19 else "0.123456789" if day == 20 else "0"}
                for day in range(19, 31)]}
        forecast = b.get_forecast_result(ce, NOW, account_id=ACCOUNT)
        self.assertEqual(forecast, {"amount": "0.123456789", "currency": "EUR"})
        self.assertEqual(ce.get_cost_forecast.call_args.kwargs["Filter"]["Dimensions"]["Values"], [ACCOUNT])
        self.assertEqual(ce.get_cost_forecast.call_args.kwargs["TimePeriod"]["Start"], "2026-11-19")
        self.assertEqual(ce.get_cost_forecast.call_args.kwargs["Granularity"], "DAILY")

    def test_forecast_rejects_missing_daily_coverage_and_excludes_today_on_last_day(self):
        client = Mock()
        client.get_cost_forecast.return_value = {"Total": {"Amount": "5", "Unit": "USD"}}
        with self.assertRaises(b.BillingUnavailableError):
            b.get_forecast_result(client, NOW, ACCOUNT)
        client.get_cost_forecast.return_value["ForecastResultsByTime"] = [{"TimePeriod": {
            "Start": "2026-11-30", "End": "2026-12-01"}, "MeanValue": "5"}]
        self.assertEqual(b.get_forecast_result(client, datetime(2026, 11, 30, tzinfo=timezone.utc), ACCOUNT),
                         {"amount": "0", "currency": "USD"})

    def test_official_duplicates_do_not_inflate_balance_and_other_account_is_rejected(self):
        sts, credits = Mock(), Mock()
        sts.get_caller_identity.return_value = {"Account": ACCOUNT}
        raw = {"creditId": "same", "accountId": ACCOUNT,
               "remainingAmount": {"currencyAmount": "10.123456789", "currencyCode": "USD"}}
        credits.get_credits.return_value = {"credits": [raw, dict(raw)]}
        result = b.get_aws_credits("fake", "fake", NOW, sts, credits)
        self.assertEqual(len(result["credits"]), 1)
        self.assertEqual(result["credits"][0]["remaining_amount"], "10.123456789")
        credits.get_credits.return_value = {"credits": [{**raw, "accountId": "210987654321"}]}
        with self.assertRaises(b.BillingValidationError):
            b.get_aws_credits("fake", "fake", NOW, sts, credits)

    def test_aggregate_marks_missing_balance_and_partial_results_without_fake_zero(self):
        from app.services.billing_summary import aggregate
        success = {"costs": {"status": "ok", "currencies": [{"currency": "USD", "total_cost": "1",
            "net_cost": "1", "credits_applied": "0", "refunds": "0", "discounts": "0", "lightsail_cost": "1"}]},
            "forecast": {"status": "unavailable"},
            "credits": {"status": "ok", "credits": [{"currency": "USD", "remaining_amount": None, "estimated_amount": "2"}]}}
        failed = {key: {"status": "unavailable"} for key in ("costs", "forecast", "credits")}
        rows = aggregate([success, failed])
        self.assertEqual(rows[0]["net_cost"], "1")
        self.assertFalse(rows[0]["costs_complete"])
        self.assertIsNone(rows[0]["forecast_remaining"])
        self.assertIsNone(rows[0]["official_remaining"])
        self.assertFalse(rows[0]["credits_complete"])
        self.assertEqual(rows[0]["official_estimated"], "2")


class BillingRouteTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        import tempfile
        from app.database import Base, get_db
        from app.deps import get_current_user
        from app.main import app
        from app.models import AwsCredential, User
        from app.security import encrypt_secret
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name).as_posix()}/app.db", connect_args={"check_same_thread": False})
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.user = User(username="t03-owner", password_hash="opaque")
        other = User(username="t03-other", password_hash="opaque")
        self.db.add_all([self.user, other]); self.db.flush()
        def cred(user, account, default=False):
            result = AwsCredential(user_id=user.id, account_id=account, last_confirmed_account_id=account,
                access_key_id_enc=encrypt_secret("AKIAIOSFODNN7EXAMPLE"),
                secret_access_key_enc=encrypt_secret("fixed-fake-secret"), is_default=default)
            self.db.add(result); self.db.flush(); return result
        self.first = cred(self.user, ACCOUNT, True)
        self.duplicate = cred(self.user, ACCOUNT)
        self.second = cred(self.user, "210987654321")
        self.foreign = cred(other, ACCOUNT)
        self.db.commit()
        self.old_overrides = dict(app.dependency_overrides)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(lambda: (app.dependency_overrides.clear(), app.dependency_overrides.update(self.old_overrides)))
        self.addCleanup(self.engine.dispose)
        self.addCleanup(self.db.close)

    def record(self, account=None, credit_id=None, credential=None):
        from app.models import CreditRecord
        from datetime import date
        record = CreditRecord(user_id=self.user.id, account_id=account, credential_id=credential,
            name="manual", credit_id=credit_id, total_amount=100,
            total_amount_decimal="100.123456789", currency="USD", start_date=date(2026, 11, 1))
        self.db.add(record); self.db.commit(); return record

    def mocks(self):
        from app.models import DataSnapshot
        self.db.query(DataSnapshot).delete()
        self.db.commit()
        return patch.multiple(b, get_sts_identity=Mock(return_value=ACCOUNT),
            make_ce_client=Mock(return_value=Mock()),
            get_billing_summary=Mock(return_value={"year_month": "2026-11", "currencies": [{
                "currency": "USD", "total_cost": "10", "net_cost": "8", "credits_applied": "2",
                "refunds": "0", "discounts": "0", "lightsail_cost": "10"}], "by_service": []}),
            get_forecast_result=Mock(return_value={"amount": "5", "currency": "USD"}),
            get_aws_credits=Mock(return_value={"credits": [{"credit_id": "official-1", "currency": "USD",
                "remaining_amount": "70", "estimated_amount": "68", "initial_amount": "100"}]}))

    def test_v2_deduplicates_accounts_and_does_not_allocate_manual_consumption(self):
        self.record(ACCOUNT, "official-1", self.first.id)
        unmatched = self.record(ACCOUNT, "manual-only", self.first.id)
        pending = self.record(None, "old-history", self.first.id)
        with self.mocks(), patch.object(b, "get_sts_identity", side_effect=[ACCOUNT, "210987654321"]) as sts:
            response = self.client.get("/api/v2/billing/summary?wait=true")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(len(data["accounts"]), 2)
        self.assertEqual(sts.call_count, 2)
        self.assertEqual(data["currencies"][0]["official_remaining"], "140")
        self.assertEqual([row["id"] for row in data["pending_assignment"]], [pending.id])
        rows = {row["id"]: row for row in data["manual_credits"]}
        self.assertIsNone(rows[unmatched.id]["remaining_estimate"])
        self.assertTrue(all(not row["balance_included"] for row in rows.values()))

    def test_account_filter_cross_user_and_partial_states(self):
        self.record("210987654321", "not-selected", self.second.id)
        self.record(ACCOUNT, "selected", self.first.id)
        with self.mocks(), patch.object(b, "get_forecast_result", side_effect=b.BillingNoPermissionError("denied")):
            response = self.client.get(f"/api/v2/billing/summary?wait=true&credential_id={self.first.id}")
        self.assertEqual(response.status_code, 200, response.text)
        account = response.json()["accounts"][0]
        self.assertEqual(account["costs"]["status"], "ok")
        self.assertEqual(account["forecast"]["status"], "no_permission")
        self.assertEqual(account["credits"]["status"], "ok")
        self.assertEqual([row["credit_id"] for row in response.json()["manual_credits"]], ["selected"])
        with self.mocks() as mocks:
            forbidden = self.client.get(f"/api/v2/billing/summary?wait=true&credential_id={self.foreign.id}")
        self.assertEqual(forbidden.status_code, 400, forbidden.text)

    def test_changed_identity_never_queries_old_account_costs(self):
        with self.mocks(), patch.object(b, "get_sts_identity", return_value="999999999999"), \
             patch.object(b, "get_billing_summary") as costs, patch.object(b, "get_aws_credits") as credits:
            response = self.client.get(f"/api/v2/billing/summary?wait=true&credential_id={self.first.id}")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["currencies"], [])
        costs.assert_not_called(); credits.assert_not_called()

    def test_precise_manual_create_and_explicit_assignment_are_user_scoped(self):
        response = self.client.post("/api/v2/billing/credits", json={"name": "precise", "total_amount": "0.123456789123456789",
            "currency": "EUR", "start_date": "2026-11-01", "credential_id": self.first.id})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["total_amount"], "0.123456789123456789")
        self.assertEqual(response.json()["amount_precision"], "exact_decimal")
        self.assertEqual(response.json()["account_id"], ACCOUNT)
        pending = self.record()
        forbidden = self.client.patch(f"/api/v2/billing/credits/{pending.id}/assign", json={"credential_id": self.foreign.id})
        self.assertEqual(forbidden.status_code, 400, forbidden.text)
        assigned = self.client.patch(f"/api/v2/billing/credits/{pending.id}/assign", json={"credential_id": self.first.id})
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertEqual(assigned.json()["account_id"], ACCOUNT)

    def test_unknown_manual_credentials_and_reassignment_cannot_move_existing_account(self):
        self.first.account_id = None
        self.db.commit()
        response = self.client.post("/api/v2/billing/credits", json={"name": "unknown", "total_amount": "1",
            "start_date": "2026-11-01", "credential_id": self.first.id})
        self.assertEqual(response.status_code, 409, response.text)
        record = self.record(ACCOUNT, "bound", self.duplicate.id)
        response = self.client.patch(f"/api/v2/billing/credits/{record.id}/assign", json={"credential_id": self.second.id})
        self.assertEqual(response.status_code, 409, response.text)
        self.db.refresh(record)
        self.assertEqual(record.account_id, ACCOUNT)

    def test_legacy_precision_marker_is_visible_in_api_without_remaining_estimate(self):
        record = self.record()
        record.amount_precision = "legacy_float"
        self.db.commit()
        with self.mocks():
            response = self.client.get(f"/api/v2/billing/summary?wait=true&credential_id={self.first.id}")
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()["pending_assignment"][0]
        self.assertEqual(row["amount_precision"], "legacy_float")
        self.assertIn("浮点近似", row["precision_note"])
        self.assertIsNone(row["remaining_estimate"])
        self.assertIsNone(row["account_id"])

    def test_legacy_summary_preserves_shape_and_does_not_leak_foreign_manual_rows(self):
        self.record(ACCOUNT, "owned", self.first.id)
        self.record("210987654321", "other-account", self.second.id)
        with self.mocks():
            response = self.client.get(f"/api/billing/summary?credential_id={self.first.id}")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertIn("total_cost", data)
        self.assertIsInstance(data["total_cost"], float)
        self.assertEqual(data["forecast_cost"], 13.0)  # actual signed 8 + remaining-month forecast 5
        self.assertEqual([row["credit_id"] for row in data["credits"]], ["owned"])
        self.assertIsNone(data["credits"][0]["remaining_estimate"])

    def test_full_month_forecast_requires_successful_matching_currency_costs(self):
        for forecast in ({"amount": "5", "currency": "EUR"}, b.BillingUnavailableError("forecast failed")):
            with self.subTest(forecast=forecast):
                kwargs = {"side_effect": forecast} if isinstance(forecast, Exception) else {"return_value": forecast}
                with self.mocks(), patch.object(b, "get_forecast_result", **kwargs):
                    response = self.client.get(f"/api/billing/summary?credential_id={self.first.id}")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIsNone(response.json()["forecast_cost"])
        with self.mocks():
            response = self.client.get(f"/api/v2/billing/summary?wait=true&credential_id={self.first.id}")
        row = response.json()["currencies"][0]
        self.assertEqual(row["forecast_remaining"], "5")
        self.assertEqual(row["forecast_month_total"], "13")
        with self.mocks(), patch.object(b, "get_billing_summary", side_effect=b.BillingUnavailableError("cost failed")):
            response = self.client.get(f"/api/billing/summary?credential_id={self.first.id}")
        self.assertIsNone(response.json()["forecast_cost"])
        self.assertEqual(response.json()["status"], "unavailable")

    def test_forecast_decimal_addition_is_exact_before_legacy_float_adapter(self):
        costs = {"year_month": "2026-11", "currencies": [{"currency": "USD", "total_cost": "0.1",
            "net_cost": "0.1", "credits_applied": "0", "refunds": "0", "discounts": "0",
            "lightsail_cost": "0.1"}], "by_service": []}
        with self.mocks(), patch.object(b, "get_billing_summary", return_value=costs), \
             patch.object(b, "get_forecast_result", return_value={"currency": "USD", "amount": "0.2"}):
            exact = self.client.get(f"/api/v2/billing/summary?wait=true&credential_id={self.first.id}")
            legacy = self.client.get(f"/api/billing/summary?credential_id={self.first.id}")
        self.assertEqual(exact.json()["currencies"][0]["forecast_month_total"], "0.3")
        self.assertEqual(legacy.json()["forecast_cost"], 0.3)


class MigrationV3Tests(unittest.TestCase):
    def test_real_v2_source_upgrade_failure_and_restore(self):
        import io
        import json
        import sqlite3
        import subprocess
        import tarfile
        import tempfile
        from contextlib import closing
        from pathlib import Path
        from sqlalchemy import create_engine
        from app.database import Base
        from app import migrations
        from app.sqlite_backup import restore_database, file_sha256
        root = Path(__file__).resolve().parents[2]
        base = "f868161406664dd383c856fd3345771d7176b9c2"
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            archive = subprocess.check_output(["git", "archive", base, "backend"], cwd=root)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(folder, filter="data")
            path = folder / "v2.db"
            env = {**os.environ, "DATABASE_URL": f"sqlite:///{path.as_posix()}",
                   "PYTHONPATH": str(folder / "backend"), "PYTHONDONTWRITEBYTECODE": "1"}
            seed = """from datetime import date
from app.database import init_db,engine,SessionLocal
from app.models import User,AwsCredential,CreditRecord
init_db()
with SessionLocal() as db:
 u=User(username='legacy',password_hash='opaque');db.add(u);db.flush()
 c=AwsCredential(user_id=u.id,access_key_id_enc='opaque',secret_access_key_enc='opaque',account_id='123456789012');db.add(c);db.flush()
 db.add(CreditRecord(user_id=u.id,credential_id=c.id,name='legacy grant',total_amount=0.12345678912345678,currency='USD',start_date=date(2026,11,1)))
 db.commit()
engine.dispose()
"""
            seeded = subprocess.run([sys.executable, "-c", seed], env=env, cwd=folder, capture_output=True, text=True)
            self.assertEqual(seeded.returncode, 0, seeded.stderr)
            before = file_sha256(path)
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            original = migrations.apply_v3
            def fail(conn, metadata):
                original(conn, metadata)
                raise RuntimeError("injected after v3 DDL")
            with patch.object(migrations, "apply_v3", side_effect=fail), self.assertRaises(migrations.MigrationError):
                migrations.upgrade_database(engine, Base.metadata)
            self.assertEqual(file_sha256(path), before)
            migrations.upgrade_database(engine, Base.metadata)
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,), (2,), (3,), (4,), (5,)])
                value, account, precision = conn.execute("SELECT total_amount_decimal,account_id,amount_precision FROM credit_records").fetchone()
                self.assertEqual(value, "0.12345678912345678")
                self.assertIsNone(account)
                self.assertEqual(precision, "legacy_float")
                backup, sha = conn.execute("SELECT backup_path,backup_sha256 FROM schema_migrations WHERE version=3").fetchone()
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            restored = folder / "restored.db"
            restore_database(backup, restored, sha, offline=True)
            with closing(sqlite3.connect(restored)) as conn:
                self.assertEqual(conn.execute("SELECT version FROM schema_migrations").fetchall(), [(1,), (2,)])
                self.assertNotIn("account_id", {row[1] for row in conn.execute("PRAGMA table_info(credit_records)")})
            migrations.upgrade_database(engine, Base.metadata)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
