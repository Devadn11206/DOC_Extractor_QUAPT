"""
AWS KMS asymmetric audit checkpoint signer (Team Member 3, Phase 4C).

Adheres to:
- PRD SEC-008: Independently verifiable audit checkpoints with KMS-managed asymmetric signing keys.
- Private Key Protection: Private signing keys remain inside KMS hardware security modules.
- Bounded Operations & Typed Errors: Strict timeouts, standard retry policies, sanitized errors.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSLError,
)
from cryptography.hazmat.primitives import serialization

from doc_extractor.infrastructure.audit.errors import CheckpointSigningError
from doc_extractor.infrastructure.audit.models import AuditCheckpoint
from doc_extractor.infrastructure.audit.signer import AuditCheckpointSigner

logger = logging.getLogger("doc_extractor.infrastructure.audit.kms")


class AwsKmsAuditSigner(AuditCheckpointSigner):
    """
    AWS KMS adapter for asymmetric checkpoint signing.
    Delegates RSASSA-PSS signing to AWS KMS asymmetric signing keys.
    """

    def __init__(
        self,
        key_id: str,
        key_version: str = "v1",
        algorithm: str = "RSASSA_PSS_SHA_256",
        endpoint_url: Optional[str] = None,
        region_name: str = "us-east-1",
        client: Optional[Any] = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 10.0,
        max_retries: int = 3,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        app_env: str = "production",
    ) -> None:
        self.key_id = key_id
        self.key_version = key_version
        self.algorithm = algorithm
        self.endpoint_url = endpoint_url
        self.region_name = region_name
        self.app_env = app_env

        self._injected_client = client
        self._client_instance: Optional[Any] = None

        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._max_retries = max_retries
        self._verify_ssl = verify_ssl
        self._ca_bundle = ca_bundle

        self._public_key_cache: Dict[str, bytes] = {}

    @property
    def client(self) -> Any:
        """Lazily initialized boto3 KMS client."""
        if self._injected_client is not None:
            return self._injected_client
        if self._client_instance is None:
            config = Config(
                connect_timeout=self._connect_timeout,
                read_timeout=self._read_timeout,
                retries={"max_attempts": self._max_retries, "mode": "standard"},
            )
            session = boto3.session.Session()
            self._client_instance = session.client(
                service_name="kms",
                endpoint_url=self.endpoint_url,
                region_name=self.region_name,
                verify=self._ca_bundle if self._ca_bundle else self._verify_ssl,
                config=config,
            )
        return self._client_instance

    def _map_error(self, exc: Exception, operation: str = "kms_signing") -> Exception:
        """Sanitizes AWS KMS errors into typed domain exceptions without leaking secrets."""
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NotFoundException", "KeyUnavailableException", "DisabledException"):
                return CheckpointSigningError("signing_key_not_found_or_disabled", details={"operation": operation})
            if code in ("AccessDeniedException", "UnauthorizedException"):
                return CheckpointSigningError("signing_access_denied", details={"operation": operation})
            if code in ("KMSInternalException", "InternalErrorException", "DependencyTimeoutException"):
                return CheckpointSigningError("kms_service_unavailable", details={"operation": operation})
            return CheckpointSigningError("kms_client_error", details={"operation": operation})
        if isinstance(exc, EndpointConnectionError):
            return CheckpointSigningError("kms_endpoint_unreachable", details={"operation": operation})
        if isinstance(exc, ConnectTimeoutError):
            return CheckpointSigningError("kms_connect_timeout", details={"operation": operation})
        if isinstance(exc, ReadTimeoutError):
            return CheckpointSigningError("kms_read_timeout", details={"operation": operation})
        if isinstance(exc, SSLError):
            return CheckpointSigningError("kms_ssl_validation_failed", details={"operation": operation})
        if isinstance(exc, BotoCoreError):
            return CheckpointSigningError("kms_botocore_error", details={"operation": operation})
        return CheckpointSigningError(f"kms_signing_failed ({operation})")

    def sign(self, checkpoint: AuditCheckpoint) -> AuditCheckpoint:
        data = checkpoint.canonical_bytes()
        try:
            resp = self.client.sign(
                KeyId=self.key_id,
                Message=data,
                MessageType="RAW",
                SigningAlgorithm=self.algorithm,
            )
            signature_bytes = resp["Signature"]
            return checkpoint.with_signature(signature_bytes)
        except Exception as exc:
            raise self._map_error(exc, "sign_checkpoint") from exc

    def get_public_key(self, key_id: Optional[str] = None, key_version: Optional[str] = None) -> bytes:
        target_key = key_id or self.key_id
        target_ver = key_version or self.key_version
        cache_key = f"{target_key}#{target_ver}"

        if cache_key in self._public_key_cache:
            return self._public_key_cache[cache_key]

        try:
            resp = self.client.get_public_key(KeyId=target_key)
            der_bytes = resp["PublicKey"]
            parsed_key = serialization.load_der_public_key(der_bytes)
            pem_bytes = parsed_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            self._public_key_cache[cache_key] = pem_bytes
            return pem_bytes
        except Exception as exc:
            raise self._map_error(exc, "get_public_key") from exc

    def get_key_id(self) -> str:
        return self.key_id

    def get_key_version(self) -> str:
        return self.key_version
