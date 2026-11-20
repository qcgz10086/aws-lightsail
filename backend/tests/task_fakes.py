"""Stateful AWS double used through the real factory/limiter; never opens sockets."""
from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import patch
from contextlib import contextmanager

from botocore.exceptions import ClientError


class FakeLightsail:
    def __init__(self, region="us-east-1", name="server", metric=2 * 1024**3):
        self.region = region
        self.metric = metric
        self.instances = {} if name is None else {name: self.instance(name)}
        self.ips = {}
        self.operations = {}
        self.calls = []
        self.call_configs = []
        self.clients = []
        self.lose = set()
        self.pending = set()
        self.fail = set()
        self.before = None

    def instance(self, name):
        return {"name": name, "arn": f"arn:aws:lightsail:{self.region}:123456789012:Instance/{name}-original",
                "state": {"name": "running"}, "tags": [], "publicIpAddress": "192.0.2.2"}

    def static_ip(self, name, attached=None):
        return {"name": name, "arn": f"arn:aws:lightsail:{self.region}:123456789012:StaticIp/{name}-original",
                "isAttached": bool(attached), "attachedTo": attached, "ipAddress": "192.0.2.4",
                "createdAt": datetime.now(timezone.utc).replace(microsecond=0)}

    def factory(self, service, **kwargs):
        assert service in {"lightsail", "account"}, service
        self.clients.append(kwargs)
        fake = self
        class Client:
            def __init__(self):
                client_config = kwargs.get("config")
                def invoke(operation, parameters):
                    fake.call_configs.append(client_config)
                    return fake.call(operation, parameters)
                self._make_api_call = invoke
            def __getattr__(self, method):
                operation = ''.join(part.title() for part in method.split('_'))
                return lambda **parameters: self._make_api_call(operation, parameters)
        return Client()

    def call(self, operation, parameters):
        self.calls.append((operation, deepcopy(parameters)))
        if self.before:
            self.before(operation, parameters)
        if operation == "GetRegions":
            return {"regions": [{"name": self.region, "availabilityZones": [
                {"zoneName": self.region + "a", "state": "available"}]}]}
        if operation == "GetRegionOptStatus":
            return {"RegionOptStatus": "ENABLED"}
        if operation == "GetInstances":
            return {"instances": deepcopy(list(self.instances.values()))}
        if operation == "GetInstance":
            item = self.instances.get(parameters["instanceName"])
            if item is None:
                raise ClientError({"Error": {"Code": "NotFoundException", "Message": "missing"}}, operation)
            return {"instance": deepcopy(item)}
        if operation == "GetStaticIps":
            return {"staticIps": deepcopy(list(self.ips.values()))}
        if operation == "GetInstanceMetricData":
            return {"metricData": [{"sum": self.metric}]}
        if operation == "GetOperation":
            return {"operation": deepcopy(self.operations[parameters["operationId"]])}
        oid = f"operation-{len(self.operations) + 1}"
        self.operations[oid] = {"id": oid, "isTerminal": operation not in self.pending,
            "status": "Failed" if operation in self.fail else "Started" if operation in self.pending else "Succeeded"}
        if operation not in self.pending and operation not in self.fail:
            if operation == "CreateInstances":
                for name in parameters["instanceNames"]:
                    self.instances[name] = {**self.instance(name), "tags": deepcopy(parameters["tags"])}
            elif operation in {"StartInstance", "StopInstance"}:
                self.instances[parameters["instanceName"]]["state"] = {
                    "name": "running" if operation == "StartInstance" else "stopped"}
            elif operation == "DeleteInstance":
                self.instances.pop(parameters["instanceName"], None)
                for ip in self.ips.values():
                    if ip.get("attachedTo") == parameters["instanceName"]:
                        ip.update(isAttached=False, attachedTo=None)
            elif operation == "AllocateStaticIp":
                self.ips[parameters["staticIpName"]] = self.static_ip(parameters["staticIpName"])
            elif operation == "AttachStaticIp":
                self.ips[parameters["staticIpName"]].update(isAttached=True, attachedTo=parameters["instanceName"])
            elif operation == "DetachStaticIp":
                self.ips[parameters["staticIpName"]].update(isAttached=False, attachedTo=None)
            elif operation == "ReleaseStaticIp":
                self.ips.pop(parameters["staticIpName"], None)
            elif operation not in {"RebootInstance", "PutInstancePublicPorts"}:
                raise AssertionError(f"Unexpected AWS operation {operation}")
        if operation in self.lose:
            self.lose.remove(operation)
            raise TimeoutError("simulated lost AWS response")
        return {"operations": [deepcopy(self.operations[oid])]}

    def count(self, operation):
        return sum(op == operation for op, _ in self.calls)

    @contextmanager
    def installed(self):
        with patch("app.services.aws_clients.boto3.client", side_effect=self.factory), \
                patch("app.services.task_handlers.WAIT_SECONDS", 0):
            yield self


def seed_discovered_regions(db, user_id, account_id, *regions):
    from app.query_models import QueryAccount, QueryRegion

    db.add(QueryAccount(user_id=user_id, account_key=account_id, discovery_state="complete"))
    db.add_all(QueryRegion(user_id=user_id, account_key=account_id, region=region, observed_state="active")
               for region in regions)
    db.commit()


def run_traffic(db, user):
    """Exercise the real enqueue and worker synchronously inside the fake boundary."""
    from app.services.collector import sync_user_traffic
    from app.services.query_rounds import QueryWorker, shutdown_queries
    from app.services.task_queue import queue_for
    queue = queue_for(db.get_bind())
    try:
        with patch.object(QueryWorker, "start", autospec=True,
                          side_effect=lambda worker, round_id: worker.run(round_id)):
            with patch.object(queue, "schedule"):
                accepted = sync_user_traffic(db, user)
            db.commit()
            for task in accepted:
                queue._execute(task["task_id"])
    finally:
        shutdown_queries(db.get_bind())
    db.expire_all()
    results = [queue.get(db, user.id, task["task_id"]) for task in accepted]
    assert all(task["status"] == "succeeded" for task in results), results
    return sum(task["result"]["count"] for task in results)
