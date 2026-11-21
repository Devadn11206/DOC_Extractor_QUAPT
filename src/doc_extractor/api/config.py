"""
Centralized configuration for the Document Extraction Engine REST API (Module M5).

Adheres to:
- PRD §10.1 ARC-001 / SEC-011: Strict zero-egress VPC/on-prem boundary.
- Production safety: Disallow debug mode in production environments.
"""

import os
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional
from pydantic import Field, field_validator, model_validator
from doc_extractor.config import Settings
from doc_extractor.exceptions import ConfigurationError


# The API's own deployment posture comes from the environment (the engine's Settings reads none; see .env.example).
# An explicit allow-list: only these variables are read, so no unrelated DOC_* variable can change the API, and any other
# DOC_API_* name is refused (a typo such as DOC_API_EVN=production must not silently leave the API in development).
ENV_VARIABLES: Dict[str, str] = {
    "DOC_API_ENV": "app_env",                              # development | staging | production
    "DOC_API_DEBUG": "debug",
    "DOC_API_DOCS_ENABLED": "docs_enabled",                # true/false; default: on in development only
    "DOC_API_CORS_ORIGINS": "cors_origins",                # comma separated
    "DOC_API_CORS_ALLOW_CREDENTIALS": "cors_allow_credentials",
    "DOC_API_PREFIX": "api_prefix",
    "DOC_API_ALLOW_CLIENT_REQUEST_ID": "allow_client_request_id",
    "DOC_API_LOG_LEVEL": "log_level",
    "DOC_API_STORAGE_BASE_DIR": "storage_base_dir",
    "DOC_API_MAX_CONCURRENT_UPLOADS": "max_concurrent_uploads",
    "DOC_API_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS": "upload_body_idle_timeout_seconds",
    "DOC_API_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS": "upload_body_total_timeout_seconds",
    "DOC_API_MAX_CONCURRENT_EXTRACTIONS": "max_concurrent_extractions",
    "DOC_API_EXTRACT_SYNC_TIMEOUT_SECONDS": "extract_sync_timeout_seconds",
}
_LIST_FIELDS = frozenset({"cors_origins"})
# Upper bounds of the upload body timeouts (seconds). A timeout that is infinite or absurdly long would let one silent
# client hold an upload admission permit indefinitely, which is what the timeouts exist to prevent.
MAX_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS = 300.0
MAX_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS = 3600.0
# PRD API-004: the synchronous extraction endpoint times out after 30 s. That is also the ceiling of the setting.
EXTRACT_SYNC_TIMEOUT_SECONDS = 30.0
MAX_CONCURRENT_EXTRACTIONS_LIMIT = 64
ENV_PREFIX = "DOC_API_"
ENVIRONMENT_VARIABLE = "DOC_API_ENV"
ENVIRONMENT_CHOICES = ("development", "staging", "production")


