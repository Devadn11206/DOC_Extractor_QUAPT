"""
Security alerts (PRD SEC-021).

The system raises an alert for each of these conditions:
- repeated failed logins,
- unusual export volume,
- unmasking spikes (reveals of PII values, SEC-007),
- egress attempts (outbound connections refused by the egress policy, SEC-011),
- audit-chain failures (SEC-008),
- model-file changes (SEC-014).

Rules:
- An alert carries identifiers, counts and codes only, never document content, PII or a credential (SEC-012).
  A credential is recorded as a short SHA-256 fingerprint, never as the token itself.
- Counting is per key (tenant, credential fingerprint, purpose) over a sliding window. A breach raises one alert per
  key per window, so a flood produces one alert, not thousands.
- Raising an alert never fails the operation that triggered it. Sink errors are logged by exception type only.

Thresholds are assumptions to tune against real traffic. The PRD names the conditions but not the numbers.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Deque, Dict, Hashable, Mapping, Optional

logger = logging.getLogger("doc_extractor.alerts")


class AlertCondition(str, Enum):
    REPEATED_FAILED_LOGINS = "repeated_failed_logins"
    UNUSUAL_EXPORT_VOLUME = "unusual_export_volume"
    UNMASK_SPIKE = "unmask_spike"
    EGRESS_ATTEMPT = "egress_attempt"
    AUDIT_CHAIN_FAILURE = "audit_chain_failure"
    MODEL_FILE_CHANGE = "model_file_change"
    QUEUE_WORKER_FAILURE = "queue_worker_failure"
    AUDIT_CHECKPOINT_FAILURE = "audit_checkpoint_failure"


@dataclass(frozen=True)
class Alert:
    condition: AlertCondition
    subject: str
    count: int
    window_seconds: float
    details: Dict[str, object] = field(default_factory=dict)


AlertSink = Callable[[Alert], None]


def credential_fingerprint(token: str) -> str:
    """A short, non-reversible identifier for a credential, safe to log (SEC-012)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _log_sink(alert: Alert) -> None:
    logger.warning(
        "SECURITY_ALERT condition=%s subject=%s count=%s window_seconds=%s",
        alert.condition.value,
        alert.subject,
        alert.count,
        alert.window_seconds,
    )


