"""
Typed errors of the durable queue adapter (Team Member 3, Phase 3).

Every error has a FIXED message and a fixed machine-readable ``reason`` in ``details``. None carries a broker URL, a credential, a
queue message or the text of a driver error (those can quote the connection string or the message body). Operational faults derive
from ``StorageError`` (the REST API answers those with a generic 500 and logs only the fixed reason); a configuration fault derives
from ``ConfigurationError``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from doc_extractor.exceptions import ConfigurationError, StorageError


def _details(reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    details: Dict[str, Any] = {"reason": reason}
    if extra:
        details.update(extra)
    return details


class QueueError(StorageError):
    """Base of every failure raised by the durable queue adapter."""

    default_message = "A queue operation failed."
    default_reason = "queue_failed"
    default_code = "QUEUE_FAILED"

    def __init__(self, reason: Optional[str] = None) -> None:
        super().__init__(message=self.default_message, code=self.default_code, details=_details(reason or self.default_reason))


class BrokerUnavailableError(QueueError):
    """The broker could not be reached, or an operation timed out. Nothing is known to be delivered; safe to retry later."""

    default_message = "The message broker is unavailable."
    default_reason = "broker_unavailable"
    default_code = "BROKER_UNAVAILABLE"


class BrokerPublishError(QueueError):
    """
    A message was not confirmed by the broker (refused, unroutable or unconfirmed in time). It is NOT known to be queued.
    ``published`` lists the tracking ids already confirmed before the failure of a batch (a batch is sequential, not atomic;
    republishing a whole batch is safe because jobs are de-duplicated by key).
    """

    default_message = "The message broker did not confirm the message."
    default_reason = "publish_not_confirmed"
    default_code = "BROKER_PUBLISH_FAILED"

    def __init__(self, reason: Optional[str] = None, *, published: Optional[List[str]] = None) -> None:
        super().__init__(reason)
        self.published: List[str] = list(published or [])


class ReceiptHandleError(QueueError, ValueError):
    """The receipt handle is not one this consumer handed out (unknown, already settled, or from another process)."""

    default_message = "The receipt handle is not valid."
    default_reason = "unknown_receipt_handle"
    default_code = "RECEIPT_HANDLE_INVALID"


class QueueUsageError(QueueError, ValueError):
    """A caller passed an argument the queue contract does not allow (a bound, a type)."""

    default_message = "The queue request is not valid."
    default_reason = "invalid_queue_request"
    default_code = "QUEUE_REQUEST_INVALID"


class BrokerConfigurationError(ConfigurationError):
    """The broker configuration is invalid or incomplete. The message never contains the connection URL."""

    def __init__(self, reason: str, *, setting: Optional[str] = None) -> None:
        extra = {"setting": setting} if setting else None
        super().__init__(message="The message broker configuration is not valid.", details=_details(reason, extra))
