"""One process-wide AWS budget; writes also have conservative resource locks."""
from contextlib import ExitStack
from threading import BoundedSemaphore, Lock
import weakref

_all = BoundedSemaphore(5)
_writes = BoundedSemaphore(2)
_guard = Lock()
_resources = weakref.WeakValueDictionary()


def is_mutation(operation):
    return not operation.startswith(("Get", "List", "Describe", "Head"))


def call_with_limits(fn, operation, parameters, region="", before_call=None):
    mutation = is_mutation(operation)
    resources = parameters.get("instanceNames") or [parameters.get("instanceName") or parameters.get("staticIpName") or "account-write"]
    locks = []
    if mutation:
        with _guard:
            for name in sorted(set(resources)):
                # Account is not available at this transport layer; serializing
                # same-named resources across accounts is deliberately conservative.
                key = (region, str(name))
                lock = _resources.get(key)
                if lock is None:
                    lock = Lock()
                    _resources[key] = lock
                locks.append(lock)
    with ExitStack() as stack:
        for lock in locks:
            stack.enter_context(lock)
        if mutation:
            stack.enter_context(_writes)
        stack.enter_context(_all)
        if before_call is not None:
            before_call()
        return fn(operation, parameters)


def limit_client(client, region, before_call=None):
    if getattr(client._make_api_call, "_lightsail_limited", False) is True:
        return client
    original = client._make_api_call
    def bounded(operation, parameters):
        return call_with_limits(original, operation, parameters, region, before_call)
    bounded._lightsail_limited = True
    client._make_api_call = bounded
    return client
