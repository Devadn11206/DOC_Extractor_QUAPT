"""
Tests for webhook retry scheduling and the delivery attempt contract (PRD v1.5 section 7.1), Phase 4, PROVISIONAL.

Verified: the doubling back-off and its cap; the 24-hour window, its inclusive boundary and the explicit expiry; the full retry
sequence against an independent integer-microsecond model; that there is no default interval and no maximum attempt count;
invalid attempt numbers, delays and instants; determinism with caller-supplied time and no hidden clock; and the validated
``DeliveryAttempt`` record (consistent outcomes, no payload, URL, secret or free text). Synthetic data only.
"""

import inspect
import time
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from doc_extractor.api.contracts.webhook_delivery import DeliveryAttempt, DeliveryOutcome
from doc_extractor.api.services import webhook_retry as module
from doc_extractor.api.services.webhook_retry import (
    RETRY_WINDOW,
    RetryDecision,
    RetryOutcome,
    RetryScheduleError,
    backoff_delay,
    schedule_retry,
)

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SECOND, MINUTE, HOUR = timedelta(seconds=1), timedelta(minutes=1), timedelta(hours=1)
HOSTILE = "SYNTH-SECRET-VALUE-731"


def _schedule(attempt=1, first=T0, failed=T0, base=MINUTE, cap=HOUR):
    return schedule_retry(attempt_number=attempt, first_attempt_at=first, failed_at=failed, base_delay=base, max_delay=cap)


# ============================================================================
# The delay
# ============================================================================

def test_the_delay_doubles_from_the_base_and_stops_at_the_maximum():
    delays = [backoff_delay(attempt_number=n, base_delay=MINUTE, max_delay=HOUR) for n in range(1, 11)]
    assert delays == [MINUTE * f for f in (1, 2, 4, 8, 16, 32)] + [HOUR] * 4


def test_the_delay_is_the_exact_cap_at_and_around_the_boundary():
    assert backoff_delay(attempt_number=3, base_delay=SECOND, max_delay=timedelta(seconds=4)) == timedelta(seconds=4)      # 4 == cap
    assert backoff_delay(attempt_number=3, base_delay=SECOND, max_delay=timedelta(seconds=3)) == timedelta(seconds=3)      # 4 > cap
    assert backoff_delay(attempt_number=3, base_delay=SECOND, max_delay=timedelta(seconds=5)) == timedelta(seconds=4)      # 4 < cap
    assert backoff_delay(attempt_number=1, base_delay=HOUR, max_delay=HOUR) == HOUR                                         # base == cap
    assert backoff_delay(attempt_number=50, base_delay=HOUR, max_delay=HOUR) == HOUR


def test_a_huge_attempt_number_is_fast_and_capped():
    started = time.perf_counter()
    for attempt in (10**6, 10**18, 2**64, 2**1000):
        assert backoff_delay(attempt_number=attempt, base_delay=timedelta(microseconds=1), max_delay=timedelta.max) == timedelta.max
        assert backoff_delay(attempt_number=attempt, base_delay=SECOND, max_delay=HOUR) == HOUR
    assert time.perf_counter() - started < 1.0


def test_the_delay_cannot_overflow():
    big = timedelta(days=500_000_000)
    assert backoff_delay(attempt_number=2, base_delay=big, max_delay=timedelta.max) == timedelta.max       # 2 * big overflows: the cap
    assert backoff_delay(attempt_number=1, base_delay=big, max_delay=timedelta.max) == big
    assert backoff_delay(attempt_number=3, base_delay=timedelta.max, max_delay=timedelta.max) == timedelta.max


def test_the_delay_is_independent_of_the_time():
    assert backoff_delay(attempt_number=4, base_delay=MINUTE, max_delay=HOUR) == MINUTE * 8


# ============================================================================
# The window
# ============================================================================

def test_a_retry_inside_the_window_is_scheduled_after_the_delay():
    decision = _schedule(attempt=3, failed=T0 + timedelta(minutes=10))
    assert decision.outcome is RetryOutcome.SCHEDULED
    assert decision.delay == MINUTE * 4 and decision.next_attempt_at == T0 + timedelta(minutes=14)
    assert decision.window_ends_at == T0 + RETRY_WINDOW and decision.attempt_outcome is DeliveryOutcome.FAILED_RETRYABLE