class SlidingWindowCounter:
    """Weighted events per key inside a sliding window. Reports a breach once per key per window."""

    def __init__(self, threshold: float, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        if threshold <= 0 or window_seconds <= 0:
            raise ValueError("Alert threshold and window must be positive.")
        self._threshold = threshold
        self._window = window_seconds
        self._clock = clock
        self._events: Dict[Hashable, Deque[tuple]] = {}
        self._suppressed_until: Dict[Hashable, float] = {}
        self._lock = threading.Lock()

    @property
    def window_seconds(self) -> float:
        return self._window

    def record(self, key: Hashable, weight: float = 1.0, now: Optional[float] = None) -> Optional[float]:
        """Returns the windowed total when this event breaches the threshold for the first time in the window."""
        current = self._clock() if now is None else now
        with self._lock:
            events = self._events.setdefault(key, deque())
            cutoff = current - self._window
            while events and events[0][0] <= cutoff:
                events.popleft()
            events.append((current, weight))
            total = sum(w for _, w in events)
            if total >= self._threshold and self._suppressed_until.get(key, float("-inf")) <= current:
                self._suppressed_until[key] = current + self._window
                return total
            if not events:
                self._events.pop(key, None)
        return None


class AlertMonitor:
    """Counts security events and emits alerts through a sink (SEC-021)."""

    def __init__(
        self,
        sink: Optional[AlertSink] = None,
        *,
        failed_login_threshold: int = 5,
        failed_login_window_seconds: float = 300,
        export_rows_threshold: int = 10_000,
        export_window_seconds: float = 3600,
        unmask_threshold: int = 20,
        unmask_window_seconds: float = 3600,
        egress_window_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink or _log_sink
        self._failed_logins = SlidingWindowCounter(failed_login_threshold, failed_login_window_seconds, clock)
        self._exports = SlidingWindowCounter(export_rows_threshold, export_window_seconds, clock)
        self._unmasks = SlidingWindowCounter(unmask_threshold, unmask_window_seconds, clock)
        self._egress = SlidingWindowCounter(1, egress_window_seconds, clock)

    # -- event intake ---------------------------------------------------------------------------------------

    def record_failed_login(self, credential_id: str, now: Optional[float] = None) -> Optional[Alert]:
        """``credential_id`` must be a fingerprint (see ``credential_fingerprint``), never a token."""
        total = self._failed_logins.record(credential_id, 1.0, now)
        return self._emit(AlertCondition.REPEATED_FAILED_LOGINS, credential_id, total, self._failed_logins.window_seconds)

    def record_export(self, tenant_id: str, rows: int, now: Optional[float] = None) -> Optional[Alert]:
        total = self._exports.record(tenant_id, float(rows), now)
        return self._emit(AlertCondition.UNUSUAL_EXPORT_VOLUME, tenant_id, total, self._exports.window_seconds)

    def record_unmask(self, tenant_id: str, actor_id: str, now: Optional[float] = None) -> Optional[Alert]:
        total = self._unmasks.record((tenant_id, actor_id), 1.0, now)
        return self._emit(AlertCondition.UNMASK_SPIKE, f"{tenant_id}:{actor_id}", total, self._unmasks.window_seconds)

    def record_egress_attempt(self, purpose: str, now: Optional[float] = None) -> Optional[Alert]:
        """Every refused outbound connection is a finding. Repeats within the window are folded into one alert."""
        total = self._egress.record(purpose, 1.0, now)
        return self._emit(AlertCondition.EGRESS_ATTEMPT, purpose, total, self._egress.window_seconds)

    def report_audit_chain(self, tenant_id: str, valid: bool, reason: Optional[str], first_bad_seq: Optional[int]) -> Optional[Alert]:
        if valid:
            return None
        alert = Alert(
            condition=AlertCondition.AUDIT_CHAIN_FAILURE,
            subject=tenant_id,
            count=1,
            window_seconds=0,
            details={"reason": reason or "unknown", "first_bad_seq": first_bad_seq},
        )
        return self._dispatch(alert)

    def record_queue_worker_failure(self, job_id: str, reason: str) -> Optional[Alert]:
        """Emits an alert when a queue worker encounters a terminal processing failure."""
        alert = Alert(
            condition=AlertCondition.QUEUE_WORKER_FAILURE,
            subject=job_id,
            count=1,
            window_seconds=0,
            details={"reason": reason},
        )
        return self._dispatch(alert)

    def record_checkpoint_failure(self, tenant_id: str, reason: str) -> Optional[Alert]:
        """Emits an alert when signed checkpoint generation or external sink persistence fails."""
        alert = Alert(
            condition=AlertCondition.AUDIT_CHECKPOINT_FAILURE,
            subject=tenant_id,
            count=1,
            window_seconds=0,
            details={"reason": reason},
        )
        return self._dispatch(alert)

    def check_model_files(self, expected_sha256: Mapping[str, str], root: Path) -> list:
        """Compares each model file with its pinned SHA-256 (SEC-014). Missing or changed files raise an alert each."""
        raised = []
        for name, expected in expected_sha256.items():
            path = Path(root) / name
            try:
                actual = _sha256_file(path)
            except OSError:
                actual = None
            if actual != expected:
                alert = Alert(
                    condition=AlertCondition.MODEL_FILE_CHANGE,
                    subject=name,
                    count=1,
                    window_seconds=0,
                    details={"reason": "missing" if actual is None else "hash_mismatch"},
                )
                raised.append(self._dispatch(alert))
        return [alert for alert in raised if alert is not None]

    # -- emission -------------------------------------------------------------------------------------------

    def _emit(self, condition: AlertCondition, subject: str, total: Optional[float], window: float) -> Optional[Alert]:
        if total is None:
            return None
        return self._dispatch(Alert(condition=condition, subject=subject, count=int(total), window_seconds=window))

    def _dispatch(self, alert: Alert) -> Alert:
        from doc_extractor.metrics import SECURITY_ALERTS

        SECURITY_ALERTS.inc(alert.condition.value)
        try:
            self._sink(alert)
        except Exception as exc:  # the operation that raised the alert must not fail because of the alert sink
            logger.error("Security alert delivery failed (%s).", type(exc).__name__)
        return alert


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


_default_monitor: Optional[AlertMonitor] = None
_default_lock = threading.Lock()


def get_default_monitor() -> AlertMonitor:
    """Process-wide monitor used by components that do not receive one (egress, authentication)."""
    global _default_monitor
    with _default_lock:
        if _default_monitor is None:
            _default_monitor = AlertMonitor()
        return _default_monitor


def set_default_monitor(monitor: Optional[AlertMonitor]) -> None:
    global _default_monitor
    with _default_lock:
        _default_monitor = monitor
