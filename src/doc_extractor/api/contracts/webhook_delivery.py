"""
Webhook delivery outcome and attempt contracts (PRD v1.5 section 7.1; delivery log in SCR-07), Phase 4. PROVISIONAL.

A ``DeliveryAttempt`` is the typed, validated record of ONE attempt to deliver ONE event to ONE subscription. It carries exactly
what the PRD's delivery log lists ("event ID, event, attempt count, HTTP status, next retry; no payload content") plus the
subscription, the outcome and the attempt time. It holds no payload, no URL, no secret, no response body and no free text: the
only diagnostic is a fixed ``error_code`` token. It is a contract, not storage: nothing here persists an attempt, and it does not
replace ``records.WebhookDeliveryRecord`` (the persistence shape, whose free-text ``status`` and ``error_message`` are a privacy
risk that the persistence slice must resolve; that record was not changed).

OUTCOMES. ``DELIVERED``; ``FAILED_RETRYABLE`` (a retry is scheduled: ``next_attempt_at`` is set); ``FAILED_PERMANENT`` (no
retry); ``EXPIRED`` (the failure was retryable in principle but the 24-hour window is exhausted, so delivery STOPS). WHICH
transport results map to which outcome (the 2xx range, 3xx, 4xx, 429 and ``Retry-After``, 5xx, timeouts, TLS and DNS errors,
refused destinations) is a classification policy the PRD does not state: it is NOT implemented here and is left to the
dispatcher slice. ``services/webhook_retry.py`` decides between ``FAILED_RETRYABLE`` and ``EXPIRED`` from the window.
"""

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, field_validator, model_validator

_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{7,127}")
_SUBSCRIPTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{0,127}")
_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{1,63}")


class DeliveryOutcome(str, Enum):
    DELIVERED = "delivered"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_PERMANENT = "failed_permanent"
    EXPIRED = "expired"


def _aware(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a timezone-aware time is required")
    try:
        return value.astimezone(timezone.utc)
    except OverflowError:                       # year 1 or 9999 with an offset: pydantic converts only ValueError into a validation error
        raise ValueError("a time that UTC cannot represent") from None


class DeliveryAttempt(BaseModel):
    """One delivery attempt: identifiers, codes and times only (no payload content, no URL, no secret, no response body)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    subscription_id: StrictStr
    event_id: StrictStr
    event_type: Literal["document.completed", "document.needs_review", "document.failed", "schema.approved"]
    attempt_number: StrictInt
    outcome: DeliveryOutcome
    attempted_at: datetime
    http_status: Optional[StrictInt] = None
    error_code: Optional[StrictStr] = None
    next_attempt_at: Optional[datetime] = None

    @field_validator("subscription_id")
    @classmethod
    def _subscription_id(cls, value: str) -> str:
        if not _SUBSCRIPTION_ID.fullmatch(value):
            raise ValueError("not a subscription identifier")
        return value

    @field_validator("event_id")
    @classmethod
    def _event_id(cls, value: str) -> str:
        if not _EVENT_ID.fullmatch(value):
            raise ValueError("not an event identifier")
        return value

    @field_validator("attempt_number")
    @classmethod
    def _attempt_number(cls, value: int) -> int:
        if isinstance(value, bool) or value < 1:
            raise ValueError("attempt numbers start at 1")
        return value

    @field_validator("http_status")
    @classmethod
    def _http_status(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and (isinstance(value, bool) or not 100 <= value <= 599):
            raise ValueError("not an HTTP status")
        return value

    @field_validator("error_code")
    @classmethod
    def _error_code(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not _ERROR_CODE.fullmatch(value):
            raise ValueError("not a fixed error code")
        return value

    @field_validator("attempted_at", "next_attempt_at", mode="before")
    @classmethod
    def _times(cls, value: Any) -> Optional[datetime]:
        return None if value is None else _aware(value)

    @model_validator(mode="after")
    def _outcome_is_consistent(self) -> "DeliveryAttempt":
        retry = self.outcome is DeliveryOutcome.FAILED_RETRYABLE
        if retry and (self.next_attempt_at is None or self.next_attempt_at <= self.attempted_at):
            raise ValueError("a retryable failure schedules a later attempt")
        if not retry and self.next_attempt_at is not None:
            raise ValueError("only a retryable failure has a next attempt")
        if self.outcome is DeliveryOutcome.DELIVERED and self.error_code is not None:
            raise ValueError("a delivered event has no error code")
        return self
