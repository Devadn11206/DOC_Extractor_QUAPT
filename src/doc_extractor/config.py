"""
Typed configuration module for Document Extraction Engine - Module A.

Enforces:
- Customer VPC / On-prem boundary configuration (ARC-001, SEC-011)
- Ingestion limits (magic-byte validation, file size, page count) (FR-ING-002, GR-001)
- Target rendering standards (200 DPI) (FR-ING-004, SCH-013)
- Fail-closed security validation
"""

from pathlib import Path
import re
from typing import List, Literal, Optional
import urllib.parse
from pydantic import BaseModel, Field, field_validator
from doc_extractor.exceptions import ConfigurationError
from doc_extractor.vllm_client import is_private_host


# Text model served by vLLM for the settings-driven extraction path (text only, non-thinking).
DEFAULT_LLM_MODEL = "Qwen/Qwen3-4B-Instruct-2507"
DEFAULT_LLM_API_BASE = "http://localhost:8000/v1"


class Settings(BaseModel):
    """Application and Module A configuration settings."""

    app_env: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Current runtime environment",
    )
    app_name: str = Field(
        default="doc-extraction-engine",
        description="System application name",
    )
    service_name: str = Field(
        default="module-a-ingestion",
        description="Name of the service/module",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging verbosity level",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug mode (disallowed in production)",
    )

    # Security & Egress Guardrail (ARC-001, SEC-011)
    allow_external_egress: bool = Field(
        default=False,
        description="Strictly false to preserve private on-prem zero-egress boundary",
    )

    # Ingestion Constraints (FR-ING-001, FR-ING-002, GR-001)
    max_file_size_bytes: int = Field(
        default=50 * 1024 * 1024,  # 50 MB
        gt=0,
        description="Maximum upload size per file in bytes",
    )
    max_page_count: int = Field(
        default=100,
        gt=0,
        description="Maximum page limit per document",
    )
    allowed_mime_types: List[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/tiff",
        ],
        description="Supported MIME types for MVP (PDF, JPG, PNG, TIFF)",
    )

    # Rendering standard (FR-ING-004, SCH-013)
    target_render_dpi: int = Field(
        default=200,
        description="Standard rendering DPI required by the PRD for layout and bbox consistency",
    )

    # Storage paths (ARC-003, SEC-005)
    storage_base_dir: Path = Field(
        default=Path("./data/storage"),
        description="Local directory for tenant-isolated document & page storage",
    )
    enable_storage_encryption: bool = Field(
        default=False,
        description="Whether AES-256 storage encryption is enabled",
    )

    # Malware scanning policy (FR-ING-003, SEC-009)
    malware_scan_enabled: bool = Field(
        default=True,
        description="Enforce pre-processing malware scanning",
    )
    malware_scanner_type: str = Field(
        default="mock",
        description="Scanner backend (mock, clamav_socket, offline_bundle)",
    )

    # Module B OCR / layout parser backend (FR-PRS-001, FR-PRS-005, ARC-002)
    ocr_backend: Literal["passthrough", "pdfium", "none"] = Field(
        default="passthrough",
        description=(
            "Module B parser backend. 'passthrough' (default): explicit no-OCR mode - empty layouts, "
            "page images preserved for the vision model, UNKNOWN classification and review required; needs no "
            "OCR runtime. "
            "'pdfium': native PDF text only (no OCR; fails on scanned pages and images). "
            "'none': explicit no-parser mode; the parser reports non-operational and Module B fails closed."
        ),
    )

    # Local LLM Extraction Configuration (Module C / PRD ARC-004, ARC-005)
    llm_model_name: str = Field(
        default=DEFAULT_LLM_MODEL,
        description="Model id as served by vLLM (the `vllm serve <model>` name)",
    )
    llm_api_base: str = Field(
        default=DEFAULT_LLM_API_BASE,
        description="vLLM OpenAI-compatible base URL; a public host needs https and VLLM_API_KEY",
    )
    llm_provider: str = Field(
        default="vllm",
        description="LLM serving backend ('vllm')",
    )
    llm_use_inference: bool = Field(
        default=False,
        description="Whether to execute live LLM inference in Module C (false by default for offline testing)",
    )
    llm_allow_fallback: bool = Field(
        default=False,
        description="Strict no-fallback policy: fail closed if LLM extraction fails",
    )
    llm_timeout_seconds: float = Field(
        default=120.0,
        gt=0.0,
        description="Local inference request timeout in seconds",
    )

    @field_validator("allow_external_egress")
    @classmethod
    def validate_no_egress_in_production(cls, v: bool, info) -> bool:
        """Fail closed if external egress is attempted in production or staging."""
        # Per ARC-001 & SEC-011: Data must never leave customer boundary
        if v:
            raise ValueError(
                "External egress (allow_external_egress=True) violates ARC-001/SEC-011. "
                "The engine operates strictly inside the customer VPC/on-prem boundary."
            )
        return v

    @field_validator("llm_api_base")
    @classmethod
    def validate_local_llm_endpoint(cls, v: Optional[str], info) -> Optional[str]:
        """Guardrail: Disallow public cloud LLM endpoints to preserve zero-egress boundary (ARC-001, SEC-011)."""
        if not v:
            return v
        v_clean = v.strip().lower()
        cloud_endpoints = (
            "api.openai.com",
            "api.anthropic.com",
            "api.cohere.ai",
            "generativelanguage.googleapis.com",
            "bedrock.amazonaws.com",
        )
        if any(cloud in v_clean for cloud in cloud_endpoints):
            raise ValueError(
                f"Configured llm_api_base '{v}' targets a public cloud provider, "
                "violating on-prem zero-egress policy (ARC-001, SEC-011)."
            )
        parts = urllib.parse.urlsplit(v_clean)
        if parts.scheme == "http" and parts.hostname and not is_private_host(parts.hostname):
            raise ValueError(f"Configured llm_api_base host '{parts.hostname}' is public: use https.")
        return v

    @field_validator("target_render_dpi")
    @classmethod
    def validate_render_dpi(cls, v: int) -> int:
        """PRD specifications (FR-ING-004, SCH-013) explicitly demand 200 DPI."""
        if v != 200:
            raise ValueError(
                f"Invalid target_render_dpi: {v}. PRD requirements (FR-ING-004, SCH-013) "
                "strictly mandate 200 DPI for layout analysis and coordinate alignment."
            )
        return v


def load_settings(
    env_overrides: Optional[dict] = None,
) -> Settings:
    """
    Load and validate settings with safe error translation.
    
    Raises:
        ConfigurationError: When supplied settings fail validation.
    """
    try:
        data = env_overrides or {}
        return Settings(**data)
    except Exception as exc:
        raise ConfigurationError(
            message=f"Failed to load or validate configuration: {str(exc)}",
            details={"original_error": str(exc)},
        ) from exc

