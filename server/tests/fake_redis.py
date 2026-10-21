"""A Redis double for the room directory and the Redis room store.

Implements the slice of redis-py (with decode_responses=True) those two use,
with Redis's semantics for it: keys expiring at their PX deadline, sorted-set
score ranges with exclusive "(" bounds, hashes, MULTI/EXEC pipelines applied
all-or-nothing, and the directory's Lua scripts, replayed here in Python.
Time comes from `clock` (seconds) so tests can step it.

test_rate_limits.py keeps its own smaller double for the limiter.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Optional

try:
    from server.directory import CLAIM_SCRIPT, RELEASE_SCRIPT, RENEW_SCRIPT, TAKE_OVER_SCRIPT
except ImportError:  # Render rootDir=server
    from directory import CLAIM_SCRIPT, RELEASE_SCRIPT, RENEW_SCRIPT, TAKE_OVER_SCRIPT


class RedisDown(ConnectionError):
    """What FakeRedis raises while `down` is set."""


class Clock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


COMMANDS = frozenset(
    {
        "get",
        "set",
        "delete",
        "pexpire",
        "pttl",
        "eval",
        "zadd",
        "zscore",
        "zcount",
        "zrem",
        "zrangebyscore",
        "zremrangebyscore",
        "hset",
        "hget",
        "hdel",
    }
)


def _bound(raw: Any, *, low: bool) -> tuple[float, bool]:
    """A ZRANGEBYSCORE bound as (value, inclusive)."""
    text = str(raw)
    if text in ("-inf", "+inf", "inf"):
        return (float("-inf") if text == "-inf" else float("inf")), True
    if text.startswith("("):
        return float(text[1:]), False
    return float(text), True


def _in_range(score: float, low: tuple[float, bool], high: tuple[float, bool]) -> bool:
    above = score >= low[0] if low[1] else score > low[0]
    below = score <= high[0] if high[1] else score < high[0]
    return above and below


class FakeRedis:
    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self.clock = clock or time.monotonic
        self.down = False
        # Round trips attempted, including ones that failed while down.
        self.calls = 0
        self.strings: dict[str, str] = {}
        self.expires_at: dict[str, float] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    def __getattr__(self, name: str) -> Any:
        if name in COMMANDS:
            def command(*args: Any, **kwargs: Any) -> Any:
                self._round_trip()
                return getattr(self, f"_{name}")(*args, **kwargs)

            return command
        raise AttributeError(name)

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        assert transaction, "room store writes must be atomic"
        return FakePipeline(self)

    # --- plumbing ---

    def _round_trip(self) -> None:
        self.calls += 1
        if self.down:
            raise RedisDown("Connection refused")
        now = self.clock()
        for name, deadline in list(self.expires_at.items()):
            if now >= deadline:
                self.strings.pop(name, None)
                del self.expires_at[name]

    # --- strings ---

    def _get(self, name: str) -> Optional[str]:
        return self.strings.get(name)

    def _set(
        self,
        name: str,
        value: Any,
        nx: bool = False,
        px: Optional[int] = None,
    ) -> Optional[bool]:
        if nx and name in self.strings:
            return None
        self.strings[name] = str(value)
        self.expires_at.pop(name, None)
        if px is not None:
            self.expires_at[name] = self.clock() + int(px) / 1000
        return True

    def _delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            removed += self.strings.pop(name, None) is not None
            self.expires_at.pop(name, None)
            removed += self.zsets.pop(name, None) is not None
            removed += self.hashes.pop(name, None) is not None
        return removed

    def _pexpire(self, name: str, ms: Any) -> bool:
        if name not in self.strings:
            return False
        self.expires_at[name] = self.clock() + int(ms) / 1000
        return True

    def _pttl(self, name: str) -> int:
        if name not in self.strings:
            return -2
        if name not in self.expires_at:
            return -1
        return round((self.expires_at[name] - self.clock()) * 1000)

    # --- the directory's scripts ---

    def _eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        keys = [str(key) for key in keys_and_args[:numkeys]]
        args = [str(arg) for arg in keys_and_args[numkeys:]]
        key = keys[0]
        owner = self.strings.get(key)
        if script == CLAIM_SCRIPT:
            me, ttl = args
            if owner is None:
                self._set(key, me, px=int(ttl))
                return me
            if owner == me:
                self._pexpire(key, ttl)
            return owner
        if script == RENEW_SCRIPT:
            me, ttl = args
            if owner == me:
                self._pexpire(key, ttl)
                return 1
            if owner is None:
                self._set(key, me, px=int(ttl))
                return 1
            return 0
        if script == TAKE_OVER_SCRIPT:
            stale, me, ttl = args
            if owner is None or owner in (stale, me):
                self._set(key, me, px=int(ttl))
                return 1
            return 0
        if script == RELEASE_SCRIPT:
            (me,) = args
            if owner == me:
                return self._delete(key)
            return 0
        raise AssertionError(f"FakeRedis does not know this script:\n{script}")

    # --- sorted sets ---

    def _zadd(self, name: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(name, {})
        added = sum(1 for member in mapping if member not in zset)
        zset.update({member: float(score) for member, score in mapping.items()})
        return added

    def _zscore(self, name: str, member: str) -> Optional[float]:
        return self.zsets.get(name, {}).get(member)

    def _zcount(self, name: str, low: Any, high: Any) -> int:
        lo, hi = _bound(low, low=True), _bound(high, low=False)
        return sum(1 for score in self.zsets.get(name, {}).values() if _in_range(score, lo, hi))

    def _zrem(self, name: str, *members: str) -> int:
        zset = self.zsets.get(name, {})
        return sum(1 for member in members if zset.pop(member, None) is not None)

    def _zrangebyscore(self, name: str, low: Any, high: Any) -> list[str]:
        lo, hi = _bound(low, low=True), _bound(high, low=False)
        items = sorted(self.zsets.get(name, {}).items(), key=lambda item: (item[1], item[0]))
        return [member for member, score in items if _in_range(score, lo, hi)]

    def _zremrangebyscore(self, name: str, low: Any, high: Any) -> int:
        doomed = self._zrangebyscore(name, low, high)
        return self._zrem(name, *doomed)

    # --- hashes ---

    def _hset(self, name: str, key: str, value: Any) -> int:
        bucket = self.hashes.setdefault(name, {})
        added = int(key not in bucket)
        bucket[key] = str(value)
        return added

    def _hget(self, name: str, key: str) -> Optional[str]:
        return self.hashes.get(name, {}).get(key)

    def _hdel(self, name: str, *keys: str) -> int:
        bucket = self.hashes.get(name, {})
        return sum(1 for key in keys if bucket.pop(key, None) is not None)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name in COMMANDS:
            def queue(*args: Any, **kwargs: Any) -> FakePipeline:
                self._queued.append((name, args, kwargs))
                return self

            return queue
        raise AttributeError(name)

    def execute(self) -> list[Any]:
        # One round trip, applied all-or-nothing like MULTI/EXEC.
        self._redis._round_trip()
        results = [getattr(self._redis, f"_{name}")(*args, **kwargs) for name, args, kwargs in self._queued]
        self._queued.clear()
        return results
