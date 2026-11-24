"""
Distributed rate limiting (PRD API-012, SEC-017).

A token bucket per key, stored in Redis so every replica shares one limit. A request takes one token; tokens refill
at ``refill_per_second`` up to ``capacity``. When the bucket is empty the request is refused with the number of seconds
until the next token (``Retry-After``).

The read-modify-write is optimistic (WATCH / MULTI). If another replica changed the bucket in between, the update is
retried, so two replicas can never both spend the same last token. No Lua, so the same code runs on fakeredis and on
real Redis.

Keys are hashed, so a credential or tenant identifier never appears in Redis key names (SEC-012).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from redis.exceptions import RedisError, WatchError

from doc_extractor.exceptions import StorageError

DEFAULT_NAMESPACE = "doc_extractor"
_MAX_ATTEMPTS = 20


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    remaining: float
    retry_after_seconds: int


class RedisTokenBucket:
    def __init__(
        self,
        client: Any,
        *,
        capacity: float,
        refill_per_second: float,
        namespace: str = DEFAULT_NAMESPACE,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("Rate-limit capacity and refill rate must be positive.")
        self._client = client
        self._capacity = float(capacity)
        self._refill = float(refill_per_second)
        self._namespace = namespace
        self._clock = clock

    def _key(self, subject: str) -> str:
        return f"{self._namespace}:rl:{hashlib.sha256(subject.encode('utf-8')).hexdigest()}"

    def take(self, subject: str, tokens: float = 1.0) -> RateDecision:
        """Spend ``tokens`` for ``subject``. Returns whether the request may proceed."""
        if not subject:
            raise ValueError("A rate-limit subject is required.")
        key = self._key(subject)
        ttl_ms = int(math.ceil(self._capacity / self._refill * 1000)) + 1000
        try:
            for _ in range(_MAX_ATTEMPTS):
                with self._client.pipeline() as pipe:
                    try:
                        pipe.watch(key)
                        stored = pipe.get(key)
                        now = self._clock()
                        level, stamp = self._load(stored, now)
                        level = min(self._capacity, level + (now - stamp) * self._refill)
                        if level >= tokens:
                            level -= tokens
                            allowed = True
                            retry_after = 0
                        else:
                            allowed = False
                            retry_after = int(math.ceil((tokens - level) / self._refill))
                        pipe.multi()
                        pipe.set(key, json.dumps({"level": level, "stamp": now}), px=ttl_ms)
                        pipe.execute()
                        return RateDecision(allowed=allowed, remaining=level, retry_after_seconds=retry_after)
                    except WatchError:
                        continue  # another replica changed the bucket: re-read and decide again
        except RedisError as exc:
            # Fail closed: an unreachable limiter must not turn into unlimited traffic.
            raise StorageError(message="Rate-limit store is unavailable.", details={"error_type": type(exc).__name__}) from exc
        raise StorageError(message="Rate-limit store is contended.", details={"attempts": _MAX_ATTEMPTS})

    def _load(self, stored: Optional[bytes], now: float) -> tuple:
        if stored is None:
            return self._capacity, now
        try:
            data = json.loads(stored)
            return float(data["level"]), float(data["stamp"])
        except (ValueError, KeyError, TypeError):
            return self._capacity, now  # a corrupt bucket resets to full, never to empty