def settings_from_environment(environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """
    The ApiSettings values supplied by ``DOC_API_*`` environment variables (raw strings; ApiSettings validates them).
    Fails closed with ConfigurationError for an unknown ``DOC_API_*`` name or a variable that is set but empty.
    """
    env = os.environ if environ is None else environ
    unknown = sorted(name for name in env if name.startswith(ENV_PREFIX) and name not in ENV_VARIABLES)
    if unknown:
        raise ConfigurationError(
            message=f"Unknown API setting(s) in the environment: {', '.join(unknown)}. Known: {', '.join(sorted(ENV_VARIABLES))}.",
            details={"unknown_variables": unknown},
        )
    data: Dict[str, Any] = {}
    for variable, field_name in ENV_VARIABLES.items():
        if variable not in env:
            continue
        raw = env[variable].strip()
        if not raw:
            raise ConfigurationError(
                message=f"{variable} is set but empty; unset it or give it a value.",
                details={"variable": variable},
            )
        data[field_name] = [item.strip() for item in raw.split(",") if item.strip()] if field_name in _LIST_FIELDS else raw
    return data


class ApiSettings(Settings):
    """Configuration settings for the REST API application."""

    service_name: str = Field(
        default="doc-extractor-api",
        description="Name of the REST API service",
    )
    api_prefix: str = Field(
        default="/api/v1",
        description="Global route prefix for REST API endpoints (PRD §7.1)",
    )
    api_title: str = Field(
        default="Document Extraction Engine REST API",
        description="OpenAPI specification title",
    )
    api_version: str = Field(
        default="1.0.0",
        description="Semantic version of the REST API",
    )
    api_description: str = Field(
        default="On-premise schema-guided document extraction engine REST API.",
        description="OpenAPI specification description",
    )
    docs_url: Optional[str] = Field(
        default="/docs",
        description="Swagger UI documentation route (set None to disable in hardened environments)",
    )
    redoc_url: Optional[str] = Field(
        default="/redoc",
        description="ReDoc documentation route (set None to disable)",
    )
    openapi_url: Optional[str] = Field(
        default="/openapi.json",
        description="OpenAPI JSON specification route",
    )
    request_id_header: str = Field(
        default="X-Request-ID",
        description="HTTP header name used for request correlation (API-010)",
    )
    allow_client_request_id: bool = Field(
        default=True,
        description="Whether strictly validated client-provided request IDs are accepted",
    )
    cors_origins: List[str] = Field(
        default_factory=list,
        description="Allowed origins for CORS (default empty for private intranet)",
    )
    cors_allow_credentials: bool = Field(
        default=True,
        description="Whether to support credentials for CORS requests",
    )
    docs_enabled: Optional[bool] = Field(
        default=None,
        description=(
            "Serve the interactive API documentation (/docs, /redoc, /openapi.json). None (default) = on in development "
            "only; it can never be enabled in production."
        ),
    )
    max_concurrent_uploads: int = Field(
        default=2,
        ge=1,
        description="Maximum concurrent upload requests admitted before HTTP 429",
    )
    upload_body_idle_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        le=MAX_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS,
        allow_inf_nan=False,
        description=(
            "PROVISIONAL. Longest an upload client may send NO body bytes before the request is answered with HTTP 408 and "
            "its admission permit is released. Resets only when body bytes actually arrive. Finite, greater than 0 and at "
            "most 300 seconds. The interval is measured between receive calls, so it can include server-side multipart "
            "parsing or spooling delays."
        ),
    )
    upload_body_total_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        le=MAX_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS,
        allow_inf_nan=False,
        description=(
            "PROVISIONAL. Longest the complete upload body may take to arrive (from admission until the last body byte); "
            "HTTP 408 afterwards. Module A processing time is not counted. Finite, greater than 0, at most 3600 seconds and "
            "not smaller than the idle timeout. The default allows the default 50 MiB limit at about 170 KiB/s on average."
        ),
    )

    max_concurrent_extractions: int = Field(
        default=2,
        ge=1,
        le=MAX_CONCURRENT_EXTRACTIONS_LIMIT,
        description=(
            "PROVISIONAL. Maximum extract-sync executions running in this process at the same time. A slot stays occupied "
            "until the underlying worker thread really finishes, even after the request timed out. Further requests are "
            "answered at once with HTTP 429; nothing is queued."
        ),
    )
    extract_sync_timeout_seconds: float = Field(
        default=EXTRACT_SYNC_TIMEOUT_SECONDS,
        gt=0,
        le=EXTRACT_SYNC_TIMEOUT_SECONDS,
        allow_inf_nan=False,
        description="Deadline of POST /documents/{id}/extract-sync (PRD API-004: 30 s). Finite, greater than 0, at most 30.",
    )

    @model_validator(mode="after")
    def validate_production_guards(self) -> "ApiSettings":
        """Enforces security hardening in production/staging environments and CORS constraints."""
        if self.app_env in ("production", "staging") and self.debug:
            raise ValueError("Debug mode must be disabled in production and staging environments.")
        if "*" in self.cors_origins and self.cors_allow_credentials:
            raise ValueError(
                "Wildcard CORS origin ('*') cannot be combined with credentials (allow_credentials=True). "
                "Specify explicit origins to prevent cross-origin credential reflection."
            )
        if self.app_env in ("production", "staging") and "*" in self.cors_origins:
            raise ValueError("Wildcard CORS origin ('*') is strictly prohibited in production and staging environments.")
        if self.upload_body_total_timeout_seconds < self.upload_body_idle_timeout_seconds:
            raise ValueError("upload_body_total_timeout_seconds must not be smaller than upload_body_idle_timeout_seconds.")
        if self.docs_enabled is True and self.app_env == "production":
            raise ValueError("Interactive API documentation must not be enabled in production.")
        # Development-only documentation: off in staging and production unless staging opts in explicitly.
        documentation_on = self.docs_enabled if self.docs_enabled is not None else self.app_env == "development"
        if not documentation_on:
            self.docs_url = None
            self.redoc_url = None
            self.openapi_url = None
        return self

    def to_engine_settings(self) -> Settings:
        """Create a validated engine Settings instance from this ApiSettings instance."""
        return bridge_to_engine_settings(self)

    @classmethod
    def from_engine_settings(
        cls,
        engine_settings: Settings,
        **api_overrides: Any,
    ) -> "ApiSettings":
        """Create an ApiSettings instance bridged from an engine Settings instance."""
        return bridge_to_api_settings(engine_settings, **api_overrides)


