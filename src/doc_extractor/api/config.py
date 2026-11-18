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
}
_LIST_FIELDS = frozenset({"cors_origins"})
ENV_PREFIX = "DOC_API_"


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
        if self.docs_enabled is True and self.app_env == "production":
            raise ValueError("Interactive API documentation must not be enabled in production.")
        # Development-only documentation: off in staging and production unless staging opts in explicitly.
        documentation_on = self.docs_enabled if self.docs_enabled is not None else self.app_env == "development"
        if not documentation_on:
            self.docs_url = None
            self.redoc_url = None
            self.openapi_url = None
        return self


def load_api_settings(env_overrides: Optional[Dict[str, Any]] = None) -> ApiSettings:
    """
    Load and validate API settings with safe error translation.

    Values come from the ``DOC_API_*`` environment variables (``settings_from_environment``); ``env_overrides`` passed in
    code win over the environment.

    Raises:
        ConfigurationError: When the environment or the supplied settings fail validation.
    """
    try:
        data = {**settings_from_environment(), **(env_overrides or {})}
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
