"""
Prometheus metrics (PRD NFR-010, SEC-012, SEC-021). Labels are low-cardinality codes; tenant identifiers, principal
identifiers, raw paths and credentials never appear in the exposition.
"""

import pytest

from doc_extractor.metrics import Counter, Histogram, Registry, route_label

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[api]')")

from fastapi.testclient import TestClient  # noqa: E402

from doc_extractor.alerts import AlertMonitor  # noqa: E402
from doc_extractor.api.metrics_app import app as metrics_app  # noqa: E402
from doc_extractor.metrics import HTTP_REQUESTS, REGISTRY, SECURITY_ALERTS, render_metrics  # noqa: E402


def test_the_exposition_has_help_and_type_lines_and_labelled_samples():
    registry = Registry()
    counter = registry.register(Counter("demo_total", "Demo counter.", ("code",)))
    counter.inc("ok")
    counter.inc("ok")
    text = registry.render()
    assert "# HELP demo_total Demo counter." in text
    assert "# TYPE demo_total counter" in text
    assert 'demo_total{code="ok"} 2' in text


def test_histogram_buckets_are_cumulative_and_end_with_inf():
    registry = Registry()
    histogram = registry.register(Histogram("demo_seconds", "Demo.", (0.5, 1.0), ("route",)))
    histogram.observe(0.2, "/x")
    histogram.observe(0.7, "/x")
    text = registry.render()
    assert 'demo_seconds_bucket{route="/x",le="0.5"} 1' in text
    assert 'demo_seconds_bucket{route="/x",le="1"} 2' in text
    assert 'demo_seconds_bucket{route="/x",le="+Inf"} 2' in text
    assert 'demo_seconds_count{route="/x"} 2' in text


def test_label_values_are_escaped():
    registry = Registry()
    counter = registry.register(Counter("esc_total", "Escaping.", ("v",)))
    counter.inc('a"b\\c\nd')
    assert 'esc_total{v="a\\"b\\\\c\\nd"} 1' in registry.render()


def test_a_wrong_label_count_is_a_programming_error():
    with pytest.raises(ValueError):
        Counter("x_total", "x", ("a", "b")).inc("only-one")


def test_route_labels_are_templates_and_unmatched_is_fixed():
    assert route_label("/api/v1/documents/{document_id}") == "/api/v1/documents/{document_id}"
    assert route_label(None) == "unmatched"


def test_requests_are_counted_by_template_not_by_identifier():
    from doc_extractor.api.app import create_app
    from doc_extractor.api.config import ApiSettings

    client = TestClient(create_app(ApiSettings(app_env="development")))
    # No authentication is configured here, so the documented route answers 503 (PortNotConfigured), deterministically.
    before = HTTP_REQUESTS.value("/api/v1/documents/{document_id}", "503")
    response = client.get("/api/v1/documents/doc_SECRETID123")
    assert response.status_code == 503
    assert HTTP_REQUESTS.value("/api/v1/documents/{document_id}", "503") == before + 1
    assert "doc_SECRETID123" not in render_metrics()


def test_unknown_paths_are_counted_as_unmatched():
    from doc_extractor.api.app import create_app
    from doc_extractor.api.config import ApiSettings

    client = TestClient(create_app(ApiSettings(app_env="development")))
    before = HTTP_REQUESTS.value("unmatched", "404")
    assert client.get("/no/such/path/tenant_secret_name").status_code == 404
    assert HTTP_REQUESTS.value("unmatched", "404") == before + 1
    assert "tenant_secret_name" not in render_metrics()


def test_the_internal_endpoint_serves_metrics_and_nothing_else():
    client = TestClient(metrics_app)
    ok = client.get("/metrics")
    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/plain; version=0.0.4")
    assert "doc_extractor_http_requests_total" in ok.text
    assert client.get("/api/v1/documents").status_code == 404
    assert client.post("/metrics").status_code == 404


def test_a_security_alert_increments_its_condition_counter():
    before = SECURITY_ALERTS.value("egress_attempt")
    AlertMonitor(lambda alert: None).record_egress_attempt("metric_test_purpose")
    assert SECURITY_ALERTS.value("egress_attempt") == before + 1
    assert 'doc_extractor_security_alerts_total{condition="egress_attempt"}' in render_metrics()


def test_the_registry_holds_only_the_documented_metrics():
    names = [m.name for m in REGISTRY._metrics]
    assert names == [
        "doc_extractor_http_requests_total",
        "doc_extractor_http_request_duration_seconds",
        "doc_extractor_rate_limited_total",
        "doc_extractor_security_alerts_total",
    ]