def test_the_retry_window_is_the_prds_24_hours():
    assert RETRY_WINDOW == timedelta(hours=24)


def test_a_retry_exactly_at_the_end_of_the_window_is_still_scheduled():
    end = T0 + RETRY_WINDOW
    decision = _schedule(attempt=1, failed=end - MINUTE, base=MINUTE)
    assert decision.outcome is RetryOutcome.SCHEDULED and decision.next_attempt_at == end


def test_a_retry_one_microsecond_after_the_window_is_expired():
    end = T0 + RETRY_WINDOW
    decision = _schedule(attempt=1, failed=end - MINUTE + timedelta(microseconds=1), base=MINUTE)
    assert decision.outcome is RetryOutcome.WINDOW_EXPIRED
    assert decision.next_attempt_at is None and decision.delay is None and decision.window_ends_at == end
    assert decision.attempt_outcome is DeliveryOutcome.EXPIRED


def test_a_failure_after_the_window_is_expired_whatever_the_delay():
    end = T0 + RETRY_WINDOW
    for failed in (end + timedelta(microseconds=1), end + MINUTE, end + timedelta(days=30)):
        decision = _schedule(attempt=1, failed=failed, base=timedelta(microseconds=1), cap=timedelta(microseconds=1))
        assert decision.outcome is RetryOutcome.WINDOW_EXPIRED and decision.next_attempt_at is None


def test_a_failure_exactly_at_the_end_of_the_window_can_only_expire():
    end = T0 + RETRY_WINDOW
    assert _schedule(failed=end).outcome is RetryOutcome.WINDOW_EXPIRED            # any positive delay passes the end


def test_a_delay_longer_than_the_window_expires_on_the_first_failure():
    decision = _schedule(attempt=1, failed=T0, base=timedelta(hours=25), cap=timedelta(hours=48))
    assert decision.outcome is RetryOutcome.WINDOW_EXPIRED


def test_the_window_is_anchored_at_the_first_attempt_not_at_the_failure():
    first = T0
    late_failure = T0 + timedelta(hours=23, minutes=59)
    assert _schedule(first=first, failed=late_failure, base=timedelta(seconds=30), cap=timedelta(seconds=30)).outcome is RetryOutcome.SCHEDULED
    assert _schedule(first=first, failed=late_failure, base=timedelta(minutes=2), cap=timedelta(minutes=2)).outcome is RetryOutcome.WINDOW_EXPIRED
    assert _schedule(first=first, failed=late_failure).window_ends_at == T0 + RETRY_WINDOW


def _model_sequence(base_us: int, cap_us: int, attempt_us: int):
    """An independent model in integer microseconds: the instants of every attempt until the 24-hour window ends."""
    window, t, attempt, instants = 24 * 3600 * 10**6, 0, 1, [0]
    while True:
        failed = t + attempt_us
        delay = min(cap_us, base_us * 2 ** (attempt - 1))
        if failed > window or failed + delay > window:
            return instants, attempt
        t = failed + delay
        instants.append(t)
        attempt += 1


