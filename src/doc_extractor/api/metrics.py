"""
Prometheus Metrics & Observability Registry (API-009, M1, M6, M7, M9, M11, SEC-012).
Zero PII/PHI in metric labels or values.
"""

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

METRICS_REGISTRY = CollectorRegistry()

# Extraction latency per page (M7)
PAGE_EXTRACTION_LATENCY = Histogram(
    "doc_extractor_page_extraction_seconds",
    "Time taken to extract and validate a single page in seconds",
    ["document_type"],
    registry=METRICS_REGISTRY,
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0),
)

# Field confidence distributions (M6)
FIELD_CONFIDENCE_HISTOGRAM = Histogram(
    "doc_extractor_field_confidence",
    "Calibrated confidence score distribution for extracted fields",
    ["document_type", "is_handwritten"],
    registry=METRICS_REGISTRY,
    buckets=(0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0),
)

# Document counter by status (auto_approved, needs_review, failed, reviewed)
DOCUMENT_PROCESSED_TOTAL = Counter(
    "doc_extractor_documents_total",
    "Total documents processed by terminal / routing status",
    ["tenant_id", "document_type", "status"],
    registry=METRICS_REGISTRY,
)

# Egress counter (M9 - must be 0 for on-premise air-gapped guarantees)
EXTERNAL_EGRESS_TOTAL = Counter(
    "doc_extractor_external_egress_total",
    "Total attempted external network egress connections",
    ["destination"],
    registry=METRICS_REGISTRY,
)

# Escapes counter (M11 - documents erroneously auto-approved)
ESCAPES_TOTAL = Counter(
    "doc_extractor_escapes_total",
    "Total production escapes where an auto-approved document contained invalid data",
    ["document_type"],
    registry=METRICS_REGISTRY,
)


def get_prometheus_metrics() -> bytes:
    """Returns serialized Prometheus metrics strictly free of PII."""
    return generate_latest(METRICS_REGISTRY)

