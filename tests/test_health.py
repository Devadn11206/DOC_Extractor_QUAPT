"""
Unit tests for Module A health status and basic entry point.
"""

import pytest
from doc_extractor.config import Settings
from doc_extractor.main import VALID_DOCUMENT_STATUSES, get_module_health


@pytest.mark.unit
def test_get_module_health():
    """Verify health endpoint response contains PRD-compliant attributes and truthful scanner status."""
    settings = Settings()
    health = get_module_health(settings)

    assert health["status"] == "healthy"
    assert health["service"] == "module-a-ingestion"
    assert health["egress_isolated"] is True
    assert health["target_render_dpi"] == 200
    assert health["malware_scan_enabled"] is True
    # Without an operational scanner supplied, malware_scanner_ready must be False
    assert health["malware_scanner_ready"] is False
    assert health["malware_scanner_engine"] == "unconfigured"


@pytest.mark.unit
def test_valid_document_statuses():
    """Ensure standard SCH-012 statuses are present."""
    expected = {"queued", "processing", "auto_approved", "needs_review", "reviewed", "rejected", "failed"}
    assert set(VALID_DOCUMENT_STATUSES) == expected