@pytest.mark.parametrize("base, cap, took", [
    (MINUTE, HOUR, timedelta(0)), (SECOND, timedelta(minutes=30), timedelta(seconds=2)), (timedelta(seconds=30), HOUR * 5, timedelta(seconds=7)),
    (HOUR, HOUR, timedelta(minutes=1)), (timedelta(milliseconds=500), timedelta(minutes=7), timedelta(milliseconds=250)),
])
def test_the_whole_retry_sequence_matches_an_independent_model_and_ends_inside_24_hours(base, cap, took):
    to_us = lambda d: d // timedelta(microseconds=1)
    expected, expected_attempts = _model_sequence(to_us(base), to_us(cap), to_us(took))
    attempt, failed, actual = 1, T0 + took, [T0]
    while True:
        decision = _schedule(attempt=attempt, first=T0, failed=failed, base=base, cap=cap)
        if decision.outcome is RetryOutcome.WINDOW_EXPIRED:
            break
        assert T0 < decision.next_attempt_at <= T0 + RETRY_WINDOW
        actual.append(decision.next_attempt_at)
        attempt, failed = attempt + 1, decision.next_attempt_at + took
    assert [(moment - T0) // timedelta(microseconds=1) for moment in actual] == expected and attempt == expected_attempts


def test_there_is_no_maximum_attempt_count_only_the_window_ends_the_retries():
    """One-second retries for 24 hours: 86,400 retries are scheduled, then the window (not a count) stops them."""
    one = timedelta(seconds=1)
    attempt, failed, scheduled = 1, T0, 0
    while True:
        decision = _schedule(attempt=attempt, failed=failed, base=one, cap=one)
        if decision.outcome is RetryOutcome.WINDOW_EXPIRED:
            break
        scheduled += 1
        attempt, failed = attempt + 1, decision.next_attempt_at
    assert scheduled == 86_400 and failed == T0 + RETRY_WINDOW


def test_nothing_about_the_intervals_or_the_attempt_count_is_defaulted_or_invented():
    for function in (schedule_retry, backoff_delay):
        for name in ("base_delay", "max_delay"):
            assert inspect.signature(function).parameters[name].default is inspect.Parameter.empty
    for name in dir(module):
        assert not any(word in name.upper() for word in ("MAX_ATTEMPT", "MAX_RETRIES", "ATTEMPT_LIMIT", "DEFAULT_BASE", "DEFAULT_DELAY", "DEFAULT_MAX"))
    with pytest.raises(TypeError):
        schedule_retry(attempt_number=1, first_attempt_at=T0, failed_at=T0)                    # type: ignore[call-arg]
    with pytest.raises(TypeError):
        backoff_delay(attempt_number=1)                                                         # type: ignore[call-arg]


# ============================================================================
# Invalid input
# ============================================================================

@pytest.mark.parametrize("bad", [0, -1, -(10**9), True, False, 1.0, 2.5, "1", None, [1], b"1", float("nan")])
def test_an_attempt_number_must_be_a_positive_integer(bad):
    for call in (lambda: _schedule(attempt=bad), lambda: backoff_delay(attempt_number=bad, base_delay=MINUTE, max_delay=HOUR)):
        with pytest.raises(RetryScheduleError) as caught:
            call()
        assert caught.value.code == "INVALID_ATTEMPT_NUMBER"


@pytest.mark.parametrize("base, cap", [
    (timedelta(0), HOUR), (-MINUTE, HOUR), (MINUTE, timedelta(0)), (HOUR, MINUTE), (MINUTE, MINUTE - timedelta(microseconds=1)),
    (60, HOUR), (MINUTE, 3600), (60.0, 3600.0), ("1m", "1h"), (None, HOUR), (MINUTE, None), (True, HOUR), (MINUTE, [HOUR]),
])
def test_the_delays_must_be_positive_with_the_maximum_not_below_the_base(base, cap):
    for call in (lambda: _schedule(base=base, cap=cap), lambda: backoff_delay(attempt_number=1, base_delay=base, max_delay=cap)):
        with pytest.raises(RetryScheduleError) as caught:
            call()
        assert caught.value.code == "INVALID_DELAY"


@pytest.mark.parametrize("bad", [None, "2026-03-01T12:00:00Z", 1_700_000_000, 1.5, datetime(2026, 3, 1, 12, 0), T0.date(), object(), T0.timestamp()])
def test_every_instant_must_be_a_timezone_aware_datetime(bad):
    for call in (lambda: _schedule(first=bad), lambda: _schedule(failed=bad)):
        with pytest.raises(RetryScheduleError) as caught:
            call()
        assert caught.value.code == "INVALID_TIME"


def test_a_failure_before_the_first_attempt_is_refused():
    with pytest.raises(RetryScheduleError) as caught:
        _schedule(first=T0, failed=T0 - timedelta(microseconds=1))
    assert caught.value.code == "INVALID_TIME"
    assert _schedule(first=T0, failed=T0).outcome is RetryOutcome.SCHEDULED                 # equal is fine


def test_instants_at_the_edge_of_the_representable_range_are_handled_safely():
    last_day = datetime(9999, 12, 31, 12, tzinfo=timezone.utc)
    with pytest.raises(RetryScheduleError) as caught:                                      # the window end is unrepresentable
        _schedule(first=last_day, failed=last_day)
    assert caught.value.code == "INVALID_TIME"
    near = datetime(9999, 12, 30, 12, tzinfo=timezone.utc)
    decision = _schedule(first=near, failed=near, base=timedelta(days=3), cap=timedelta(days=3))     # failed + delay overflows
    assert decision.outcome is RetryOutcome.WINDOW_EXPIRED and decision.next_attempt_at is None
    early = datetime(1, 1, 1, 12, tzinfo=timezone.utc)
    assert _schedule(first=early, failed=early).outcome is RetryOutcome.SCHEDULED


def test_scheduling_errors_are_fixed_and_never_echo_the_input():
    for call in (lambda: _schedule(attempt=HOSTILE), lambda: _schedule(first=HOSTILE), lambda: _schedule(base=HOSTILE)):
        with pytest.raises(RetryScheduleError) as caught:
            call()
        error = caught.value
        assert HOSTILE not in str(error) and HOSTILE not in repr(error)
    assert RetryScheduleError("anything").code == "INVALID_TIME"
    for code, message in RetryScheduleError._MESSAGES.items():
        assert str(RetryScheduleError(code)) == message


# ============================================================================
# Determinism, no hidden clock
# ============================================================================

def test_the_decision_depends_only_on_its_arguments():
    first = _schedule(attempt=5, failed=T0 + timedelta(hours=2))
    assert all(_schedule(attempt=5, failed=T0 + timedelta(hours=2)) == first for _ in range(20))
    assert isinstance(first, RetryDecision) and first == RetryDecision(first.outcome, first.window_ends_at, first.next_attempt_at, first.delay)


def test_an_equal_instant_in_another_offset_gives_the_same_decision():
    india = timezone(timedelta(hours=5, minutes=30))
    utc = _schedule(attempt=2, first=T0, failed=T0 + MINUTE)
    local = _schedule(attempt=2, first=T0.astimezone(india), failed=(T0 + MINUTE).astimezone(india))
    assert local.outcome is utc.outcome and local.delay == utc.delay
    assert local.next_attempt_at == utc.next_attempt_at and local.window_ends_at == utc.window_ends_at


def test_no_clock_or_randomness_is_read(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a clock, sleep or random source was used")

    import random

    for target, names in ((time, ("time", "monotonic", "perf_counter", "sleep", "time_ns")), (random, ("random", "uniform", "randint", "choice"))):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    first = _schedule(attempt=3, failed=T0 + HOUR)
    assert first == _schedule(attempt=3, failed=T0 + HOUR) and first.outcome is RetryOutcome.SCHEDULED
    assert backoff_delay(attempt_number=3, base_delay=MINUTE, max_delay=HOUR) == MINUTE * 4


def test_the_decision_is_frozen():
    decision = _schedule()
    with pytest.raises(Exception):
        decision.outcome = RetryOutcome.WINDOW_EXPIRED                                      # type: ignore[misc]


# ============================================================================
# The delivery attempt contract
# ============================================================================

def _attempt(**overrides):
    values = dict(
        subscription_id="sub_1", event_id="evt_00000000000000000000000000000001", event_type="document.completed", attempt_number=1,
        outcome=DeliveryOutcome.DELIVERED, attempted_at=T0, http_status=200,
    )
    values.update(overrides)
    return DeliveryAttempt(**values)


def test_an_attempt_records_each_outcome_consistently():
    assert _attempt().outcome is DeliveryOutcome.DELIVERED
    retry = _attempt(outcome=DeliveryOutcome.FAILED_RETRYABLE, http_status=503, error_code="HTTP_5XX", next_attempt_at=T0 + MINUTE)
    assert retry.next_attempt_at == T0 + MINUTE and retry.error_code == "HTTP_5XX"
    assert _attempt(outcome=DeliveryOutcome.FAILED_PERMANENT, http_status=410, error_code="HTTP_4XX").next_attempt_at is None
    assert _attempt(outcome=DeliveryOutcome.EXPIRED, http_status=None, error_code="RETRY_WINDOW_EXPIRED", attempt_number=40).next_attempt_at is None
    assert _attempt(outcome=DeliveryOutcome.FAILED_PERMANENT, http_status=None, error_code="DESTINATION_REFUSED").http_status is None


def test_the_outcomes_are_the_four_documented_ones():
    assert {member.value for member in DeliveryOutcome} == {"delivered", "failed_retryable", "failed_permanent", "expired"}


@pytest.mark.parametrize("overrides", [
    {"outcome": DeliveryOutcome.FAILED_RETRYABLE},                                                         # no next attempt
    {"outcome": DeliveryOutcome.FAILED_RETRYABLE, "next_attempt_at": T0},                                  # not later than the attempt
    {"outcome": DeliveryOutcome.FAILED_RETRYABLE, "next_attempt_at": T0 - MINUTE},
    {"outcome": DeliveryOutcome.DELIVERED, "next_attempt_at": T0 + MINUTE},
    {"outcome": DeliveryOutcome.FAILED_PERMANENT, "next_attempt_at": T0 + MINUTE},
    {"outcome": DeliveryOutcome.EXPIRED, "next_attempt_at": T0 + MINUTE},
    {"error_code": "HTTP_5XX"},                                                                            # a delivered event has no error
])
def test_an_inconsistent_attempt_is_refused(overrides):
    with pytest.raises(ValidationError):
        _attempt(**overrides)


@pytest.mark.parametrize("overrides", [
    {"attempt_number": 0}, {"attempt_number": -1}, {"attempt_number": True}, {"attempt_number": 1.0}, {"attempt_number": "1"},
    {"http_status": 99}, {"http_status": 600}, {"http_status": True}, {"http_status": 200.0}, {"http_status": "200"},
    *({"outcome": DeliveryOutcome.FAILED_PERMANENT, "error_code": bad}                                       # a non-delivered outcome, so that
      for bad in ("free text with spaces", "lower", f"ERROR {HOSTILE}", 5, "X", "A" * 65, "ERROR\n", "ÉRROR", b"HTTP_5XX")),   # the CODE is what is refused
    {"attempted_at": datetime(2026, 3, 1)}, {"attempted_at": "2026-03-01T12:00:00Z"}, {"attempted_at": 1_700_000_000}, {"attempted_at": None},
    {"event_type": "document.unknown"}, {"event_type": "document.completed "}, {"event_id": "bad"}, {"event_id": b"evt_00000001"},
    {"subscription_id": ""}, {"subscription_id": "sub 1"}, {"subscription_id": "s" * 129}, {"subscription_id": b"sub"},
    {"outcome": "delivered "}, {"outcome": "ok"}, {"next_attempt_at": datetime(2026, 3, 1, 12, 1)},
])
def test_a_malformed_attempt_is_refused(overrides):
    with pytest.raises(ValidationError):
        _attempt(**overrides)


def test_a_valid_error_code_is_a_fixed_token_on_a_failed_attempt():
    for code in ("HTTP_5XX", "TIMEOUT", "DESTINATION_REFUSED", "AB", "A" * 64):
        assert _attempt(outcome=DeliveryOutcome.FAILED_PERMANENT, error_code=code).error_code == code


def test_an_attempt_holds_only_the_fields_of_the_prds_delivery_log_and_never_content():
    names = set(DeliveryAttempt.model_fields)
    assert names == {"subscription_id", "event_id", "event_type", "attempt_number", "outcome", "attempted_at", "http_status",
                     "error_code", "next_attempt_at"}
    for forbidden in ("payload", "body", "url", "target_url", "secret", "signature", "headers", "response_body", "error_message", "message",
                      "tenant_id", "data", "values", "evidence"):
        assert forbidden not in names
        with pytest.raises(ValidationError):
            _attempt(**{forbidden: HOSTILE})
    assert HOSTILE not in repr(_attempt())


def test_an_attempt_is_frozen_and_its_times_are_utc():
    attempt = _attempt(attempted_at=T0.astimezone(timezone(timedelta(hours=5, minutes=30))))
    assert attempt.attempted_at == T0 and attempt.attempted_at.utcoffset() == timedelta(0)
    with pytest.raises(ValidationError):
        attempt.attempt_number = 2


def test_a_retry_decision_maps_onto_a_consistent_attempt_record():
    failed_at = T0 + timedelta(hours=1)
    scheduled = _schedule(attempt=2, failed=failed_at)
    retry = _attempt(attempt_number=2, outcome=scheduled.attempt_outcome, attempted_at=failed_at, http_status=503, error_code="HTTP_5XX",
                     next_attempt_at=scheduled.next_attempt_at)
    assert retry.outcome is DeliveryOutcome.FAILED_RETRYABLE and retry.next_attempt_at == failed_at + MINUTE * 2
    expired = _schedule(attempt=30, failed=T0 + RETRY_WINDOW + SECOND)
    stopped = _attempt(attempt_number=30, outcome=expired.attempt_outcome, attempted_at=T0 + RETRY_WINDOW + SECOND, http_status=None,
                       error_code="RETRY_WINDOW_EXPIRED")
    assert stopped.outcome is DeliveryOutcome.EXPIRED and stopped.next_attempt_at is None
    with pytest.raises(ValidationError):                                                    # an expired decision cannot carry a retry
        _attempt(outcome=expired.attempt_outcome, next_attempt_at=failed_at + MINUTE, attempted_at=failed_at)


# ============================================================================
# Hardening F1: every instant is converted to UTC, so the window is 24 REAL hours across a daylight-saving change
# ============================================================================

UTC = timezone.utc


@pytest.fixture
def new_york():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:                                       # pragma: no cover - environment without a tz database
        pytest.skip("no time-zone database in this environment")


def _local_wall_difference(later_utc, earlier_local):
    """The wall-clock difference (what naive local arithmetic would call the elapsed time)."""
    return later_utc.astimezone(earlier_local.tzinfo).replace(tzinfo=None) - earlier_local.replace(tzinfo=None)


def test_the_window_is_24_real_hours_across_the_spring_forward_change(new_york):
    """2026-03-08 02:00 -> 03:00 in New York: that local day has 23 hours, so 24 REAL hours end at 13:00 local, not 12:00."""
    first = datetime(2026, 3, 7, 12, 0, tzinfo=new_york)
    first_utc = first.astimezone(UTC)
    end_utc = first_utc + timedelta(hours=24)
    assert first_utc == datetime(2026, 3, 7, 17, 0, tzinfo=UTC) and end_utc == datetime(2026, 3, 8, 17, 0, tzinfo=UTC)

    inside = _schedule(first=first, failed=(end_utc - MINUTE).astimezone(new_york), base=MINUTE, cap=MINUTE)
    assert inside.outcome is RetryOutcome.SCHEDULED and inside.next_attempt_at == end_utc            # the inclusive boundary
    assert inside.window_ends_at == end_utc and inside.window_ends_at - first_utc == timedelta(hours=24)
    assert _local_wall_difference(inside.window_ends_at, first) == timedelta(hours=25)                 # the local clock shows 25 hours

    just_after = _schedule(first=first, failed=(end_utc - MINUTE + timedelta(microseconds=1)).astimezone(new_york), base=MINUTE, cap=MINUTE)
    assert just_after.outcome is RetryOutcome.WINDOW_EXPIRED and just_after.next_attempt_at is None


def test_a_failure_23_real_hours_after_the_first_attempt_is_still_inside_the_window_in_a_new_york_spring(new_york):
    """The defect: with wall-clock arithmetic this failure (23h10m real elapsed) was reported as expired."""
    first = datetime(2026, 3, 7, 12, 0, tzinfo=new_york)
    failed = datetime(2026, 3, 8, 12, 10, tzinfo=new_york)                                              # EDT by now
    assert failed.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=23, minutes=10)
    decision = _schedule(first=first, failed=failed, base=SECOND, cap=SECOND)
    assert decision.outcome is RetryOutcome.SCHEDULED
    assert decision == _schedule(first=first.astimezone(UTC), failed=failed.astimezone(UTC), base=SECOND, cap=SECOND)


def test_the_window_is_24_real_hours_across_the_fall_back_change(new_york):
    """2026-11-01 02:00 -> 01:00 in New York: that local day has 25 hours, so 24 REAL hours end at 11:00 local, not 12:00."""
    first = datetime(2026, 10, 31, 12, 0, tzinfo=new_york)
    first_utc = first.astimezone(UTC)
    end_utc = first_utc + timedelta(hours=24)
    assert first_utc == datetime(2026, 10, 31, 16, 0, tzinfo=UTC) and end_utc == datetime(2026, 11, 1, 16, 0, tzinfo=UTC)

    inside = _schedule(first=first, failed=(end_utc - MINUTE).astimezone(new_york), base=MINUTE, cap=MINUTE)
    assert inside.outcome is RetryOutcome.SCHEDULED and inside.next_attempt_at == end_utc
    assert inside.window_ends_at - first_utc == timedelta(hours=24)
    assert _local_wall_difference(inside.window_ends_at, first) == timedelta(hours=23)                 # the local clock shows 23 hours

    just_after = _schedule(first=first, failed=(end_utc - MINUTE + timedelta(microseconds=1)).astimezone(new_york), base=MINUTE, cap=MINUTE)
    assert just_after.outcome is RetryOutcome.WINDOW_EXPIRED


def test_a_failure_24_real_hours_after_the_first_attempt_has_expired_in_a_new_york_fall(new_york):
    """The mirror defect: with wall-clock arithmetic this failure (24h30m real elapsed) was still inside the window."""
    first = datetime(2026, 10, 31, 12, 0, tzinfo=new_york)
    failed = datetime(2026, 11, 1, 11, 30, tzinfo=new_york)                                             # EST by now
    assert failed.astimezone(UTC) - first.astimezone(UTC) == timedelta(hours=24, minutes=30)
    assert _schedule(first=first, failed=failed, base=SECOND, cap=SECOND).outcome is RetryOutcome.WINDOW_EXPIRED
    earlier = datetime(2026, 11, 1, 10, 30, tzinfo=new_york)                                            # 23h30m real
    assert _schedule(first=first, failed=earlier, base=SECOND, cap=SECOND).outcome is RetryOutcome.SCHEDULED


def test_a_delay_is_real_elapsed_time_across_both_changes(new_york):
    spring = datetime(2026, 3, 8, 1, 30, tzinfo=new_york)                                               # EST, 06:30 UTC; the next hour is skipped
    decision = _schedule(first=spring, failed=spring, base=HOUR, cap=HOUR)
    assert decision.next_attempt_at == datetime(2026, 3, 8, 7, 30, tzinfo=UTC) and decision.delay == HOUR
    assert decision.next_attempt_at - spring.astimezone(UTC) == HOUR
    assert decision.next_attempt_at.astimezone(new_york).hour == 3                                      # 03:30 EDT: wall clocks jumped

    first_pass = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0)                                  # EDT, 05:30 UTC (the hour happens twice)
    second_pass = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)                                 # EST, 06:30 UTC
    assert second_pass.astimezone(UTC) - first_pass.astimezone(UTC) == HOUR
    one = _schedule(first=first_pass, failed=first_pass, base=HOUR, cap=HOUR)
    two = _schedule(first=first_pass, failed=second_pass, base=HOUR, cap=HOUR)
    assert one.next_attempt_at == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)                              # one real hour later
    assert two.next_attempt_at == datetime(2026, 11, 1, 7, 30, tzinfo=UTC)


