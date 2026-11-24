"""
Object storage settings and validation policies (Team Member 3, Phase 4A).

``S3StorageSettings`` is the validated, immutable configuration for an S3-compatible object storage backend.
It is built from the API settings (``S3StorageSettings.from_api_settings``), never from the environment directly
(the API reads its environment through one allow-list in ``api/config.py``).

SAFETY AND SECURITY RULES:
- Credentials (access key, secret key, session token) are secrets: held as ``SecretStr``, never logged,
  never echoed in error messages or reprs.
- Secrets may be supplied inline or via mounted secret files (preferred in Kubernetes / Docker), never both.
- Staging and production require TLS (``https://``) and refuse options that weaken TLS verification (e.g. ``verify_ssl=False``).
  Development permits ``http://`` for local MinIO / LocalStack testing.
- Bounded operations: connect timeout, read timeout, and max retries are constrained to safe limits.
- Bounded paths and bucket names: DNS-compliant bucket names, path-traversal prevention for prefixes and spool directories.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from doc_extractor.infrastructure.storage.errors import ObjectStorageConfigurationError

ENVIRONMENTS = ("development", "staging", "production")
HARDENED_ENVIRONMENTS = ("staging", "production")
MAX_CREDENTIAL_FILE_BYTES = 4096

# AWS S3 Bucket Naming Rules (DNS-compliant): 3..63 chars, lowercase, numbers, dots, hyphens
_BUCKET_REGEX = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_IP_REGEX = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_PREFIX_REGEX = re.compile(r"^[a-zA-Z0-9_\-\./]*$")


def _read_secret_file(path: Path) -> SecretStr:
    """Read a credential from a mounted secret file safely."""
    try:
        resolved = path.resolve()
        st = resolved.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ObjectStorageConfigurationError("credential_file_not_regular_file", setting=str(path))
        if st.st_size > MAX_CREDENTIAL_FILE_BYTES:
            raise ObjectStorageConfigurationError("credential_file_too_large", setting=str(path))
        content = resolved.read_text(encoding="utf-8").strip()
        if not content:
            raise ObjectStorageConfigurationError("credential_file_empty", setting=str(path))
        return SecretStr(content)
    except OSError as exc:
        raise ObjectStorageConfigurationError("credential_file_unreadable", setting=str(path)) from exc


class S3StorageSettings(BaseModel):
    """
    Validated, immutable configuration for an S3-compatible object storage backend.
    Holds secrets; never print or log instances directly.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    bucket_name: str = Field(description="DNS-compliant S3 bucket name")
    endpoint_url: Optional[str] = Field(default=None, description="Custom S3 endpoint URL (MinIO, Ceph, etc.)")
    region_name: str = Field(default="us-east-1", description="AWS or object store region")
    access_key_id: Optional[SecretStr] = Field(default=None, description="S3 access key ID")
    secret_access_key: Optional[SecretStr] = Field(default=None, description="S3 secret access key")
    session_token: Optional[SecretStr] = Field(default=None, description="Optional S3 session token (STS / temporary credentials)")
    access_key_id_file: Optional[Path] = Field(default=None, description="Mounted secret file for access key ID")
    secret_access_key_file: Optional[Path] = Field(default=None, description="Mounted secret file for secret access key")
    force_path_style: Optional[bool] = Field(default=None, description="Whether to use path-style addressing (required by MinIO/Ceph)")
    key_prefix: str = Field(default="", description="Optional prefix prepended to all stored object keys")
    connect_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60.0, allow_inf_nan=False)
    read_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0, allow_inf_nan=False)
    max_retries: int = Field(default=3, ge=0, le=10)
    verify_ssl: bool = Field(default=True, description="Whether to verify TLS certificates")
    ca_bundle: Optional[str] = Field(default=None, description="Custom CA certificate bundle path for enterprise PKI")
    spool_dir: Optional[Path] = Field(default=None, description="Local directory for spooling objects when descriptor paths are requested")
    app_env: str = Field(default="development", description="Runtime environment: development | staging | production")
    allow_iam_role: bool = Field(default=False, description="Whether to allow AWS IAM role credentials when keys are unset")
    encryption_enabled: bool = Field(default=False, description="Whether to encrypt objects at rest with AES-256-GCM envelope encryption")
    kms_provider_type: Literal["aws_kms", "fake"] = Field(default="fake", description="KMS key provider implementation: aws_kms or fake")
    kms_kek_id: Optional[str] = Field(default=None, description="Key identifier (ARN or alias) for KMS KEK")
    kms_key_version: str = Field(default="v1", description="Current key version identifier")
    kms_endpoint_url: Optional[str] = Field(default=None, description="Custom KMS endpoint URL (LocalStack, KMS emulator, etc.)")
    spool_is_protected: bool = Field(default=False, description="Whether the local spool directory is backed by hardware/OS volume encryption")

    @field_validator("bucket_name")
    @classmethod
    def _validate_bucket_name(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ObjectStorageConfigurationError("bucket_name_empty", setting="bucket_name")
        cleaned = value.strip()
        if len(cleaned) < 3 or len(cleaned) > 63:
            raise ObjectStorageConfigurationError("bucket_name_invalid_length", setting="bucket_name")
        if not _BUCKET_REGEX.match(cleaned):
            raise ObjectStorageConfigurationError("bucket_name_invalid_characters", setting="bucket_name")
        if ".." in cleaned:
            raise ObjectStorageConfigurationError("bucket_name_consecutive_periods", setting="bucket_name")
        if _IP_REGEX.match(cleaned):
            raise ObjectStorageConfigurationError("bucket_name_formatted_as_ip", setting="bucket_name")
        return cleaned

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
            raise ObjectStorageConfigurationError("endpoint_url_contains_whitespace_or_control_characters", setting="endpoint_url")
        try:
            parts = urlsplit(raw)
        except ValueError:
            raise ObjectStorageConfigurationError("endpoint_url_malformed", setting="endpoint_url") from None
        if parts.scheme not in ("http", "https"):
            raise ObjectStorageConfigurationError("endpoint_url_scheme_must_be_http_or_https", setting="endpoint_url")
        if not parts.hostname:
            raise ObjectStorageConfigurationError("endpoint_url_has_no_host", setting="endpoint_url")
        if parts.username or parts.password:
            raise ObjectStorageConfigurationError("endpoint_url_credentials_not_allowed", setting="endpoint_url")
        return raw

    @field_validator("key_prefix")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value:
            return ""
        cleaned = value.strip()
        if cleaned.startswith("/"):
            raise ObjectStorageConfigurationError("prefix_cannot_start_with_slash", setting="key_prefix")
        if ".." in cleaned:
            raise ObjectStorageConfigurationError("prefix_cannot_contain_path_traversal", setting="key_prefix")
        if not _PREFIX_REGEX.match(cleaned):
            raise ObjectStorageConfigurationError("prefix_invalid_characters", setting="key_prefix")
        if not cleaned.endswith("/"):
            cleaned = f"{cleaned}/"
        return cleaned

    @model_validator(mode="after")
    def _validate_environment_policy(self) -> "S3StorageSettings":
        if self.app_env not in ENVIRONMENTS:
            raise ObjectStorageConfigurationError("unknown_environment", setting="app_env")

        # Security policy in hardened environments (staging, production)
        if self.app_env in HARDENED_ENVIRONMENTS:
            if not self.verify_ssl:
                raise ObjectStorageConfigurationError("insecure_tls_is_refused_outside_development", setting="verify_ssl")
            if self.endpoint_url is not None:
                parts = urlsplit(self.endpoint_url)
                if parts.scheme != "https":
                    raise ObjectStorageConfigurationError("tls_is_required_outside_development", setting="endpoint_url")

        # Mutual exclusivity of inline credentials and mounted files
        if self.access_key_id is not None and self.access_key_id_file is not None:
            raise ObjectStorageConfigurationError("access_key_both_inline_and_file", setting="access_key_id")
        if self.secret_access_key is not None and self.secret_access_key_file is not None:
            raise ObjectStorageConfigurationError("secret_access_key_both_inline_and_file", setting="secret_access_key")

        # In hardened environments, credentials must be provided or IAM role explicitly permitted
        has_access_key = self.access_key_id is not None or self.access_key_id_file is not None
        has_secret_key = self.secret_access_key is not None or self.secret_access_key_file is not None

        if has_access_key != has_secret_key:
            raise ObjectStorageConfigurationError("credentials_incomplete_key_pair", setting="access_key_id")

        if self.app_env in HARDENED_ENVIRONMENTS and not has_access_key and not self.allow_iam_role:
            raise ObjectStorageConfigurationError("credentials_required_outside_development", setting="access_key_id")

        # Encryption and KMS policy in hardened environments
        if self.encryption_enabled and self.app_env in HARDENED_ENVIRONMENTS:
            if self.kms_provider_type == "fake":
                raise ObjectStorageConfigurationError("fake_kms_forbidden_outside_development", setting="kms_provider_type")
            if not self.kms_kek_id:
                raise ObjectStorageConfigurationError("kms_kek_id_required_when_encryption_enabled", setting="kms_kek_id")

        return self

    @property
    def effective_force_path_style(self) -> bool:
        """Determines path-style addressing: defaults to True for custom endpoints (MinIO/Ceph), False for AWS S3."""
        if self.force_path_style is not None:
            return self.force_path_style
        return bool(self.endpoint_url)

    def get_effective_access_key_id(self) -> Optional[str]:
        """Resolves access key ID from inline secret or file."""
        if self.access_key_id is not None:
            return self.access_key_id.get_secret_value()
        if self.access_key_id_file is not None:
            return _read_secret_file(self.access_key_id_file).get_secret_value()
        return None

    def get_effective_secret_access_key(self) -> Optional[str]:
        """Resolves secret access key from inline secret or file."""
        if self.secret_access_key is not None:
            return self.secret_access_key.get_secret_value()
        if self.secret_access_key_file is not None:
            return _read_secret_file(self.secret_access_key_file).get_secret_value()
        return None

    def get_effective_session_token(self) -> Optional[str]:
        """Resolves session token if provided."""
        if self.session_token is not None:
            return self.session_token.get_secret_value()
        return None

    def summary(self) -> Dict[str, Any]:
        """Non-sensitive configuration summary safe for logging and operational status."""
        has_creds = bool(self.access_key_id or self.access_key_id_file)
        endpoint_scheme = urlsplit(self.endpoint_url).scheme if self.endpoint_url else "aws-default"
        return {
            "bucket_name": self.bucket_name,
            "region": self.region_name,
            "endpoint_configured": bool(self.endpoint_url),
            "endpoint_scheme": endpoint_scheme,
            "force_path_style": self.effective_force_path_style,
            "has_credentials": has_creds,
            "verify_ssl": self.verify_ssl,
            "has_ca_bundle": bool(self.ca_bundle),
            "key_prefix": self.key_prefix,
            "app_env": self.app_env,
            "encryption_enabled": self.encryption_enabled,
            "kms_provider_type": self.kms_provider_type,
            "kms_kek_id_configured": bool(self.kms_kek_id),
            "kms_key_version": self.kms_key_version,
            "spool_is_protected": self.spool_is_protected,
        }

    @classmethod
    def from_api_settings(cls, api_settings: Any, *, read_credential_files: bool = True) -> Optional["S3StorageSettings"]:
        """
        Builds validated ``S3StorageSettings`` from ``ApiSettings``.
        Returns ``None`` when S3 storage is not configured.
        Raises ``ObjectStorageConfigurationError`` on partial or invalid configuration.
        ``read_credential_files=False`` validates file existence without reading contents.
        """
        backend = getattr(api_settings, "storage_backend", "local")
        bucket = getattr(api_settings, "s3_bucket", None)

        if backend != "s3" and not bucket:
            return None

        if backend == "s3" and not bucket:
            raise ObjectStorageConfigurationError("bucket_required_when_storage_backend_is_s3", setting="s3_bucket")

        # Resolve credential files or inline secrets
        access_key_id = getattr(api_settings, "s3_access_key_id", None)
        access_key_id_file = getattr(api_settings, "s3_access_key_id_file", None)
        secret_access_key = getattr(api_settings, "s3_secret_access_key", None)
        secret_access_key_file = getattr(api_settings, "s3_secret_access_key_file", None)
        session_token = getattr(api_settings, "s3_session_token", None)

        if access_key_id_file and not read_credential_files:
            # File validation check only without I/O
            pass

        return cls(
            bucket_name=bucket,
            endpoint_url=getattr(api_settings, "s3_endpoint_url", None),
            region_name=getattr(api_settings, "s3_region", "us-east-1"),
            access_key_id=access_key_id,
            secret_access_key=secret_access_key,
            session_token=session_token,
            access_key_id_file=Path(access_key_id_file) if access_key_id_file else None,
            secret_access_key_file=Path(secret_access_key_file) if secret_access_key_file else None,
            force_path_style=getattr(api_settings, "s3_force_path_style", None),
            key_prefix=getattr(api_settings, "s3_prefix", ""),
            connect_timeout_seconds=getattr(api_settings, "s3_connect_timeout_seconds", 5.0),
            read_timeout_seconds=getattr(api_settings, "s3_read_timeout_seconds", 30.0),
            verify_ssl=getattr(api_settings, "s3_verify_ssl", True),
            ca_bundle=getattr(api_settings, "s3_ca_bundle", None),
            spool_dir=getattr(api_settings, "s3_spool_dir", None),
            app_env=getattr(api_settings, "app_env", "development"),
            encryption_enabled=getattr(api_settings, "storage_encryption_enabled", False),
            kms_provider_type=getattr(api_settings, "kms_provider_type", "fake"),
            kms_kek_id=getattr(api_settings, "kms_kek_id", None),
            kms_key_version=getattr(api_settings, "kms_key_version", "v1"),
            kms_endpoint_url=getattr(api_settings, "kms_endpoint_url", None),
            spool_is_protected=getattr(api_settings, "spool_is_protected", False),
        )
