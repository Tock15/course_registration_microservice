"""Unit tests for the gateway token-bucket rate limiter."""
import pytest

from api_gateway.rate_limiter import RateLimitExceeded, TokenBucketRateLimiter
from tests.gateway_support import FakeClock


def test_burst_up_to_capacity_then_rejects():
    limiter = TokenBucketRateLimiter(3, 1.0, clock=FakeClock())

    for _ in range(3):
        limiter.check("student:STU_001")

    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check("student:STU_001")
    assert exc.value.retry_after_seconds == 1


def test_tokens_refill_over_time():
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(2, 0.5, clock=clock)
    limiter.check("k")
    limiter.check("k")

    clock.advance(1.0)  # only half a token back
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check("k")
    assert exc.value.retry_after == pytest.approx(1.0)

    clock.advance(1.0)
    limiter.check("k")


def test_refill_never_exceeds_capacity():
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(2, 1.0, clock=clock)
    limiter.check("k")

    clock.advance(3600)
    limiter.check("k")
    limiter.check("k")
    with pytest.raises(RateLimitExceeded):
        limiter.check("k")


def test_callers_have_independent_buckets():
    limiter = TokenBucketRateLimiter(1, 1.0, clock=FakeClock())
    limiter.check("student:STU_001")
    limiter.check("student:STU_002")

    with pytest.raises(RateLimitExceeded):
        limiter.check("student:STU_001")


def test_idle_buckets_are_evicted():
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(2, 1.0, clock=clock)
    for i in range(50):
        limiter.check(f"student:STU_{i:03d}")
    assert len(limiter) == 50

    clock.advance(10)
    limiter.check("student:STU_NEW")
    assert len(limiter) == 1


@pytest.mark.parametrize(("capacity", "refill"), [(0, 1.0), (5, 0.0), (5, -1.0)])
def test_rejects_invalid_settings(capacity, refill):
    with pytest.raises(ValueError):
        TokenBucketRateLimiter(capacity, refill)
