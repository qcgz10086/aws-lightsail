"""登录/改密失败限流（R-2）。内存实现，后端单 worker 适用。

规则：同一 IP+用户名，15 分钟滑动窗口内累计失败 5 次后锁定 15 分钟；
锁定期间请求返回 429 并带 Retry-After（剩余锁定秒数）；成功后清零。
锁定期间的失败不再延长锁定；锁定期满后重新开始计数。
"""

import threading
import time
from collections import deque

MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60


class LoginRateLimiter:
    """按 key（IP+用户名）计数的滑动窗口限流器。"""

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES,
        window_seconds: int = WINDOW_SECONDS,
        lock_seconds: int = LOCK_SECONDS,
    ) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lock_seconds = lock_seconds
        self._lock = threading.Lock()
        self._entries: dict[str, dict] = {}

    def _prune(self, entry: dict, now: float) -> None:
        while entry["failures"] and entry["failures"][0] <= now - self.window_seconds:
            entry["failures"].popleft()

    def check_locked(self, key: str) -> int | None:
        """被锁定时返回剩余锁定秒数（向上取整），否则返回 None。"""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            locked_until = entry["locked_until"]
            if locked_until is not None:
                if now < locked_until:
                    return max(1, int(locked_until - now + 0.999))
                entry["locked_until"] = None
                entry["failures"].clear()
            self._prune(entry, now)
            return None

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.setdefault(
                key, {"failures": deque(), "locked_until": None}
            )
            if entry["locked_until"] is not None and now < entry["locked_until"]:
                return
            self._prune(entry, now)
            entry["failures"].append(now)
            if len(entry["failures"]) >= self.max_failures:
                entry["locked_until"] = now + self.lock_seconds
                entry["failures"].clear()

    def record_success(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def reset(self) -> None:
        """清空全部计数（仅测试使用）。"""
        with self._lock:
            self._entries.clear()


login_limiter = LoginRateLimiter()


def rate_limit_key(client_ip: str, username: str) -> str:
    return f"{client_ip or 'unknown'}|{username}"
