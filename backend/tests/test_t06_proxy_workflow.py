"""T06 AWS workflow proxy inheritance tests; fake boto client only."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
os.environ.setdefault("ENCRYPTION_KEY", "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=")
os.environ.setdefault("ADMIN_PASSWORD", "TestAdminPass123!")

from app.services import lightsail as ls

PROXY = "http://alice:s3cret@proxy.example.com:8080"


class FakeLightsailClient:
    def __init__(self, events, config):
        self.events = events
        self.config = config
        self.api_calls = []
        self._make_api_call = self._record_call

    def _record_call(self, operation, parameters):
        self.api_calls.append((operation, parameters))
        self.events.append({
            "operation": operation,
            "parameters": parameters,
            "config": self.config,
        })
        if operation == "GetRegions":
            return {"regions": [{"name": "us-east-1", "availabilityZones": [
                {"zoneName": "us-east-1a", "state": "available"}]}]}
        if operation == "GetInstance":
            return {"instance": {"name": "task-server", "state": {"name": "running"}}}
        return {}

    def get_regions(self, **kwargs):
        return self._make_api_call("GetRegions", kwargs)

    def create_instances(self, **kwargs):
        return self._make_api_call("CreateInstances", kwargs)


class CreateInstanceProxyInheritanceTests(unittest.TestCase):
    def run_create(self, availability_zone):
        clients = []
        events = []

        def make_fake_client(*args, **kwargs):
            config = kwargs.get("config")
            client = FakeLightsailClient(events, config)
            clients.append(client)
            return client

        with patch("app.services.aws_clients.boto3.client", side_effect=make_fake_client), \
                patch.object(ls, "_wait_instance_not_pending", return_value=None):
            result = ls.create_instance(
                "AKID", "SECRET", "us-east-1", "task-server", "ubuntu_22_04", "nano_3_0",
                availability_zone=availability_zone, allocate_static_ip=False, proxy_url=PROXY,
            )
        self.assertEqual(result["name"], "task-server")
        return clients, events

    def test_explicit_az_uses_one_proxied_client(self):
        clients, events = self.run_create("us-east-1a")
        self.assertEqual(len(clients), 1)
        self.assertEqual(clients[0].config.proxies, {"http": PROXY, "https": PROXY})
        self.assertEqual([event["operation"] for event in events], ["CreateInstances"])
        self.assertTrue(all(event["config"].proxies == {"http": PROXY, "https": PROXY}
                            for event in events))

    def test_default_az_lookup_and_create_share_proxy(self):
        clients, events = self.run_create(None)
        self.assertEqual(len(clients), 2)
        self.assertEqual([event["operation"] for event in events],
                         ["GetRegions", "CreateInstances"])
        self.assertTrue(all(event["config"].proxies == {"http": PROXY, "https": PROXY}
                            for event in events))
