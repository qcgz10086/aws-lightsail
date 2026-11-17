"""Tests for the measurement boundary, not optimization acceptance tests."""
import os
import sys
import socket
import unittest
from pathlib import Path

os.environ.update({"DATABASE_URL": "sqlite:///:memory:", "SECRET_KEY": "test-only-secret-key-0123456789abcdef", "ENCRYPTION_KEY": "WWevy1rc32FPbz51VPopb5qw2SqEo-6VNp0jX1b4_EE=", "ADMIN_PASSWORD": "TestAdminPass123!"})
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))

from tools.optimization.fake_aws import FixtureAws, block_external_connections
from tools.optimization.measure import summarize

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "aws.json"


class MeasurementTests(unittest.TestCase):
    def test_p95_uses_nearest_rank_and_keeps_every_sample(self):
        result = summarize([20, 1, 2, 3, 4, 5, 6, 7, 8, 9,
                            10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
        self.assertEqual(result["count"], 20)
        self.assertEqual(result["median_ms"], 10.5)
        self.assertEqual(result["p95_ms"], 19)
        self.assertEqual(result["max_ms"], 20)

    def test_empty_samples_are_missing_not_zero(self):
        self.assertEqual(summarize([]), {
            "count": 0, "median_ms": None, "p95_ms": None, "max_ms": None,
        })

    def test_unknown_credentials_and_operations_cannot_fall_through(self):
        aws = FixtureAws(FIXTURE, delay_ms=0)
        with self.assertRaises(ValueError):
            aws.client("lightsail", aws_access_key_id="UNRECOGNIZED")
        client = aws.client("lightsail", aws_access_key_id="T00_ALPHA_PRIMARY",
                            region_name="us-east-1")
        with self.assertRaises(RuntimeError):
            client.delete_instance(instanceName="do-not-delete")
        self.assertEqual(len(aws.snapshot()["violations"]), 2)

    def test_real_service_consumes_all_fixture_pages(self):
        from unittest.mock import patch
        from app.services import lightsail
        aws = FixtureAws(FIXTURE, delay_ms=0)
        with patch("boto3.client", aws.client):
            items = lightsail.list_instances_in_region(
                "T00_ALPHA_PRIMARY", "fictional", "us-east-1")
        self.assertEqual(len(items), 6)
        self.assertEqual(len({item["name"] for item in items}), 6)
        self.assertEqual(aws.snapshot()["calls"], {
            "lightsail.get_instances": 3, "lightsail.get_static_ips": 1,
        })

    def test_duplicate_keys_represent_the_same_account(self):
        aws = FixtureAws(FIXTURE, delay_ms=0)
        ids = [aws.client("sts", aws_access_key_id=key).get_caller_identity()["Account"]
               for key in ("T00_ALPHA_PRIMARY", "T00_ALPHA_DUPLICATE", "T00_BETA_PRIMARY")]
        self.assertEqual(ids, ["111111111111", "111111111111", "222222222222"])

    def test_external_socket_is_rejected_before_connect(self):
        with block_external_connections():
            with self.assertRaisesRegex(RuntimeError, "external connection blocked"):
                with socket.socket() as connection:
                    connection.connect(("192.0.2.10", 443))

    def test_fixture_is_not_mutated_by_service_consumers(self):
        aws = FixtureAws(FIXTURE, delay_ms=0)
        client = aws.client("lightsail", aws_access_key_id="T00_ALPHA_PRIMARY",
                            region_name="us-east-1")
        client.get_instances()["instances"][0]["name"] = "changed"
        self.assertEqual(client.get_instances()["instances"][0]["name"],
                         "t00-111111111111-us-east-1-00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
