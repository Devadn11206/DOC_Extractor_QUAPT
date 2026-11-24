"""
Prometheus metrics (PRD NFR-010, SEC-021).

A small registry that renders the Prometheus text exposition format (version 0.0.4). It needs no dependency, so the
API extra stays exactly as documented.

Label rules: labels are low-cardinality codes only. Routes are recorded by their template (for example
``/api/v1/documents/{document_id}``), never the raw path. Tenant identifiers, principal identifiers, document content
and credentials never become label values (SEC-012).

The metrics are served by ``doc_extractor.api.metrics_app`` on an internal port, not on the public API port.
"""

from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

LabelValues = Tuple[str, ...]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(names: Sequence[str], values: LabelValues) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{n}="{_escape(v)}"' for n, v in zip(names, values))
    return "{" + pairs + "}"


class Counter:
    kind = "counter"

    def __init__(self, name: str, help_text: str, label_names: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self._values: Dict[LabelValues, float] = {}
        self._lock = threading.Lock()

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name} takes {len(self.label_names)} labels")
        with self._lock:
            self._values[label_values] = self._values.get(label_values, 0.0) + amount

    def value(self, *label_values: str) -> float:
        with self._lock:
            return self._values.get(label_values, 0.0)

    def samples(self) -> List[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [f"{self.name}{_labels(self.label_names, lv)} {_num(v)}" for lv, v in items]


class Histogram:
    kind = "histogram"

    def __init__(self, name: str, help_text: str, buckets: Iterable[float], label_names: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = tuple(label_names)
        self._buckets = sorted(float(b) for b in buckets)
        self._counts: Dict[LabelValues, List[int]] = {}
        self._sums: Dict[LabelValues, float] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, *label_values: str) -> None:
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name} takes {len(self.label_names)} labels")
        with self._lock:
            counts = self._counts.setdefault(label_values, [0] * (len(self._buckets) + 1))
            for index, bound in enumerate(self._buckets):
                if value <= bound:
                    counts[index] += 1
            counts[-1] += 1  # +Inf
            self._sums[label_values] = self._sums.get(label_values, 0.0) + value

    def samples(self) -> List[str]:
        out: List[str] = []
        with self._lock:
            items = sorted(self._counts.items())
            sums = dict(self._sums)
        for lv, counts in items:
            base = dict(zip(self.label_names, lv))
            for index, bound in enumerate(self._buckets):
                labels = {**base, "le": _num(bound)}
                out.append(f"{self.name}_bucket{_format_labels(labels)} {counts[index]}")
            out.append(f"{self.name}_bucket{_format_labels({**base, 'le': '+Inf'})} {counts[-1]}")
            out.append(f"{self.name}_sum{_format_labels(base)} {_num(sums[lv])}")
            out.append(f"{self.name}_count{_format_labels(base)} {counts[-1]}")
        return out


def _format_labels(labels: Dict[str, str]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


def _num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class Registry:
    def __init__(self) -> None:
        self._metrics: List[object] = []

    def register(self, metric):
        self._metrics.append(metric)
        return metric

    def render(self) -> str:
        lines: List[str] = []
        for metric in self._metrics:
            lines.append(f"# HELP {metric.name} {metric.help}")
            lines.append(f"# TYPE {metric.name} {metric.kind}")
            lines.extend(metric.samples())
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

HTTP_REQUESTS = REGISTRY.register(Counter(
    "doc_extractor_http_requests_total",
    "HTTP requests by route template and status code.",
    ("route", "status"),
))
HTTP_DURATION = REGISTRY.register(Histogram(
    "doc_extractor_http_request_duration_seconds",
    "HTTP request duration by route template.",
    (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    ("route",),
))
RATE_LIMITED = REGISTRY.register(Counter(
    "doc_extractor_rate_limited_total",
    "Requests refused by the distributed rate limiter (API-012).",
    (),
))
SECURITY_ALERTS = REGISTRY.register(Counter(
    "doc_extractor_security_alerts_total",
    "Security alerts raised, by condition (SEC-021).",
    ("condition",),
))
def render_metrics() -> str:
    return REGISTRY.render()


def route_label(template: Optional[str]) -> str:
    """The route template, or a fixed label when the request matched no route. Never the raw path."""
    return template or "unmatched"