def test_the_decision_does_not_depend_on_the_zone_the_instants_were_expressed_in(new_york):
    india = timezone(timedelta(hours=5, minutes=30))
    for first, failed in (
        (datetime(2026, 3, 7, 12, 0, tzinfo=new_york), datetime(2026, 3, 8, 9, 0, tzinfo=new_york)),
        (datetime(2026, 10, 31, 12, 0, tzinfo=new_york), datetime(2026, 11, 1, 9, 0, tzinfo=new_york)),
    ):
        expected = _schedule(attempt=4, first=first.astimezone(UTC), failed=failed.astimezone(UTC))
        for zone in (new_york, india, timezone(timedelta(hours=-11)), UTC):
            assert _schedule(attempt=4, first=first.astimezone(zone), failed=failed.astimezone(zone)) == expected
        mixed = _schedule(attempt=4, first=first.astimezone(india), failed=failed.astimezone(new_york))
        assert mixed == expected


def test_the_instants_of_a_decision_are_utc():
    india = timezone(timedelta(hours=5, minutes=30))
    decision = _schedule(first=T0.astimezone(india), failed=(T0 + MINUTE).astimezone(india))
    for moment in (decision.window_ends_at, decision.next_attempt_at):
        assert moment.tzinfo is UTC and moment.utcoffset() == timedelta(0)


