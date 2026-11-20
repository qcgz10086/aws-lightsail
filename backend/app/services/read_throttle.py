"""Share AWS network slots and pace read attempts in account order."""
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Condition
from time import monotonic
from typing import Callable

from app.services.query_settings import query_interval_seconds


def read_scope(user_id, account_id, credential_id):
    if account_id and len(account_id) == 12 and account_id.isascii() and account_id.isdigit():
        return (user_id, "account", account_id)
    return (user_id, "credential", credential_id)


@dataclass(frozen=True)
class ReadContext:
    scope: tuple
    checks: tuple[Callable[[], None], ...] = ()


_current = ContextVar("aws_read_context", default=ReadContext(("unscoped",)))


@contextmanager
def read_context(scope=None, check=None):
    previous = _current.get()
    current = ReadContext(scope if scope is not None else previous.scope,
                          (*previous.checks, check) if check is not None else previous.checks)
    token = _current.set(current)
    try:
        yield
    finally:
        _current.reset(token)


class ReadThrottle:
    def __init__(self, interval=query_interval_seconds, clock=monotonic):
        self.interval = interval
        self.clock = clock
        self.last_sent = None
        self.last_scope = None
        self.turns = {}
        self.condition = Condition()

    def settings_changed(self):
        with self.condition:
            self.condition.notify_all()

    def _next_scope(self):
        return next((scope for scope in self.turns if scope != self.last_scope), self.last_scope)

    def send(self, slots, send, request):
        context = _current.get()
        ticket = object()
        with self.condition:
            queue = self.turns.setdefault(context.scope, deque())
            queue.append(ticket)
            granted = False
            try:
                while True:
                    if context.scope == self._next_scope() and queue[0] is ticket:
                        interval = self.interval()
                        delay = 0 if self.last_sent is None else self.last_sent + interval - self.clock()
                        if delay <= 0 and slots.acquire(blocking=False):
                            try:
                                for check in context.checks:
                                    check()
                            except BaseException:
                                slots.release()
                                raise
                            self.last_sent = self.clock()
                            self.last_scope = context.scope
                            granted = True
                            break
                        self.condition.wait(min(delay, .25) if delay > 0 else .25)
                    else:
                        self.condition.wait()
            finally:
                queue.remove(ticket)
                if not queue or granted:
                    del self.turns[context.scope]
                    if queue:
                        self.turns[context.scope] = queue
                self.condition.notify_all()
        try:
            return send(request)
        finally:
            slots.release()
            self.settings_changed()


read_throttle = ReadThrottle()
