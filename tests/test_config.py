"""
Unit tests for Module A configuration and security guardrails.

Tests verify:
- Default configuration values align with PRD specs.
- Rejection of invalid configuration (e.g., target_render_dpi != 200).
- Strict enforcement of zero outbound egress (ARC-001, SEC-011).
"""

import pytest
from doc_extractor.config import Settings, load_settings
from doc_extractor.exceptions import ConfigurationError


@pytest.mark.unit
def test_default_settings():
    """Verify that default settings adhere to PRD specifications."""
    settings = load_settings()
    assert settings.app_name == "doc-extraction-engine"
    assert settings.service_name == "module-a-ingestion"
    assert settings.target_render_dpi == 200  # PRD standard (FR-ING-004, SCH-013)
    assert settings.allow_external_egress is False  # PRD ARC-001, SEC-011
    assert "application/pdf" in settings.allowed_mime_types
    assert "image/tiff" in settings.allowed_mime_types
    assert settings.max_file_size_bytes == 50 * 1024 * 1024
    assert settings.max_page_count == 100


@pytest.mark.security
def test_reject_external_egress():
    """Ensure attempting to enable external egress fails closed with a ConfigurationError."""
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings({"allow_external_egress": True})
    assert "violates ARC-001/SEC-011" in str(exc_info.value)


@pytest.mark.unit
def test_reject_non_standard_render_dpi():
    """Ensure render DPI other than 200 is rejected per PRD FR-ING-004 and SCH-013."""
    with pytest.raises(ConfigurationError) as exc_info:
        load_settings({"target_render_dpi": 300})
    assert "strictly mandate 200 DPI" in str(exc_info.value)


@pytest.mark.unit
def test_custom_valid_settings():
    """Verify valid environment overrides are properly parsed."""
    settings = load_settings({
        "app_env": "production",
        "log_level": "WARNING",
        "max_page_count": 50,
    })
    assert settings.app_env == "production"
    assert settings.log_level == "WARNING"
    assert settings.max_page_count == 50