def test_the_formula_and_the_inclusive_boundary_are_unchanged_by_the_normalization():
    assert [backoff_delay(attempt_number=n, base_delay=MINUTE, max_delay=HOUR) for n in range(1, 9)] == [MINUTE * f for f in (1, 2, 4, 8, 16, 32)] + [HOUR] * 2
    end = T0 + RETRY_WINDOW
    assert _schedule(failed=end - MINUTE).next_attempt_at == end and _schedule(failed=end).outcome is RetryOutcome.WINDOW_EXPIRED


EXTREME_OFFSET_INSTANTS = [
    datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=14))),                                     # before year 1 in UTC
    datetime(1, 1, 1, 13, 59, 59, 999999, tzinfo=timezone(timedelta(hours=14))),
    datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=23, minutes=59))),
    datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=timezone(timedelta(hours=-14))),                 # past year 9999 in UTC
    datetime(9999, 12, 31, 10, 0, tzinfo=timezone(timedelta(hours=-14))),
    datetime(9999, 12, 31, 0, 1, tzinfo=timezone(timedelta(hours=-23, minutes=-59))),                  # 00:01 + 23:59 = day 10000
]


@pytest.mark.parametrize("instant", EXTREME_OFFSET_INSTANTS)
def test_an_instant_that_utc_cannot_represent_is_invalid_time_never_a_raw_overflow(instant):
    for call in (lambda: _schedule(first=instant, failed=instant), lambda: _schedule(first=T0, failed=instant),
                 lambda: _schedule(first=instant, failed=T0)):
        with pytest.raises(RetryScheduleError) as caught:
            call()
        error = caught.value
        assert error.code == "INVALID_TIME" and not isinstance(error, OverflowError) and error.__cause__ is None
        assert str(error) == "A timezone-aware time that is not before the first attempt is required."