def bridge_to_engine_settings(api_settings: ApiSettings) -> Settings:
    """
    Explicit, validated bridge creating an engine Settings instance from ApiSettings.
    Avoids duplicate or conflicting configuration values while validating runtime safety.
    Fails closed if the configuration violates security boundaries (ARC-001/SEC-011).
    """
    if not isinstance(api_settings, ApiSettings):
        raise ConfigurationError(
            message="Cannot bridge to engine settings: input must be an ApiSettings instance.",
            details={"received_type": type(api_settings).__name__},
        )

    # ARC-001 / SEC-011 guardrail: strictly private VPC zero-egress boundary
    if api_settings.allow_external_egress:
        raise ConfigurationError(
            message="Cannot bridge to engine settings: external egress is enabled (ARC-001/SEC-011).",
            details={"allow_external_egress": True},
        )

    # Production safety guardrail: debug disallowed in production / staging
    if api_settings.app_env in ("production", "staging") and api_settings.debug:
        raise ConfigurationError(
            message=f"Cannot bridge to engine settings: debug mode disallowed in {api_settings.app_env}.",
            details={"app_env": api_settings.app_env, "debug": True},
        )

    engine_fields = set(Settings.model_fields.keys())
    data: Dict[str, Any] = {}
    for field_name in engine_fields:
        if hasattr(api_settings, field_name):
            data[field_name] = getattr(api_settings, field_name)

    try:
        return Settings(**data)
    except Exception as exc:
        raise ConfigurationError(
            message=f"Failed to bridge ApiSettings to engine Settings: {str(exc)}",
            details={"error": str(exc)},
        ) from exc


def bridge_to_api_settings(
    engine_settings: Settings,
    **api_overrides: Any,
) -> ApiSettings:
    """
    Explicit, validated bridge creating an ApiSettings instance from an engine Settings instance.
    Preserves engine configuration while allowing API-specific overrides.
    """
    if not isinstance(engine_settings, Settings):
        raise ConfigurationError(
            message="Cannot bridge to API settings: input must be an engine Settings instance.",
            details={"received_type": type(engine_settings).__name__},
        )

    data = engine_settings.model_dump()
    data.update(api_overrides)
    try:
        return ApiSettings(**data)
    except Exception as exc:
        raise ConfigurationError(
            message=f"Failed to bridge engine Settings to ApiSettings: {str(exc)}",
            details={"error": str(exc)},
        ) from exc


def load_api_settings(env_overrides: Optional[Dict[str, Any]] = None) -> ApiSettings:
    """
    Load and validate API settings with safe error translation.

    Values come from the ``DOC_API_*`` environment variables (``settings_from_environment``); ``env_overrides`` passed in
    code win over the environment.

    The deployment environment is NEVER assumed: ``DOC_API_ENV`` (or an explicit ``app_env`` passed in code) is required,
    so a deployment that forgot to set it stops at start-up instead of silently running as ``development`` (interactive
    docs on, permissive guards). Code that builds ``ApiSettings(app_env=...)`` itself, as the tests do, is unaffected.

    Raises:
        ConfigurationError: When the environment or the supplied settings fail validation, or no environment is named.
    """
    try:
        data = {**settings_from_environment(), **(env_overrides or {})}
        if "app_env" not in data:
            raise ConfigurationError(
                message=(
                    f"{ENVIRONMENT_VARIABLE} is not set. Set it explicitly to one of {', '.join(ENVIRONMENT_CHOICES)}: "
                    "the API never assumes an environment."
                ),
                details={"variable": ENVIRONMENT_VARIABLE, "allowed": list(ENVIRONMENT_CHOICES)},
            )
        return ApiSettings(**data)
    except ConfigurationError:
        raise                                       # already typed (unknown/empty DOC_API_* variable): keep its details
    except Exception as exc:
        raise ConfigurationError(
            message=f"Failed to load or validate API configuration: {str(exc)}",
            details={"original_error": str(exc)},
        ) from exc


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    """Dependency injection provider for API settings."""
    return load_api_settings()
