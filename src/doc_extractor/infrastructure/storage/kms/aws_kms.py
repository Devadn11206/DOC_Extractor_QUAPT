"""
AWS KMS key management provider (Team Member 3, Phase 4B).

Adheres to:
- PRD SEC-005: Cloud/hybrid envelope encryption with keys held in a KMS/HSM.
- Server-side DEK rewrapping: Uses KMS ReEncrypt to rotate KEKs without plaintext exposure.
- Bounded operations: Explicit connect/read timeouts and standard botocore retry policies.
- Sanitized errors: Raw keys, credential headers, and cryptographic material are never echoed.
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

from doc_extractor.infrastructure.storage.kms.base import DataKey, KmsProvider
from doc_extractor.infrastructure.storage.kms.errors import (
    KmsAuthenticationError,
    KmsError,
    KmsKeyNotFoundError,
    KmsUnavailableError,
)

logger = logging.getLogger("doc_extractor.infrastructure.storage.kms")


class AwsKmsProvider(KmsProvider):
    """
    AWS KMS adapter for envelope encryption.
    Delegates DEK generation, decryption, and rewrapping to AWS KMS hardware security modules.
    """

    def __init__(
        self,
        kek_id: str,
        key_version: str = "v1",
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
        self.kek_id = kek_id
        self.key_version = key_version
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

    def _map_error(self, exc: Exception, operation: str = "kms_operation") -> Exception:
        """Sanitize and map AWS KMS errors to typed domain errors."""
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}) .get("Code", "")
            if code in ("NotFoundException", "KeyUnavailableException", "DisabledException"):
                return KmsKeyNotFoundError("kms_key_not_found_or_disabled", details={"operation": operation})
            if code in ("AccessDeniedException", "UnauthorizedException"):
                return KmsAuthenticationError("kms_access_denied", details={"operation": operation})
            if code in ("KMSInternalException", "InternalErrorException", "DependencyTimeoutException"):
                return KmsUnavailableError("kms_service_unavailable", details={"operation": operation})
            return KmsError("kms_client_error", details={"operation": operation})
        if isinstance(exc, EndpointConnectionError):
            return KmsUnavailableError("kms_endpoint_unreachable", details={"operation": operation})
        if isinstance(exc, ConnectTimeoutError):
            return KmsUnavailableError("kms_connect_timeout", details={"operation": operation})
        if isinstance(exc, ReadTimeoutError):
            return KmsUnavailableError("kms_read_timeout", details={"operation": operation})
        if isinstance(exc, SSLError):
            return KmsUnavailableError("kms_ssl_validation_failed", details={"operation": operation})
        if isinstance(exc, BotoCoreError):
            return KmsUnavailableError("kms_botocore_error", details={"operation": operation})
        return KmsError(f"kms_operation_failed ({operation})")

    def generate_data_key(
        self,
        kek_id: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> DataKey:
        target_kek = kek_id or self.kek_id
        kwargs: Dict[str, Any] = {"KeyId": target_kek, "KeySpec": "AES_256"}
        if encryption_context:
            kwargs["EncryptionContext"] = encryption_context

        try:
            resp = self.client.generate_data_key(**kwargs)
            return DataKey(
                plaintext_key=resp["Plaintext"],
                encrypted_key=resp["CiphertextBlob"],
                kek_id=resp.get("KeyId", target_kek),
                key_version=self.key_version,
            )
        except Exception as exc:
            raise self._map_error(exc, "generate_data_key") from exc

    def decrypt_data_key(
        self,
        encrypted_key: bytes,
        kek_id: Optional[str] = None,
        key_version: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> bytes:
        target_kek = kek_id or self.kek_id
        kwargs: Dict[str, Any] = {"CiphertextBlob": encrypted_key, "KeyId": target_kek}
        if encryption_context:
            kwargs["EncryptionContext"] = encryption_context

        try:
            resp = self.client.decrypt(**kwargs)
            return resp["Plaintext"]
        except Exception as exc:
            raise self._map_error(exc, "decrypt_data_key") from exc

    def rewrap_data_key(
        self,
        encrypted_key: bytes,
        source_kek_id: Optional[str] = None,
        target_kek_id: Optional[str] = None,
        source_version: Optional[str] = None,
        target_version: Optional[str] = None,
        encryption_context: Optional[Dict[str, str]] = None,
    ) -> DataKey:
        src_kek = source_kek_id or self.kek_id
        tgt_kek = target_kek_id or self.kek_id
        kwargs: Dict[str, Any] = {
            "CiphertextBlob": encrypted_key,
            "DestinationKeyId": tgt_kek,
        }
        if src_kek:
            kwargs["SourceKeyId"] = src_kek
        if encryption_context:
            kwargs["SourceEncryptionContext"] = encryption_context
            kwargs["DestinationEncryptionContext"] = encryption_context

        try:
            resp = self.client.re_encrypt(**kwargs)
            return DataKey(
                plaintext_key=b"",  # Re-wrapped key doesn't expose plaintext
                encrypted_key=resp["CiphertextBlob"],
                kek_id=tgt_kek,
                key_version=target_version or self.key_version,
            )
        except Exception as exc:
            raise self._map_error(exc, "rewrap_data_key") from exc

    def get_current_key_id(self) -> str:
        return self.kek_id

    def get_current_key_version(self) -> str:
        return self.key_version

    def check_health(self) -> Dict[str, Any]:
        try:
            resp = self.client.describe_key(KeyId=self.kek_id)
            meta = resp.get("KeyMetadata", {})
            enabled = meta.get("Enabled", False)
            if not enabled:
                raise KmsKeyNotFoundError("kms_key_disabled")
            return {
                "status": "ok",
                "provider": "aws_kms",
                "kek_id": self.kek_id,
                "arn": meta.get("Arn"),
                "state": meta.get("KeyState"),
            }
        except Exception as exc:
            raise self._map_error(exc, "check_health") from exc
