"""Strict, offline AWS boundary for the T00 workload."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import socket
from threading import Lock
import time
from unittest.mock import patch


@contextmanager
def block_external_connections():
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def guard(original):
        def checked(sock, address):
            host = address[0] if isinstance(address, tuple) else ""
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == "localhost"
            if not allowed:
                raise RuntimeError(f"external connection blocked: {address!r}")
            return original(sock, address)
        return checked

    with patch.object(socket.socket, "connect", guard(connect)), patch.object(
        socket.socket, "connect_ex", guard(connect_ex)
    ):
        yield


class FixtureAws:
    ALLOWED = {
        "lightsail": {"get_regions", "get_instances", "get_static_ips",
                     "get_bundles", "get_blueprints"},
        "ce": {"get_cost_and_usage", "get_cost_forecast"},
        "sts": {"get_caller_identity"},
        "billing": {"get_credits"},
    }

    def __init__(self, fixture: Path, delay_ms: int | None = None):
        data = fixture.read_bytes()
        self.digest = hashlib.sha256(data).hexdigest()
        self.data = json.loads(data)
        self.delay = (self.data["delay_ms"] if delay_ms is None else delay_ms) / 1000
        self._lock = Lock()
        self._calls = Counter()
        self._events = []
        self._violations = []
        self._active = 0
        self._peak = 0

    def reject(self, message, exception=RuntimeError):
        with self._lock:
            self._violations.append(message)
        raise exception(message)

    def client(self, service_name, **kwargs):
        key = kwargs.get("aws_access_key_id")
        if service_name not in self.ALLOWED:
            self.reject(f"unsupported service: {service_name}")
        if key not in self.data["keys"]:
            self.reject(f"unknown fixture credential: {key!r}", ValueError)
        return _Client(self, service_name, key, kwargs.get("region_name", "us-east-1"))

    def reset(self):
        with self._lock:
            if self._active:
                raise RuntimeError("cannot reset while AWS calls are active")
            self._calls.clear()
            self._events.clear()
            self._violations.clear()
            self._peak = 0

    def snapshot(self):
        with self._lock:
            return {"calls": dict(self._calls), "events": deepcopy(self._events),
                    "violations": list(self._violations), "active": self._active,
                    "peak_concurrency": self._peak, "fixture_sha256": self.digest}

    def invoke(self, service, key, region, operation, params):
        with self._lock:
            self._calls[f"{service}.{operation}"] += 1
            self._events.append({"service": service, "operation": operation,
                                 "key": key, "account_id": self.data["keys"][key],
                                 "region": region, "params": deepcopy(params)})
            self._active += 1
            self._peak = max(self._peak, self._active)
        try:
            time.sleep(self.delay)
            return deepcopy(self._response(service, key, region, operation, params))
        finally:
            with self._lock:
                self._active -= 1

    def _page(self, rows, params, name):
        size = self.data["page_size"]
        token = params.get("pageToken")
        if token is not None and (not str(token).isdigit() or int(token) >= len(rows)):
            self.reject(f"invalid page token: {token!r}")
        start = int(token or 0)
        result = {name: rows[start:start + size]}
        if start + size < len(rows):
            result["nextPageToken"] = str(start + size)
        return result

    def _response(self, service, key, region, operation, params):
        account = self.data["keys"][key]
        if service == "sts":
            return {"Account": account, "UserId": key,
                    "Arn": f"arn:aws:iam::{account}:user/t00-fixture"}
        if service == "ce":
            b = self.data["billing"]
            if operation == "get_cost_forecast":
                return {"Total": {"Amount": b["forecast"], "Unit": "USD"}}
            metrics = {"UnblendedCost": {"Amount": b["total"], "Unit": "USD"},
                       "NetUnblendedCost": {"Amount": b["net"], "Unit": "USD"}}
            period = {"TimePeriod": params["TimePeriod"], "Estimated": True}
            if params.get("GroupBy"):
                period["Groups"] = [{"Keys": ["Amazon Lightsail"], "Metrics": metrics}]
            else:
                period["Total"] = metrics
            return {"ResultsByTime": [period]}
        if service == "billing":
            if params.get("accountId") != account:
                self.reject("GetCredits account does not match fixture credential")
            c = self.data["credit"]
            money = lambda name: {"currencyAmount": c[name], "currencyCode": "USD"}
            return {"credits": [{
                "creditId": f"T00-CREDIT-{account}", "description": "T00 fixture credit",
                "creditType": "PROMOTIONAL", "creditStatus": "ACTIVE",
                "initialAmount": money("initial"), "remainingAmount": money("remaining"),
                "estimatedAmount": money("estimated"),
                "startDate": "2026-01-01", "endDate": "2027-01-01",
                "applicableProductNames": ["Amazon Lightsail"],
            }]}
        if operation == "get_regions":
            return {"regions": [
                {"name": r, "displayName": r, "continentCode": "NA" if r.startswith("us") else "AS",
                 "availabilityZones": [{"zoneName": r + "a", "state": "available"}]}
                for r in self.data["regions"]
            ]}
        if region not in self.data["regions"]:
            self.reject(f"unknown fixture region: {region}")
        if operation == "get_instances":
            rows = [{
                "name": f"t00-{account}-{region}-{n:02}",
                "arn": f"arn:aws:lightsail:{region}:{account}:Instance/t00-{n}",
                "location": {"regionName": region, "availabilityZone": region + "a"},
                "state": {"code": 16, "name": "running"},
                "publicIpAddress": f"203.0.113.{n + 1}", "privateIpAddress": f"10.0.0.{n + 1}",
                "blueprintId": "ubuntu_24_04", "blueprintName": "Ubuntu",
                "bundleId": "nano_3_0", "isStaticIp": False,
                "createdAt": datetime.fromisoformat(self.data["date"]),
            } for n in range(self.data["instances_per_region"])]
            return self._page(rows, params, "instances")
        if operation == "get_static_ips":
            return {"staticIps": []}
        if operation == "get_bundles":
            rows = [{"bundleId": f"bundle-{n}", "name": f"Fixture {n}", "price": 5.0 + n,
                     "cpuCount": 2, "ramSizeInGb": 1.0 + n, "diskSizeInGb": 20 + n * 10,
                     "transferPerMonthInGb": 1024, "power": n + 1, "isActive": True,
                     "supportedPlatforms": ["LINUX_UNIX"]} for n in range(4)]
            return self._page(rows, params, "bundles")
        if operation == "get_blueprints":
            rows = [{"blueprintId": f"ubuntu_24_04_{n}", "name": f"Ubuntu fixture {n}",
                     "group": "ubuntu", "type": "os", "platform": "LINUX_UNIX",
                     "version": "24.04", "isActive": True} for n in range(4)]
            return self._page(rows, params, "blueprints")
        self.reject(f"missing fixture response: {service}.{operation}")


class _Client:
    def __init__(self, fixture, service, key, region):
        self.fixture, self.service, self.key, self.region = fixture, service, key, region

    def __getattr__(self, operation):
        if operation not in self.fixture.ALLOWED[self.service]:
            self.fixture.reject(f"unsupported AWS operation: {self.service}.{operation}")
        return lambda **params: self.fixture.invoke(
            self.service, self.key, self.region, operation, params
        )