def test_the_representable_extremes_are_accepted():
    lowest = datetime(1, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=14)))                            # exactly 0001-01-01T00:00:00Z
    assert lowest.astimezone(UTC) == datetime(1, 1, 1, tzinfo=UTC)
    assert _schedule(first=lowest, failed=lowest).outcome is RetryOutcome.SCHEDULED
    highest = datetime(9999, 12, 31, 9, 59, 59, 999999, tzinfo=timezone(timedelta(hours=-14)))         # exactly 9999-12-31T23:59:59.999999Z
    assert highest.astimezone(UTC) == datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
    with pytest.raises(RetryScheduleError) as caught:                                                  # as the first attempt the window end overflows
        _schedule(first=highest, failed=highest)
    assert caught.value.code == "INVALID_TIME"
    late_first = datetime(9999, 12, 30, 12, 0, tzinfo=UTC)
    assert _schedule(first=late_first, failed=highest, base=timedelta(days=3), cap=timedelta(days=3)).outcome is RetryOutcome.WINDOW_EXPIRED


# ============================================================================
# Hardening F2 (delivery attempt): an unrepresentable timestamp is a validation error, never a raw overflow
# ============================================================================

@pytest.mark.parametrize("instant", EXTREME_OFFSET_INSTANTS)
def test_an_attempt_with_a_timestamp_utc_cannot_represent_is_a_validation_error(instant):
    for overrides in ({"attempted_at": instant},
                      {"outcome": DeliveryOutcome.FAILED_RETRYABLE, "next_attempt_at": instant, "http_status": 503}):
        with pytest.raises(ValidationError) as caught:
            _attempt(**overrides)
        assert not isinstance(caught.value, OverflowError) and "UTC cannot represent" in str(caught.value)


def test_an_attempt_accepts_the_representable_extremes_in_utc():
    lowest = datetime(1, 1, 1, 14, 0, tzinfo=timezone(timedelta(hours=14)))
    highest = datetime(9999, 12, 31, 9, 59, 59, 999999, tzinfo=timezone(timedelta(hours=-14)))
    assert _attempt(attempted_at=lowest).attempted_at == datetime(1, 1, 1, tzinfo=UTC)
    retry = _attempt(attempted_at=lowest, outcome=DeliveryOutcome.FAILED_RETRYABLE, next_attempt_at=highest, http_status=503)
    assert retry.next_attempt_at == datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
