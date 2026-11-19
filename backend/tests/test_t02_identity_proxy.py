"""T02 account identity, proxy failure, and policy-conflict regressions."""
import os
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import AwsCredential, InstanceSetting, TrafficUsage, User
from app.security import encrypt_secret, hash_password
from app.services import aws_clients, billing, lightsail
from app.services.collector import sync_user_traffic
from app.services.traffic import (conflicting_auto_stop_credentials, reconcile_auto_stop_conflicts,
                                  resolve_auto_stop_policy, build_traffic_summary, current_year_month,
                                  upsert_traffic, traffic_for_instance)


class T02Tests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.old_overrides = dict(app.dependency_overrides)

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)
        self.user = User(username="t02-alice", password_hash=hash_password("AlicePass123!x"), is_admin=False)
        self.other = User(username="t02-bob", password_hash=hash_password("BobPass123!x"), is_admin=False)
        self.db.add_all([self.user, self.other])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.other)
        self.token = self.client.post("/api/auth/login", json={"username": "t02-alice", "password": "AlicePass123!x"}).json()["access_token"]
        self.other_token = self.client.post("/api/auth/login", json={"username": "t02-bob", "password": "BobPass123!x"}).json()["access_token"]
        self.client.cookies.clear()

    def tearDown(self):
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self.old_overrides)
        self.db.close()

    def auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def credential(self, user=None, account_id="123456789012", label="account", key="AKIAIOSFODNN7EXAMPLE", is_default=False):
        row = AwsCredential(user_id=(user or self.user).id, access_key_id_enc=encrypt_secret(key),
                            secret_access_key_enc=encrypt_secret("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
                            account_label=label, is_default=is_default, account_id=account_id,
                            last_confirmed_account_id=account_id)
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row

    def setting(self, cred, auto=True, limit=1, configured=True, region="us-east-1", name="server"):
        row = InstanceSetting(user_id=cred.user_id, credential_id=cred.id, region=region,
                               instance_name=name, monthly_limit_gb=limit, auto_stop_on_limit=auto,
                               auto_stop_configured=configured,
                               auto_stop_account_id=cred.account_id if configured else None)
        self.db.add(row)
        self.db.commit()
        return row

    def test_account_id_is_persisted_and_exposed_without_access_key_material(self):
        with patch("app.routers.credentials.ls.validate_credentials", return_value={}), \
             patch("app.routers.credentials.billing_svc.get_sts_identity", return_value="123456789012"), \
             patch("app.routers.credentials._apply_quota_to_cred"):
            response = self.client.post("/api/credentials", headers=self.auth(), json={
                "access_key_id": "AKIAIOSFODNN7EXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            })
        self.assertEqual(response.status_code, 201, response.text)
        stored = self.db.query(AwsCredential).one()
        self.assertEqual(stored.account_id, "123456789012")
        item = response.json()["items"][0]
        self.assertEqual(item["account_id"], stored.account_id)
        self.assertNotIn("access_key_id", item)

    def test_identity_failure_saves_credential_as_unknown_without_reusing_old_identity(self):
        old = self.credential(account_id="123456789012")
        with patch("app.routers.credentials.ls.validate_credentials", return_value={}), \
             patch("app.routers.credentials.billing_svc.get_sts_identity", side_effect=billing.BillingUnavailableError("STS offline")), \
             patch("app.routers.credentials._apply_quota_to_cred"):
            response = self.client.put(f"/api/credentials/{old.id}", headers=self.auth(), json={"access_key_id": "ASIAAAAAAAAAAAAAAAAA"})
        self.assertEqual(response.status_code, 200, response.text)
        self.db.refresh(old)
        self.assertIsNone(old.account_id)
        self.assertIsNone(old.last_confirmed_account_id)
        self.assertIsNone(response.json()["items"][0]["account_id"])

    def test_sts_outage_preserves_last_known_conflict_and_blocks_other_key_stop(self):
        enabled = self.credential(account_id="123456789012", label="enabled", is_default=True)
        disabled = self.credential(account_id="123456789012", label="disabled", key="AKIAIOSFODNN8EXAMPLE")
        enabled_setting = self.setting(enabled, auto=True, limit=1)
        disabled_setting = self.setting(disabled, auto=False, limit=1)

        def scan():
            with patch("app.services.collector.ls.get_regions", return_value=[{"name": "us-east-1"}]), \
                 patch("app.services.collector.ls.list_instances_in_region", return_value=[{"name": "server", "state": "running"}]), \
                 patch("app.services.collector.ls.get_metric_sum_bytes", return_value=2 * 1024**3), \
                 patch("app.services.collector.ls.stop_instance") as stop:
                sync_user_traffic(self.db, self.user)
                return stop

        self.assertEqual(len(reconcile_auto_stop_conflicts(self.db, self.user)), 1)
        self.assertEqual(scan().call_count, 0)
        with patch("app.routers.credentials.ls.validate_credentials", return_value={}), \
             patch("app.routers.credentials.billing_svc.get_sts_identity", side_effect=RuntimeError("temporary STS failure")), \
             patch("app.routers.credentials._apply_quota_to_cred"):
            response = self.client.post(f"/api/credentials/{disabled.id}/validate", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.db.refresh(disabled)
        self.db.refresh(disabled_setting)
        self.assertIsNone(disabled.account_id)
        self.assertEqual(disabled.last_confirmed_account_id, "123456789012")
        self.assertTrue(disabled_setting.auto_stop_conflict)
        self.assertTrue(enabled_setting.auto_stop_conflict)
        self.assertEqual(scan().call_count, 0)

    def test_credentials_are_scoped_to_user_and_account_identity(self):
        first = self.credential(account_id="123456789012", label="first")
        same = self.credential(account_id="123456789012", label="same", key="AKIAIOSFODNN8EXAMPLE")
        other_account = self.credential(account_id="210987654321", label="other", key="AKIAIOSFODNN8EYAMPLE")
        other_user = self.credential(user=self.other, account_id="123456789012", label="other-user")
        scopes = aws_clients.group_credentials([first, same, other_account, other_user])
        self.assertEqual(scopes[(self.user.id, "123456789012")], (first, same))
        self.assertEqual(scopes[(self.user.id, "210987654321")], (other_account,))
        self.assertEqual(scopes[(self.other.id, "123456789012")], (other_user,))
        unknown = self.credential(account_id=None, label="unknown")
        self.assertNotIn((self.user.id, ""), aws_clients.group_credentials([unknown]))

    def test_one_client_factory_passes_same_explicit_credentials_and_proxy_to_all_services(self):
        from botocore.httpsession import URLLib3Session
        from urllib3.poolmanager import PoolManager, ProxyManager
        from app.proxy_config import proxy_config
        with patch.object(aws_clients.boto3, "client") as session_factory:
            aws_clients.make_client("lightsail", "AKIA", "secret", "us-west-2", "http://proxy.example.com:8080")
        session_factory.assert_called_once()
        self.assertEqual(session_factory.call_args.kwargs["aws_access_key_id"], "AKIA")
        self.assertEqual(session_factory.call_args.kwargs["aws_secret_access_key"], "secret")
        self.assertEqual(session_factory.call_args.kwargs["config"].proxies,
                         proxy_config("http://proxy.example.com:8080").proxies)

        env_proxy = "http://environment-proxy.invalid:8080"
        credential_proxy = "http://credential-proxy.invalid:9090"
        with patch.dict(os.environ, {"HTTP_PROXY": env_proxy, "HTTPS_PROXY": env_proxy,
                                     "http_proxy": env_proxy, "https_proxy": env_proxy,
                                     "NO_PROXY": "*", "no_proxy": "*"}):
            direct_config = proxy_config(None)
            self.assertEqual(direct_config.proxies, {"http": None, "https": None})
            direct_session = URLLib3Session(proxies=direct_config.proxies)
            explicit_config = proxy_config(credential_proxy)
            explicit_session = URLLib3Session(proxies=explicit_config.proxies)
            for _ in range(3):
                for scheme in ("http", "https"):
                    url = f"{scheme}://service.example.invalid/"
                    direct_url = direct_session._proxy_config.proxy_url_for(url)
                    self.assertIsNone(direct_url)
                    self.assertIsInstance(direct_session._get_connection_manager(url, direct_url), PoolManager)
                    explicit_url = explicit_session._proxy_config.proxy_url_for(url)
                    self.assertEqual(explicit_url, credential_proxy)
                    self.assertIsInstance(explicit_session._get_connection_manager(url, explicit_url), ProxyManager)

    def test_all_service_factories_delegate_to_common_session_factory(self):
        with patch.object(aws_clients.boto3, "client") as factory:
            lightsail.make_client("AKIA", "secret", "eu-west-1", "http://proxy.example.com:8080")
            self.assertEqual(factory.call_args.args[0], "lightsail")
            self.assertEqual(factory.call_args.kwargs["aws_access_key_id"], "AKIA")
            self.assertEqual(factory.call_args.kwargs["config"].proxies["https"], "http://proxy.example.com:8080")
            billing.make_sts_client("AKIA", "secret", "http://proxy.example.com:8080")
            self.assertEqual(factory.call_args.args[0], "sts")
            billing.make_billing_client("AKIA", "secret")
            self.assertEqual(factory.call_args.args[0], "billing")
            billing.make_ce_client("AKIA", "secret")
            self.assertEqual(factory.call_args.args[0], "ce")

    def test_corrupt_stored_proxy_rejects_key_rotation_without_falling_back_to_direct(self):
        cred = self.credential()
        cred.proxy_url_enc = "corrupt-encrypted-proxy"
        self.db.commit()
        with patch("app.routers.credentials.ls.validate_credentials") as validate:
            response = self.client.put(f"/api/credentials/{cred.id}", headers=self.auth(), json={"secret_access_key": "new-secret-value-123"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("代理", response.json()["detail"])
        validate.assert_not_called()
        self.db.refresh(cred)
        self.assertNotEqual(cred.secret_access_key_enc, encrypt_secret("new-secret-value-123"))

    def test_explicitly_clearing_proxy_allows_direct_validation_but_omitting_it_does_not(self):
        cred = self.credential()
        cred.proxy_url_enc = "corrupt-encrypted-proxy"
        self.db.commit()
        with patch("app.routers.credentials.ls.validate_credentials") as validate, \
             patch("app.routers.credentials.billing_svc.get_sts_identity", return_value=None), \
             patch("app.routers.credentials._apply_quota_to_cred"):
            omitted = self.client.put(f"/api/credentials/{cred.id}", headers=self.auth(), json={"secret_access_key": "new-secret-value-123"})
            self.assertEqual(omitted.status_code, 400)
            validate.assert_not_called()
            cleared = self.client.put(f"/api/credentials/{cred.id}", headers=self.auth(), json={"proxy_url": ""})
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertGreaterEqual(validate.call_count, 1)
        self.db.refresh(cred)
        self.assertIsNone(cred.proxy_url_enc)

    def test_automatic_conflicting_strategies_are_preserved_but_paused(self):
        first = self.credential(account_id="123456789012", label="first")
        second = self.credential(account_id="123456789012", label="second", key="AKIAIOSFODNN8EXAMPLE")
        self.setting(first, auto=True, limit=1)
        self.setting(second, auto=True, limit=2)
        states = reconcile_auto_stop_conflicts(self.db, self.user)
        identity = ("123456789012", "us-east-1", "server")
        self.assertIn(identity, states)
        self.assertTrue(first.auto_stop_conflict)
        self.assertTrue(second.auto_stop_conflict)
        self.assertEqual(self.db.query(InstanceSetting).filter(InstanceSetting.auto_stop_on_limit.is_(True)).count(), 2)
        self.assertEqual(conflicting_auto_stop_credentials(self.db, first), {first.id, second.id})

    def test_explicit_true_vs_false_conflicts_but_missing_strategy_does_not(self):
        enabled = self.credential(account_id="123456789012", label="enabled")
        disabled = self.credential(account_id="123456789012", label="disabled", key="AKIAIOSFODNN8EXAMPLE")
        self.setting(enabled, True, 1)
        self.setting(disabled, False, 1)
        conflicts = reconcile_auto_stop_conflicts(self.db, self.user)
        self.assertIn(("123456789012", "us-east-1", "server"), conflicts)
        self.assertTrue(all(row.auto_stop_conflict for row in conflicts[("123456789012", "us-east-1", "server")]))
        self.assertFalse(resolve_auto_stop_policy(self.db, self.user, enabled, "us-east-1", "server")[0])

    def test_two_enabled_different_effective_thresholds_conflict_but_equal_thresholds_do_not(self):
        first = self.credential(account_id="123456789012", label="first")
        second = self.credential(account_id="123456789012", label="second", key="AKIAIOSFODNN8EXAMPLE")
        self.setting(first, True, 1)
        self.setting(second, True, 2)
        self.assertIn(("123456789012", "us-east-1", "server"), reconcile_auto_stop_conflicts(self.db, self.user))
        self.db.query(InstanceSetting).filter_by(credential_id=second.id).one().monthly_limit_gb = 1
        self.db.flush()
        self.assertIn(("123456789012", "us-east-1", "server"),
                      reconcile_auto_stop_conflicts(self.db, self.user))
        self.assertTrue(resolve_auto_stop_policy(self.db, self.user, first, "us-east-1", "server")[2])

    def test_patch_cannot_silently_release_conflict_and_collector_stays_paused(self):
        first = self.credential(is_default=True)
        second = self.credential(key="AKIAIOSFODNN8EXAMPLE")
        first_setting = self.setting(first, True, 1)
        self.setting(second, False, 1)
        reconcile_auto_stop_conflicts(self.db, self.user)
        self.db.commit()
        raw = {"region": "us-east-1", "name": "server", "state": "running"}

        def save(cred, values):
            with patch("app.routers.instances.ls.get_instance", return_value=raw):
                return self.client.patch(
                    f"/api/instances/us-east-1/server/settings?credential_id={cred.id}",
                    headers=self.auth(), json=values)

        for cred, values in ((first, {"auto_stop_on_limit": False}),
                             (first, {"auto_stop_on_limit": True}),
                             (second, {"auto_stop_on_limit": True})):
            response = save(cred, values)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["auto_stop_conflict"])
            with patch("app.services.collector.ls.get_regions", return_value=[{"name": "us-east-1"}]), \
                 patch("app.services.collector.ls.list_instances_in_region", return_value=[raw]), \
                 patch("app.services.collector.ls.get_metric_sum_bytes", return_value=2 * 1024**3), \
                 patch("app.services.collector.ls.stop_instance") as stop:
                sync_user_traffic(self.db, self.user)
                stop.assert_not_called()

        rejected = save(first, {"resolve_conflict_credential_ids": [first.id]})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.db.rollback()
        self.db.refresh(first_setting)
        self.assertTrue(first_setting.auto_stop_conflict)
        details = self.client.get(f"/api/instances/us-east-1/server/policy-conflict?credential_id={first.id}",
                                  headers=self.auth())
        self.assertEqual(details.status_code, 200, details.text)
        self.assertEqual([row["credential_id"] for row in details.json()["strategies"]],
                         [first.id, second.id])
        stale = save(first, {"resolve_conflict_credential_ids": [first.id, second.id],
                              "resolve_conflict_token": "stale-confirmation"})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.db.rollback()
        resolved = save(first, {"resolve_conflict_credential_ids": [first.id, second.id],
                                "resolve_conflict_token": details.json()["confirmation_token"]})
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertFalse(resolved.json()["auto_stop_conflict"])
        with patch("app.services.collector.ls.get_regions", return_value=[{"name": "us-east-1"}]), \
             patch("app.services.collector.ls.list_instances_in_region", return_value=[raw]), \
             patch("app.services.collector.ls.get_metric_sum_bytes", return_value=2 * 1024**3), \
             patch("app.services.collector.ls.stop_instance") as stop:
            sync_user_traffic(self.db, self.user)
            stop.assert_called_once()

    def test_conflict_acknowledgement_rejects_uncoordinated_unknown_or_foreign_policies(self):
        first = self.credential(is_default=True)
        second = self.credential(key="AKIAIOSFODNN8EXAMPLE")
        foreign = self.credential(user=self.other)
        self.setting(first, True, 1)
        second_setting = self.setting(second, False, 1)
        reconcile_auto_stop_conflicts(self.db, self.user)
        self.db.commit()
        url = f"/api/instances/us-east-1/server/settings?credential_id={first.id}"
        for ids in ([first.id, second.id], [first.id, second.id, foreign.id]):
            response = self.client.patch(url, headers=self.auth(),
                                         json={"resolve_conflict_credential_ids": ids})
            self.assertEqual(response.status_code, 409, response.text)
            self.db.rollback()
        second_setting.auto_stop_on_limit = True
        second.account_id = None
        self.db.commit()
        response = self.client.patch(url, headers=self.auth(),
            json={"resolve_conflict_credential_ids": [first.id, second.id]})
        self.assertEqual(response.status_code, 409, response.text)
        self.db.rollback()
        forbidden = self.client.get(
            f"/api/instances/us-east-1/server/policy-conflict?credential_id={first.id}",
            headers=self.auth(self.other_token))
        self.assertEqual(forbidden.status_code, 400, forbidden.text)
        self.assertTrue(resolve_auto_stop_policy(self.db, self.user, first, "us-east-1", "server")[2])

    def test_two_explicitly_disabled_strategies_and_absent_record_are_distinct(self):
        first = self.credential(account_id="123456789012", label="first")
        second = self.credential(account_id="123456789012", label="second", key="AKIAIOSFODNN8EXAMPLE")
        self.setting(first, False, 1)
        self.setting(second, False, 2)
        self.assertEqual(reconcile_auto_stop_conflicts(self.db, self.user), {})
        self.assertFalse(resolve_auto_stop_policy(self.db, self.user, first, "us-east-1", "server")[0])
        self.assertFalse(resolve_auto_stop_policy(self.db, self.user, second, "us-east-1", "server")[2])
        self.assertEqual(self.db.query(InstanceSetting).filter(InstanceSetting.credential_id == first.id).count(), 1)

    def test_account_id_change_pauses_old_strategy_without_moving_it(self):
        credential = self.credential(account_id="123456789012")
        row = self.setting(credential, True, 1)
        credential.account_id = "210987654321"
        self.db.flush()
        enabled, limit, conflict, identity_changed = resolve_auto_stop_policy(
            self.db, self.user, credential, "us-east-1", "server")
        self.assertFalse(enabled)
        self.assertIsNone(limit)
        self.assertFalse(conflict)
        self.assertTrue(identity_changed)
        self.assertEqual(row.auto_stop_account_id, "123456789012")
        self.assertEqual(row.credential_id, credential.id)

    def test_paused_policy_response_keeps_saved_value_and_note_edit_does_not_disable_it(self):
        first = self.credential(is_default=True)
        second = self.credential(key="AKIAIOSFODNN8EXAMPLE")
        saved = self.setting(first, True, 1)
        self.setting(second, False, 1)
        raw = {"region": "us-east-1", "name": "server", "state": "running"}
        with patch("app.routers.instances.ls.get_instance", return_value=raw):
            response = self.client.get(f"/api/instances/us-east-1/server?credential_id={first.id}",
                                       headers=self.auth())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertFalse(response.json()["auto_stop_on_limit"])
            self.assertTrue(response.json()["configured_auto_stop_on_limit"])
            response = self.client.patch(
                f"/api/instances/us-east-1/server/settings?credential_id={first.id}",
                headers=self.auth(), json={"note": "keep original strategy"})
        self.assertEqual(response.status_code, 200, response.text)
        self.db.refresh(saved)
        self.assertTrue(saved.auto_stop_on_limit)
        self.assertTrue(saved.auto_stop_conflict)
        self.assertTrue(response.json()["configured_auto_stop_on_limit"])

    def test_changed_account_does_not_apply_old_limit_to_instance_traffic(self):
        credential = self.credential()
        saved = self.setting(credential, True, 1)
        credential.account_id = "210987654321"
        self.db.commit()
        upsert_traffic(self.db, self.user.id, credential.id, "us-east-1", "server",
                       current_year_month(), 2 * 1024**3, 0, credential.account_id)
        self.db.commit()
        with patch("app.routers.instances.ls.get_instance", return_value={
            "region": "us-east-1", "name": "server", "state": "running"}):
            response = self.client.get(f"/api/instances/us-east-1/server?credential_id={credential.id}",
                                       headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["auto_stop_identity_changed"])
        self.assertIsNone(response.json()["traffic"]["limit_gb"])
        self.assertFalse(response.json()["traffic"]["over_limit"])
        self.assertEqual(saved.auto_stop_account_id, "123456789012")
        self.assertEqual(saved.monthly_limit_gb, 1)

    def test_unknown_identity_keeps_settings_isolated(self):
        known = self.credential(account_id="123456789012", label="known")
        unknown = self.credential(account_id=None, label="unknown", key="AKIAIOSFODNN8EXAMPLE")
        self.setting(known, auto=True, limit=2)
        self.setting(unknown, auto=True, limit=3)
        self.assertEqual(reconcile_auto_stop_conflicts(self.db, self.user), {})
        enabled, limit, conflict, identity_changed = resolve_auto_stop_policy(
            self.db, self.user, unknown, "us-east-1", "server")
        self.assertTrue(enabled)
        self.assertEqual(limit, 3)
        self.assertFalse(conflict)
        self.assertFalse(identity_changed)

    def test_manual_validation_refreshes_sts_identity(self):
        credential = self.credential(account_id=None)
        with patch("app.routers.credentials.ls.validate_credentials", return_value={}), \
             patch("app.routers.credentials.billing_svc.get_sts_identity", return_value="123456789012"), \
             patch("app.routers.credentials._apply_quota_to_cred"):
            response = self.client.post(f"/api/credentials/{credential.id}/validate", headers=self.auth())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["account_id"], "123456789012")
        self.db.refresh(credential)
        self.assertEqual(credential.account_id, "123456789012")

    def test_compat_update_rejects_omitted_corrupt_proxy(self):
        credential = self.credential()
        credential.is_default = True
        credential.proxy_url_enc = "corrupt-encrypted-proxy"
        self.db.commit()
        with patch("app.routers.credentials.ls.validate_credentials") as validate:
            response = self.client.put("/api/credentials", headers=self.auth(), json={
                "access_key_id": "ASIAAAAAAAAAAAAAAAAA",
                "secret_access_key": "new-secret-value-123",
            })
        self.assertEqual(response.status_code, 400)
        validate.assert_not_called()

    def test_default_switch_does_not_bypass_policy_conflict_and_scan_once(self):
        first = self.credential(account_id="123456789012", label="first", is_default=True)
        second = self.credential(account_id="123456789012", label="second", key="AKIAIOSFODNN8EXAMPLE")
        self.setting(first, auto=True, limit=1)
        self.setting(second, auto=False, limit=1)
        with patch("app.services.collector.ls.get_regions", return_value=[{"name": "us-east-1"}]) as regions, \
             patch("app.services.collector.ls.list_instances_in_region", return_value=[{"name": "server", "state": "running"}]), \
             patch("app.services.collector.ls.get_metric_sum_bytes", return_value=2 * 1024**3), \
             patch("app.services.collector.ls.stop_instance") as stop:
            self.assertEqual(sync_user_traffic(self.db, self.user), 1)
            regions.assert_called_once()
            stop.assert_not_called()
            self.db.refresh(first)
            self.assertFalse(resolve_auto_stop_policy(self.db, self.user, first, "us-east-1", "server")[0])
            response = self.client.post(f"/api/credentials/{second.id}/default", headers=self.auth())
            self.assertEqual(response.status_code, 200)
            self.assertFalse(resolve_auto_stop_policy(self.db, self.user, second, "us-east-1", "server")[0])

    def test_traffic_summary_respects_explicit_credential_filter(self):
        first = self.credential(account_id="123456789012", label="first")
        second = self.credential(account_id="123456789012", label="second", key="AKIAIOSFODNN8EXAMPLE")
        for cred, amount in ((first, 10), (second, 20)):
            self.db.add(TrafficUsage(user_id=self.user.id, credential_id=cred.id, region="us-east-1",
                                     instance_name=f"server-{cred.id}", year_month=current_year_month(),
                                     network_in_bytes=amount, network_out_bytes=0))
        self.db.commit()
        summary = build_traffic_summary(self.db, self.user, second.id)
        self.assertEqual([row["credential_id"] for row in summary["instances"]], [second.id])

    def test_unknown_traffic_is_visible_but_excluded_from_confirmed_total(self):
        credential = self.credential(account_id=None)
        upsert_traffic(self.db, self.user.id, credential.id, "us-east-1", "server",
                       current_year_month(), 2 * 1024**3, 0)
        self.db.commit()
        summary = build_traffic_summary(self.db, self.user)
        self.assertEqual(summary["instances"][0]["total_gb"], 2)
        self.assertIsNone(summary["instances"][0]["account_id"])
        self.assertEqual(summary["by_region"], [])

    def test_instance_list_deduplicates_account_and_batches_database_reads(self):
        credential = self.credential(is_default=True)
        self.credential(key="AKIAIOSFODNN8EXAMPLE")
        raw = [{"region": "us-east-1", "name": f"server-{index}", "state": "running"}
               for index in range(24)]
        self.db.refresh(self.user)
        # Authenticate outside the measured route and keep all AWS calls mocked.
        from app.deps import get_current_user
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.addCleanup(lambda: app.dependency_overrides.pop(get_current_user, None))
        # T04 adds persistent snapshot bookkeeping on cold reads. Measure the hot
        # route separately so policy/settings/usage SQL must stay independent of N.
        with patch("app.routers.instances.ls.get_regions", return_value=[{"name": "us-east-1"}]), \
             patch("app.routers.instances.ls.list_instances_in_region", return_value=raw):
            warm = self.client.get("/api/instances", headers=self.auth())
            self.assertEqual(warm.status_code, 200, warm.text)
        statements = []
        def count_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)
        event.listen(self.db.bind, "before_cursor_execute", count_statement)
        try:
            with patch("app.routers.instances.ls.get_regions", return_value=[{"name": "us-east-1"}]) as regions, \
                 patch("app.routers.instances.ls.list_instances_in_region", return_value=raw):
                response = self.client.get("/api/instances", headers=self.auth())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(len(response.json()), 24)
            regions.assert_not_called()
            self.assertLessEqual(len(statements), 8, f"24 cached instances issued {len(statements)} SELECTs")
        finally:
            event.remove(self.db.bind, "before_cursor_execute", count_statement)

    def test_traffic_history_stays_bound_to_observed_account_after_key_identity_change(self):
        credential = self.credential(account_id="123456789012")
        upsert_traffic(self.db, self.user.id, credential.id, "us-east-1", "server",
                             current_year_month(), 2 * 1024**3, 0, "123456789012")
        upsert_traffic(self.db, self.user.id, credential.id, "us-east-1", "server",
                             current_year_month(), 3 * 1024**3, 0, "210987654321")
        self.db.commit()
        credential.account_id = "210987654321"
        self.db.commit()

        summary = build_traffic_summary(self.db, self.user)
        self.assertEqual({row["account_id"] for row in summary["instances"]},
                         {"123456789012", "210987654321"})
        self.assertEqual(len(summary["instances"]), 2)
        old_traffic = traffic_for_instance(self.db, self.user, "us-east-1", "server", credential.id,
                                           "123456789012")
        new_traffic = traffic_for_instance(self.db, self.user, "us-east-1", "server", credential.id,
                                           "210987654321")
        self.assertEqual(old_traffic["total_gb"], 2)
        self.assertEqual(new_traffic["total_gb"], 3)

    def test_traffic_summary_query_count_is_constant_as_instance_count_grows(self):
        for size in (24, 100, 240):
            engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
            Base.metadata.create_all(engine)
            db = sessionmaker(bind=engine)()
            user = User(username=f"perf-{size}", password_hash="opaque", is_admin=False)
            credential = AwsCredential(user=user, access_key_id_enc=encrypt_secret("AKIAIOSFODNN8EXAMPLE"),
                secret_access_key_enc=encrypt_secret("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
                account_id="123456789012", last_confirmed_account_id="123456789012")
            db.add(user)
            db.flush()
            for index in range(size):
                name = f"instance-{index:03}"
                db.add(InstanceSetting(user_id=user.id, credential_id=credential.id,
                    region="us-east-1", instance_name=name, monthly_limit_gb=1,
                    auto_stop_on_limit=True, auto_stop_configured=True,
                    auto_stop_account_id=credential.account_id))
                db.add(TrafficUsage(user_id=user.id, credential_id=credential.id,
                    region="us-east-1", instance_name=name, year_month=current_year_month(),
                    account_id=credential.account_id, network_in_bytes=1024, network_out_bytes=0))
            db.commit()
            statements = []

            def count_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
                if statement.lstrip().upper().startswith("SELECT"):
                    statements.append(statement)

            event.listen(engine, "before_cursor_execute", count_statement)
            started = time.perf_counter()
            summary = build_traffic_summary(db, user)
            elapsed = time.perf_counter() - started
            event.remove(engine, "before_cursor_execute", count_statement)
            self.assertEqual(len(summary["instances"]), size)
            self.assertLessEqual(len(statements), 6, f"{size} instances issued {len(statements)} SELECTs")
            if size == 240:
                self.assertLess(elapsed, 1.0, f"240-row hot summary took {elapsed:.3f}s")
            db.close()
            engine.dispose()

    def test_same_label_in_different_panel_users_never_conflicts(self):
        alice = self.credential(account_id="123456789012", label="alice")
        bob = self.credential(user=self.other, account_id="123456789012", label="bob")
        self.setting(alice)
        self.setting(bob)
        self.assertEqual(reconcile_auto_stop_conflicts(self.db, self.user), {})
        self.assertEqual(reconcile_auto_stop_conflicts(self.db, self.other), {})

    def test_conflict_is_credential_identity_based_and_different_aws_accounts_do_not_conflict(self):
        known = self.credential(account_id="123456789012", label="known")
        unknown = self.credential(account_id=None, label="unknown", key="AKIAIOSFODNN8EXAMPLE")
        different = self.credential(account_id="210987654321", label="different", key="AKIAIOSFODNN8EYAMPLE")
        self.setting(known)
        self.setting(unknown)
        self.setting(different)
        self.assertEqual(reconcile_auto_stop_conflicts(self.db, self.user), {})


if __name__ == "__main__":
    unittest.main()
