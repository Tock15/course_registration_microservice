"""
api_gateway/rate_limiter.py
Token-bucket rate limiter: the anti-spam access check of the Protection Proxy.
"""
import math
import time
from collections.abc import Callable


class RateLimitExceeded(Exception):
    def __init__(self, key: str, retry_after: float) -> None:
        self.key = key
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for {key}; retry in {retry_after:.2f}s")

    @property
    def retry_after_seconds(self) -> int:
        """Whole seconds for the Retry-After header (at least 1)."""
        return max(1, math.ceil(self.retry_after))


class TokenBucket:
    """Holds up to `capacity` tokens, refilled continuously at `refill_per_second`."""

    __slots__ = ("capacity", "refill_per_second", "tokens", "updated_at")

    def __init__(self, capacity: int, refill_per_second: float, now: float) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.tokens = float(capacity)
        self.updated_at = now

    def try_consume(self, now: float) -> bool:
        """Refill for the elapsed time, then take one token if available."""
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        self.updated_at = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def seconds_until_token(self) -> float:
        return max(0.0, (1.0 - self.tokens) / self.refill_per_second)


class TokenBucketRateLimiter:
    """One bucket per caller key (e.g. `student:STU_001`, `ip:10.0.0.7`).

    `check` never awaits, so it runs atomically on the event loop and needs no lock.
    """

    def __init__(
        self,
        capacity: int,
        refill_per_second: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if refill_per_second <= 0:
            raise ValueError("refill_per_second must be positive")
        self._capacity = capacity
        self._refill_per_second = refill_per_second
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        # A bucket untouched for this long has refilled completely.
        self._idle_seconds = capacity / refill_per_second
        self._last_sweep = clock()

    def check(self, key: str) -> None:
        """Spend one token for `key`, or raise RateLimitExceeded."""
        now = self._clock()
        self._evict_idle(now)

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(self._capacity, self._refill_per_second, now)
            self._buckets[key] = bucket

        if not bucket.try_consume(now):
            raise RateLimitExceeded(key, bucket.seconds_until_token())

    def _evict_idle(self, now: float) -> None:
        """Drop buckets that are full again, so memory does not grow per caller."""
        if now - self._last_sweep < self._idle_seconds:
            return
        self._last_sweep = now
        self._buckets = {
            key: bucket
            for key, bucket in self._buckets.items()
            if now - bucket.updated_at < self._idle_seconds
        }

    def __len__(self) -> int:
        return len(self._buckets)
