"""
Pure exponential back-off scheduling inside the PRD's 24-hour retry window (PRD v1.5 section 7.1), Phase 4. PROVISIONAL.

PRD 7.1: webhooks are "retried with exponential back-off for 24 hours, and delivered at least once". This module is only the
ARITHMETIC of that sentence: a pure function of its arguments. It reads no clock (the caller supplies every instant), draws no
random jitter, starts no timer or background scheduler, stores nothing and claims no durable retry: a retry that survives a
restart needs a persistent delivery log and a worker that reads it (see the register section for Phase 4).

NOTHING IS INVENTED THAT THE PRD DOES NOT STATE. The base delay and the maximum delay are REQUIRED arguments with no default;
there is no default interval table and NO MAXIMUM ATTEMPT COUNT (the window alone ends the retries). The 24 hours is the PRD's
(``RETRY_WINDOW``). Two choices are provisional assumptions and are named here so that they can be overruled:

- DOUBLING. "Exponential" is taken as the delay after failed attempt ``n`` (1-based) being ``min(max_delay, base_delay * 2**(n-1))``.
- THE ANCHOR AND THE BOUNDARY. The window starts at the FIRST delivery attempt of the (subscription, event) pair, passed in as
  ``first_attempt_at`` (the PRD does not say whether it starts there or at the event time), and a retry may be scheduled at any
  instant up to and INCLUDING ``first_attempt_at + 24h``. A retry that would fall after it, or a failure observed after it, is
  ``WINDOW_EXPIRED``: delivery stops, explicitly (the caller records ``DeliveryOutcome.EXPIRED``).

TIME IS ELAPSED TIME. Every supplied instant is converted to UTC before any arithmetic, so the 24 hours and the delays are real
elapsed time whatever zone the caller used (a zone that observes DST would otherwise make "+24 hours" 23 or 25 real hours); the
instants in a ``RetryDecision`` are UTC.

Invalid input (an attempt number that is not a positive ``int``, a naive or non-datetime instant, an instant that cannot be
represented in UTC, a failure before the first attempt, a non-positive base delay, a maximum below the base) is a
``RetryScheduleError`` with a fixed code that echoes nothing.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from doc_extractor.api.contracts.webhook_delivery import DeliveryOutcome

RETRY_WINDOW = timedelta(hours=24)               # PRD 7.1


class RetryOutcome(str, Enum):
    SCHEDULED = "scheduled"                      # a retry is due at ``next_attempt_at``
    WINDOW_EXPIRED = "window_expired"            # the 24-hour window is exhausted: stop


class RetryScheduleError(Exception):
    """Invalid scheduling input. ``code`` is one of the constants below; the message is fixed and carries no input."""

    INVALID_ATTEMPT_NUMBER = "INVALID_ATTEMPT_NUMBER"
    INVALID_TIME = "INVALID_TIME"
    INVALID_DELAY = "INVALID_DELAY"
    _MESSAGES = {
        INVALID_ATTEMPT_NUMBER: "The attempt number must be a positive integer.",
        INVALID_TIME: "A timezone-aware time that is not before the first attempt is required.",
        INVALID_DELAY: "The base delay must be positive and the maximum delay not below it.",
    }

    def __init__(self, code: str) -> None:
        self.code = code if code in self._MESSAGES else self.INVALID_TIME
        super().__init__(self._MESSAGES[self.code])


@dataclass(frozen=True)
class RetryDecision:
    """The scheduling decision. ``next_attempt_at`` and ``delay`` are set only when ``outcome`` is ``SCHEDULED``."""

    outcome: RetryOutcome
    window_ends_at: datetime
    next_attempt_at: Optional[datetime] = None
    delay: Optional[timedelta] = None

    @property
    def attempt_outcome(self) -> DeliveryOutcome:
        """The outcome to record for the failed attempt: a retry is scheduled, or delivery has stopped."""
        return DeliveryOutcome.FAILED_RETRYABLE if self.outcome is RetryOutcome.SCHEDULED else DeliveryOutcome.EXPIRED


def _aware(value: Any) -> datetime:
    """
    The instant in UTC. Python adds a ``timedelta`` to an aware ``datetime`` in WALL-CLOCK time when its zone observes DST, so
    "+24 hours" would be 23 or 25 real hours across a change of clock: every instant is converted to UTC first, where a
    ``timedelta`` is exact elapsed time. An instant that cannot be represented in UTC is ``INVALID_TIME``.
    """
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RetryScheduleError(RetryScheduleError.INVALID_TIME)
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:
        raise RetryScheduleError(RetryScheduleError.INVALID_TIME) from None


def _valid_attempt(attempt_number: Any) -> int:
    if not isinstance(attempt_number, int) or isinstance(attempt_number, bool) or attempt_number < 1:
        raise RetryScheduleError(RetryScheduleError.INVALID_ATTEMPT_NUMBER)
    return attempt_number


def _valid_delays(base_delay: Any, max_delay: Any) -> None:
    if (
        not isinstance(base_delay, timedelta) or not isinstance(max_delay, timedelta)
        or base_delay <= timedelta(0) or max_delay < base_delay
    ):
        raise RetryScheduleError(RetryScheduleError.INVALID_DELAY)


def backoff_delay(*, attempt_number: int, base_delay: timedelta, max_delay: timedelta) -> timedelta:
    """``min(max_delay, base_delay * 2**(attempt_number - 1))`` for the delay after failed attempt ``attempt_number`` (1-based)."""
    _valid_attempt(attempt_number)
    _valid_delays(base_delay, max_delay)
    delay = base_delay
    try:
        for _ in range(attempt_number - 1):          # ends within about 70 doublings: the cap is reached
            if delay >= max_delay:
                break
            delay = delay * 2
    except OverflowError:
        return max_delay
    return min(delay, max_delay)


def schedule_retry(
    *,
    attempt_number: int,
    first_attempt_at: datetime,
    failed_at: datetime,
    base_delay: timedelta,
    max_delay: timedelta,
) -> RetryDecision:
    """
    Decide the retry after attempt ``attempt_number`` failed at ``failed_at``. ``first_attempt_at`` anchors the 24-hour window.
    Pure and deterministic: the same arguments always give the same decision. Raises ``RetryScheduleError`` for invalid input.
    """
    _valid_attempt(attempt_number)
    _valid_delays(base_delay, max_delay)
    first = _aware(first_attempt_at)
    failed = _aware(failed_at)
    if failed < first:
        raise RetryScheduleError(RetryScheduleError.INVALID_TIME)
    try:
        window_ends_at = first + RETRY_WINDOW
    except OverflowError:
        raise RetryScheduleError(RetryScheduleError.INVALID_TIME) from None

    delay = backoff_delay(attempt_number=attempt_number, base_delay=base_delay, max_delay=max_delay)
    try:
        next_attempt_at = failed + delay
    except OverflowError:                            # beyond the largest representable instant, so beyond the window
        return RetryDecision(RetryOutcome.WINDOW_EXPIRED, window_ends_at)
    if next_attempt_at > window_ends_at:                # also every failure observed after the end: the delay is positive
        return RetryDecision(RetryOutcome.WINDOW_EXPIRED, window_ends_at)
    return RetryDecision(RetryOutcome.SCHEDULED, window_ends_at, next_attempt_at, delay)
