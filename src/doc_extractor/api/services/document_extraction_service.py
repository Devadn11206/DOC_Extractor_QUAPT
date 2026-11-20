"""
Synchronous extraction for the REST API (Module M5 / Phase 2, API-004), PROVISIONAL.

A thin adapter over the EXISTING ``DocumentService.process_stored_page``: routing, normalization, validation and
extraction are done there and are not reimplemented. This module adds only what the HTTP boundary needs:

- ``ExtractionGate``: bounded, non-blocking admission plus a deadline. The extraction runs in its own daemon worker thread
  (never on the event loop). A slot is returned only when the worker has REALLY finished, so a request that timed out
  keeps its slot until the work ends: abandoned work can never pile up beyond ``max_concurrent`` threads, and nothing is
  queued (a request that finds no free slot is refused at once).
- ``run_stored_page_extraction``: the worker body. It converts the result into the redacted public DTO inside the worker
  (the internal envelope never leaves it) and turns every failure into a sanitized public error.
- ``public_failure``: the one mapping from the processing service's failed/rejected results to HTTP errors.

Logging: only fixed reason codes, the request id and the document id. Never extracted values, evidence text, paths,
exception texts or prompts. (The processing service and Module B log their own diagnostics, which this module cannot
change; see docs/m5-open-decisions.md, "M2D API-004".)
"""

import asyncio
import logging
import threading
from typing import Any, Callable, Optional

from doc_extractor.api.contracts.extraction import ExtractionResponse
from doc_extractor.api.exceptions import (
    ApiHttpError,
    ExtractionTimeoutError,
    RateLimitExceededError,
)
from doc_extractor.exceptions import (
    LLMExecutionError,
    StorageError,
    TenantMismatchError,
    UnsupportedFileError,
)

logger = logging.getLogger("doc_extractor.api.extraction")

# Fixed server-side reason per processing-service error code (never the service's message). Anything else: "extract_failed".
_FAILURE_REASONS = {
    "HANDOFF_NOT_FOUND": "extract_handoff_not_found",
    "MALFORMED_HANDOFF": "extract_handoff_malformed",
    "PAGE_NOT_FOUND": "extract_page_not_found",
    "STORAGE_ERROR": "extract_storage_error",
    "STORAGE_RESOLUTION_FAILED": "extract_storage_resolution_failed",
    "INGESTION_ERROR": "extract_ingestion_error",
    "MODULE_B_ERROR": "extract_module_b_error",
    "ROUTING_FAILED": "extract_routing_failed",
    "ROTATION_FAILED": "extract_rotation_failed",
    "CANONICAL_ASSEMBLY_FAILED": "extract_assembly_failed",
}
REASON_UNEXPECTED = "extract_unexpected_error"
REASON_FAILED = "extract_failed"
REASON_TIMEOUT = "extract_timeout"
REASON_OVERLOAD = "extract_overloaded"


def _generic_failure(reason: str, request_id: Optional[str]) -> StorageError:
    """A generic 500 whose fixed reason is logged server-side by the existing handler; the client sees INTERNAL_ERROR only."""
    return StorageError("Extraction failed.", request_id=request_id, details={"reason": reason})


def public_failure(result: Any, request_id: Optional[str]) -> Exception:
    """The sanitized public error for a failed, rejected or otherwise unsuccessful ``DocumentProcessingResult``."""
    if getattr(result, "status", None) == "rejected":
        return UnsupportedFileError(message="This file type isn't supported.", request_id=request_id, details={})
    error = getattr(result, "error", None)
    code = error.get("code") if isinstance(error, dict) else None
    reason = _FAILURE_REASONS.get(code) if isinstance(code, str) else None
    return _generic_failure(reason or REASON_FAILED, request_id)


def run_stored_page_extraction(
    document_service: Any, document_id: str, page_number: int, request_id: Optional[str]
) -> ExtractionResponse:
    """
    Worker body (runs in the extraction thread): the existing single-page processing, then the redacted public response.
    Raises a sanitized public error for every failure; a cross-tenant signal propagates so the handler conceals it as 404.
    """
    try:
        result = document_service.process_stored_page(
            document_id=document_id, page_number=page_number, request_id=request_id
        )
    except TenantMismatchError:
        raise                                                  # concealed as DOCUMENT_NOT_FOUND by the exception handler
    except ApiHttpError:
        raise
    except StorageError:
        raise                                                  # the handler answers a generic 500 and logs its fixed reason
    except LLMExecutionError:
        raise ApiHttpError(
            message="The extraction provider failed.", code="LLM_EXECUTION_FAILED", status_code=502,
            request_id=request_id, details={},
        ) from None
    except Exception:
        raise _generic_failure(REASON_UNEXPECTED, request_id) from None

    envelope = getattr(result, "envelope", None)
    if not getattr(result, "is_success", False) or envelope is None:
        raise public_failure(result, request_id)
    try:
        return ExtractionResponse.from_envelope(envelope)
    except Exception:
        raise _generic_failure(REASON_UNEXPECTED, request_id) from None


class ExtractionGate:
    """
    Process-local admission control and deadline for synchronous extraction.

    ``max_concurrent`` is the number of extractions that may run at once, counting workers that are still running after
    their request timed out. There is no queue: a request without a free slot is refused immediately (HTTP 429).
    The lock guards only the counter and is never held across an await.
    """

    def __init__(self, max_concurrent: int, timeout_seconds: float) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._active = 0

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def _try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.max_concurrent:
                return False
            self._active += 1
            return True

    def _release(self) -> None:
        with self._lock:
            self._active -= 1

    async def run(self, work: Callable[[], Any], *, request_id: Optional[str], document_id: str) -> Any:
        """
        Run ``work`` in a dedicated daemon thread and await it for at most ``timeout_seconds``.

        - No free slot: ``RateLimitExceededError`` (429) at once.
        - Deadline exceeded: ``ExtractionTimeoutError`` (504). The worker keeps running and keeps its slot until it
          finishes; its eventual result is discarded.
        - The slot is released by the worker thread itself, after ``work`` returned or raised, whatever happened to the request
          (timeout, client disconnect, cancellation).
        """
        if not self._try_acquire():
            logger.warning(
                "Extraction refused: no free slot (reason=%s, request_id=%s, document_id=%s)",
                REASON_OVERLOAD, request_id, document_id,
            )
            raise RateLimitExceededError(
                message="Too many concurrent extractions. Try again shortly.", retry_after_seconds=5,
                request_id=request_id, details={},
            )

        loop = asyncio.get_running_loop()
        outcome: "asyncio.Future[Any]" = loop.create_future()

        def deliver(value: Any, error: Optional[BaseException]) -> None:
            if outcome.done():                                  # the request already gave up (timeout, disconnect)
                return
            if error is not None:
                outcome.set_exception(error)
            else:
                outcome.set_result(value)

        def worker() -> None:
            value: Any = None
            error: Optional[BaseException] = None
            try:
                value = work()
            except BaseException as exc:                        # noqa: BLE001 - handed to the awaiting request
                error = exc
            finally:
                self._release()                                 # only now is the capacity free again
            try:
                loop.call_soon_threadsafe(deliver, value, error)
            except RuntimeError:                                # the event loop is already closed
                pass

        try:
            threading.Thread(target=worker, name="extract-sync", daemon=True).start()
        except BaseException:
            self._release()
            raise

        try:
            return await asyncio.wait_for(outcome, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "Extraction timed out (reason=%s, request_id=%s, document_id=%s)", REASON_TIMEOUT, request_id, document_id
            )
            raise ExtractionTimeoutError(request_id=request_id, details={}) from None
