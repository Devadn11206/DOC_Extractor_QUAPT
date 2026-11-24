"""
Centralized configuration for the Document Extraction Engine REST API (Module M5).

Adheres to:
- PRD §10.1 ARC-001 / SEC-011: Strict zero-egress VPC/on-prem boundary.
- Production safety: Disallow debug mode in production environments.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping, Optional
from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator
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
    # MongoDB (Team Member 3, Phase 2A). The connection string is a secret: prefer the _FILE form (a mounted secret).
    "DOC_API_MONGO_URI": "mongo_uri",
    "DOC_API_MONGO_URI_FILE": "mongo_uri_file",
    "DOC_API_MONGO_DATABASE": "mongo_database",
    "DOC_API_MONGO_REPLICA_SET": "mongo_replica_set",
    "DOC_API_MONGO_SERVER_SELECTION_TIMEOUT_MS": "mongo_server_selection_timeout_ms",
    "DOC_API_MONGO_CONNECT_TIMEOUT_MS": "mongo_connect_timeout_ms",
    "DOC_API_MONGO_SOCKET_TIMEOUT_MS": "mongo_socket_timeout_ms",
    "DOC_API_MONGO_MAX_POOL_SIZE": "mongo_max_pool_size",
    "DOC_API_MONGO_MIN_POOL_SIZE": "mongo_min_pool_size",
    "DOC_API_MONGO_ENSURE_INDEXES": "mongo_ensure_indexes_on_startup",
    # Durable message broker (Team Member 3, Phase 3). The URL is a secret: prefer the _FILE form (a mounted secret).
    "DOC_API_BROKER_URL": "broker_url",
    "DOC_API_BROKER_URL_FILE": "broker_url_file",
    "DOC_API_BROKER_QUEUE_PREFIX": "broker_queue_prefix",
    "DOC_API_BROKER_MAX_IN_FLIGHT": "broker_max_in_flight",
    "DOC_API_BROKER_CONNECT_TIMEOUT_SECONDS": "broker_connect_timeout_seconds",
    "DOC_API_BROKER_PUBLISH_TIMEOUT_SECONDS": "broker_publish_timeout_seconds",
    # S3 Object Storage (Team Member 3, Phase 4A).
    "DOC_API_STORAGE_BACKEND": "storage_backend",
    "DOC_API_S3_BUCKET": "s3_bucket",
    "DOC_API_S3_ENDPOINT_URL": "s3_endpoint_url",
    "DOC_API_S3_REGION": "s3_region",
    "DOC_API_S3_ACCESS_KEY_ID": "s3_access_key_id",
    "DOC_API_S3_ACCESS_KEY_ID_FILE": "s3_access_key_id_file",
    "DOC_API_S3_SECRET_ACCESS_KEY": "s3_secret_access_key",
    "DOC_API_S3_SECRET_ACCESS_KEY_FILE": "s3_secret_access_key_file",
    "DOC_API_S3_SESSION_TOKEN": "s3_session_token",
    "DOC_API_S3_FORCE_PATH_STYLE": "s3_force_path_style",
    "DOC_API_S3_PREFIX": "s3_prefix",
    "DOC_API_S3_CONNECT_TIMEOUT_SECONDS": "s3_connect_timeout_seconds",
    "DOC_API_S3_READ_TIMEOUT_SECONDS": "s3_read_timeout_seconds",
    "DOC_API_S3_VERIFY_SSL": "s3_verify_ssl",
    "DOC_API_S3_CA_BUNDLE": "s3_ca_bundle",
    "DOC_API_S3_SPOOL_DIR": "s3_spool_dir",
    "DOC_API_STORAGE_ENCRYPTION_ENABLED": "storage_encryption_enabled",
    "DOC_API_KMS_PROVIDER_TYPE": "kms_provider_type",
    "DOC_API_KMS_KEK_ID": "kms_kek_id",
    "DOC_API_KMS_KEY_VERSION": "kms_key_version",
    "DOC_API_KMS_ENDPOINT_URL": "kms_endpoint_url",
    "DOC_API_SPOOL_IS_PROTECTED": "spool_is_protected",
    # Audit Checkpoints (Team Member 3, Phase 4C - SEC-008).
    "DOC_API_AUDIT_CHECKPOINT_ENABLED": "audit_checkpoint_enabled",
    "DOC_API_AUDIT_CHECKPOINT_SINK_TYPE": "audit_checkpoint_sink_type",
    "DOC_API_AUDIT_SIGNING_KEY_ID": "audit_signing_key_id",
    "DOC_API_AUDIT_SIGNING_KEY_VERSION": "audit_signing_key_version",
    "DOC_API_AUDIT_OBJECT_LOCK_RETENTION_DAYS": "audit_object_lock_retention_days",
    "DOC_API_AUDIT_OBJECT_LOCK_MODE": "audit_object_lock_mode",
    # JWT & Internal Identity (Team Member 3, Phase 5A - SEC-001, SEC-002, SEC-003).
    "DOC_API_JWT_PUBLIC_KEY_PATH": "jwt_public_key_path",
    "DOC_API_JWT_ISSUER": "jwt_issuer",
    "DOC_API_JWT_AUDIENCE": "jwt_audience",
    "DOC_API_IDENTITY_PROVIDER_ENABLED": "identity_provider_enabled",
    "DOC_API_OIDC_SSO_ENABLED": "oidc_sso_enabled",
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

    # A validation error of ANY field must not echo the raw input: the input dict carries the MongoDB connection string
    # (a secret) as supplied. Error messages keep the field names and the reasons, never the values.
    model_config = ConfigDict(hide_input_in_errors=True)

    rate_limit_capacity: Optional[int] = Field(
        default=None,
        ge=1,
        description="Burst size per principal (API-012). Unset disables rate limiting. Needs redis_url to share the limit.",
    )
    rate_limit_refill_per_second: float = Field(
        default=10.0,
        gt=0,
        description="Sustained requests per second per principal (API-012).",
    )
    audit_store_dir: Optional[str] = Field(
        default=None,
        description=(
            "Directory of the durable, hash-chained audit trail (SEC-008). When set, mandatory audit writes are "
            "fsynced and a failed write fails the operation. Unset: no audit logger is configured."
        ),
    )
    jwt_public_key_path: Optional[str] = Field(
        default=None,
        description="PEM file with the RSA public key that verifies RS256 access tokens (SEC-002). Unset: JWT disabled.",
    )
    jwt_issuer: Optional[str] = Field(default=None, description="Expected JWT 'iss' claim (required with jwt_public_key_path)")
    jwt_audience: Optional[str] = Field(default=None, description="Expected JWT 'aud' claim (required with jwt_public_key_path)")
    # --- MongoDB persistence (Team Member 3, Phase 2A). Validated by doc_extractor.infrastructure.mongo.settings. ---
    mongo_uri: Optional[SecretStr] = Field(
        default=None,
        description="MongoDB connection string (a secret). Mutually exclusive with mongo_uri_file. Unset together with it: no MongoDB.",
    )
    mongo_uri_file: Optional[str] = Field(
        default=None,
        description="File holding the MongoDB connection string (a mounted secret), read at start-up. Mutually exclusive with mongo_uri.",
    )
    mongo_database: Optional[str] = Field(
        default=None,
        description="MongoDB database name. Required whenever a connection string is set; there is no default.",
    )
    mongo_replica_set: Optional[str] = Field(
        default=None,
        description="Expected replica set name. Staging and production always require a replica set; this pins which one.",
    )
    mongo_server_selection_timeout_ms: int = Field(default=5000, ge=100, le=30000, description="Bounded server selection timeout.")
    mongo_connect_timeout_ms: int = Field(default=5000, ge=100, le=30000, description="Bounded connection timeout.")
    mongo_socket_timeout_ms: int = Field(default=30000, ge=1000, le=120000, description="Bounded socket timeout.")
    mongo_max_pool_size: int = Field(default=50, ge=1, le=500, description="Maximum pooled connections.")
    mongo_min_pool_size: int = Field(default=0, ge=0, le=500, description="Minimum pooled connections.")
    mongo_ensure_indexes_on_startup: bool = Field(
        default=True,
        description="Create the declared indexes at start-up (idempotent; never drops an index).",
    )
    # --- Durable message broker (Team Member 3, Phase 3). Validated by doc_extractor.infrastructure.queue.settings. ---
    broker_url: Optional[SecretStr] = Field(
        default=None,
        description="AMQP broker URL (a secret). Mutually exclusive with broker_url_file. Unset together with it: no broker (the queue ports stay unconfigured, 503). Needs MongoDB.",
    )
    broker_url_file: Optional[str] = Field(
        default=None,
        description="File holding the broker URL (a mounted secret), read at start-up. Mutually exclusive with broker_url.",
    )
    broker_queue_prefix: str = Field(default="doc_extractor", description="Prefix of the queue names: lowercase letters, digits, underscores, hyphens.")
    broker_max_in_flight: int = Field(default=10, ge=1, le=100, description="Unacknowledged messages one consumer may hold.")
    broker_connect_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0, allow_inf_nan=False, description="Bounded broker connection timeout.")
    broker_publish_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0, allow_inf_nan=False, description="Bounded broker publish, get and acknowledge timeout.")
    # S3 Object Storage (Team Member 3, Phase 4A).
    storage_backend: Literal["local", "s3"] = Field(
        default="local",
        description="Storage backend: 'local' (filesystem) or 's3' (object storage).",
    )
    s3_bucket: Optional[str] = Field(
        default=None,
        description="S3 bucket name. Required when storage_backend is 's3'.",
    )
    s3_endpoint_url: Optional[str] = Field(
        default=None,
        description="Custom S3 endpoint URL (MinIO, Ceph, etc.).",
    )
    s3_region: str = Field(
        default="us-east-1",
        description="S3 region name.",
    )
    s3_access_key_id: Optional[SecretStr] = Field(
        default=None,
        description="S3 access key ID (a secret).",
    )
    s3_access_key_id_file: Optional[str] = Field(
        default=None,
        description="File containing S3 access key ID (mounted secret).",
    )
    s3_secret_access_key: Optional[SecretStr] = Field(
        default=None,
        description="S3 secret access key (a secret).",
    )
    s3_secret_access_key_file: Optional[str] = Field(
        default=None,
        description="File containing S3 secret access key (mounted secret).",
    )
    s3_session_token: Optional[SecretStr] = Field(
        default=None,
        description="Optional S3 session token (STS / temporary credentials).",
    )
    s3_force_path_style: Optional[bool] = Field(
        default=None,
        description="Force path-style addressing. Defaults to True for custom endpoints, False for AWS.",
    )
    s3_prefix: str = Field(
        default="",
        description="Optional object key prefix prepended to all tenant object keys.",
    )
    s3_connect_timeout_seconds: float = Field(
        default=5.0,
        ge=0.5,
        le=60.0,
        description="Connect timeout for S3 requests.",
    )
    s3_read_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Read timeout for S3 requests.",
    )
    s3_verify_ssl: bool = Field(
        default=True,
        description="Whether to verify TLS certificates (required in staging and production).",
    )
    s3_ca_bundle: Optional[str] = Field(
        default=None,
        description="Custom CA certificate bundle path for TLS verification.",
    )
    s3_spool_dir: Optional[Path] = Field(
        default=None,
        description="Local directory for spooling objects when descriptor paths are requested.",
    )
    storage_encryption_enabled: bool = Field(
        default=False,
        description="Whether to enable AES-256-GCM envelope encryption at rest for object storage.",
    )
    kms_provider_type: Literal["aws_kms", "fake"] = Field(
        default="fake",
        description="KMS key provider implementation: 'aws_kms' or 'fake'.",
    )
    kms_kek_id: Optional[str] = Field(
        default=None,
        description="Key identifier (ARN or alias) for KMS KEK.",
    )
    kms_key_version: str = Field(
        default="v1",
        description="Current key version identifier.",
    )
    kms_endpoint_url: Optional[str] = Field(
        default=None,
        description="Custom KMS endpoint URL (LocalStack, KMS emulator, etc.).",
    )
    spool_is_protected: bool = Field(
        default=False,
        description="Whether the local spool directory is backed by hardware/OS volume encryption.",
    )
    # Audit Checkpoints & Signed Integrity (Team Member 3, Phase 4C - SEC-008).
    audit_checkpoint_enabled: bool = Field(
        default=False,
        description="Whether independently signed audit checkpoints and external storage are enabled.",
    )
    audit_checkpoint_sink_type: Literal["memory", "s3"] = Field(
        default="memory",
        description="External sink type for immutable checkpoint storage: 'memory' or 's3'.",
    )
    audit_signing_key_id: Optional[str] = Field(
        default=None,
        description="Asymmetric KMS signing key identifier (ARN or alias) for audit checkpoints.",
    )
    audit_signing_key_version: str = Field(
        default="v1",
        description="Version identifier for audit checkpoint signing key.",
    )
    audit_object_lock_retention_days: int = Field(
        default=365,
        ge=1,
        description="Retention period in days for S3 Object Lock compliance/governance retention.",
    )
    audit_object_lock_mode: Literal["COMPLIANCE", "GOVERNANCE"] = Field(
        default="COMPLIANCE",
        description="S3 Object Lock retention mode.",
    )
    # Identity & Federation (Team Member 3, Phase 5A - SEC-001, SEC-002, SEC-003).
    identity_provider_enabled: bool = Field(
        default=False,
        description="Whether internal product-owned identity and session lifecycle are enabled.",
    )
    oidc_sso_enabled: bool = Field(
        default=False,
        description="Whether customer OIDC federation and SSO are enabled.",
    )
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
        self._validate_mongo_settings()
        self._validate_broker_settings()
        self._validate_storage_settings()
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

    def _validate_mongo_settings(self) -> None:
        """MongoDB settings: complete, exclusive, and compliant with the connection-string policy for this environment."""
        from doc_extractor.infrastructure.mongo.errors import PersistenceConfigurationError
        from doc_extractor.infrastructure.mongo.settings import MongoSettings

        try:
            # No file I/O while settings are validated: a mounted secret is read when the container is built.
            MongoSettings.from_api_settings(self, read_uri_file=False)
        except PersistenceConfigurationError as exc:
            # The fixed reason only; neither the connection string nor any path is echoed.
            raise ValueError(f"Invalid MongoDB settings: {exc.details.get('reason')}.") from None
        except ValueError as exc:
            # A pydantic error from MongoSettings: its messages are fixed strings written in that module (inputs are hidden).
            errors = getattr(exc, "errors", None)
            reasons = sorted({str(e.get("msg", "")).replace("Value error, ", "") for e in errors()}) if callable(errors) else []
            raise ValueError("Invalid MongoDB settings: " + ("; ".join(reasons) or "a value is not permitted") + ".") from None

    def _validate_broker_settings(self) -> None:
        """Broker settings: complete, exclusive, compliant with the URL policy of this environment, and backed by MongoDB."""
        from doc_extractor.infrastructure.queue.errors import BrokerConfigurationError
        from doc_extractor.infrastructure.queue.settings import BrokerSettings

        try:
            # No file I/O while settings are validated: a mounted secret is read when the container is built.
            configured = BrokerSettings.from_api_settings(self, read_url_file=False)
        except BrokerConfigurationError as exc:
            raise ValueError(f"Invalid broker settings: {exc.details.get('reason')}.") from None
        except ValueError as exc:
            errors = getattr(exc, "errors", None)
            reasons = sorted({str(e.get("msg", "")).replace("Value error, ", "") for e in errors()}) if callable(errors) else []
            raise ValueError("Invalid broker settings: " + ("; ".join(reasons) or "a value is not permitted") + ".") from None
        has_broker = configured is not None or self.broker_url_file is not None
        if has_broker and self.mongo_uri is None and self.mongo_uri_file is None:
            # The lease store (job ownership, attempts, fencing) is the authority on every job and lives in MongoDB: a broker alone would
            # leave duplicate deliveries and dead workers unarbitrated, so there is no broker without it.
            raise ValueError("Invalid broker settings: mongodb_is_required_for_processing_state.")

    def _validate_storage_settings(self) -> None:
        """S3 object storage settings: validate if storage_backend is s3 or bucket is provided."""
        from doc_extractor.infrastructure.storage.errors import ObjectStorageConfigurationError
        from doc_extractor.infrastructure.storage.settings import S3StorageSettings

        try:
            S3StorageSettings.from_api_settings(self, read_credential_files=False)
        except ObjectStorageConfigurationError as exc:
            raise ValueError(f"Invalid storage settings: {exc.details.get('reason')}.") from None
        except ValueError as exc:
            errors = getattr(exc, "errors", None)
            reasons = sorted({str(e.get("msg", "")).replace("Value error, ", "") for e in errors()}) if callable(errors) else []
            raise ValueError("Invalid storage settings: " + ("; ".join(reasons) or "a value is not permitted") + ".") from None

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
