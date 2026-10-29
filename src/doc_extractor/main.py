"""
Health status and foundational entry point for Module A.

Implements the status schema required for API-009 (/v1/health) and pipeline status
definitions aligned with SCH-012.
"""

from typing import Any, Dict, Optional
from doc_extractor import __version__
from doc_extractor.config import Settings, load_settings
from doc_extractor.logging import setup_logging


# PRD SCH-012 standard processing lifecycle statuses
VALID_DOCUMENT_STATUSES = (
    "queued",
    "processing",
    "auto_approved",
    "needs_review",
    "reviewed",
    "rejected",
    "failed",
)


def get_module_health(
    settings: Settings,
    scanner: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Returns the module operational status, environment, version, and truthful security readiness.
    Aligned with PRD API-009 requirements.
    """
    scanner_operational = False
    scanner_name = "unconfigured"
    if scanner is not None:
        scanner_operational = scanner.is_operational()
        scanner_name = type(scanner).__name__

    return {
        "status": "healthy",
        "service": settings.service_name,
        "version": __version__,
        "environment": settings.app_env,
        "egress_isolated": not settings.allow_external_egress,
        "target_render_dpi": settings.target_render_dpi,
        "malware_scan_enabled": settings.malware_scan_enabled,
        "malware_scanner_type": settings.malware_scanner_type,
        "malware_scanner_ready": scanner_operational,
        "malware_scanner_engine": scanner_name,
    }


def main() -> None:
    """Foundational entry point to verify configuration and logging initialization."""
    settings = load_settings()
    logger = setup_logging(
        level=settings.log_level,
        service_name=settings.service_name,
        environment=settings.app_env,
    )
    health = get_module_health(settings)
    logger.info("Module A foundation initialized successfully", extra={"extra_data": health})


if __name__ == "__main__":
    main()

