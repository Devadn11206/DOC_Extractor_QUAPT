"""
S3-compatible object storage adapter (Team Member 3, Phase 4A).

Implements the ``StorageBackend`` contract against Amazon S3, MinIO, Ceph, and S3-compatible private cloud storage.

Adheres to:
- PRD ARC-003: Stages store output under run / document IDs in isolated namespaces.
- PRD SEC-005: Object storage foundation for encrypted resting storage (encryption integrated in Phase 4B).
- PRD SEC-006: Strict tenant isolation (collision-proof keys, cross-tenant access prohibited, 404 concealment).
- PRD §7.2 API-010 / API-011: Fixed error messages and machine-readable reasons; no leaked credentials, PII or document text.
- Clean resource handling: Bounded timeouts, streaming cleanup, connection pooling, and atomic local spooling.
- Fail closed: Remote storage errors NEVER silently fall back to local disk or memory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Union

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

from doc_extractor.exceptions import (
    CrossTenantStorageAccessError,
    InvalidTenantIdentifierError,
    StorageError,
    TenantStorageIsolationError,
    TenantStorageUnavailableError,
)
from doc_extractor.infrastructure.storage.envelope import (
    decrypt_envelope,
    encrypt_envelope,
    is_envelope_encrypted,
    rewrap_envelope,
)
from doc_extractor.infrastructure.storage.errors import (
    ObjectNotFoundError,
    ObjectStorageConfigurationError,
    ObjectStorageError,
    ObjectStorageUnavailableError,
)
from doc_extractor.infrastructure.storage.kms.aws_kms import AwsKmsProvider
from doc_extractor.infrastructure.storage.kms.base import KmsProvider
from doc_extractor.infrastructure.storage.kms.fake_kms import FakeKmsProvider
from doc_extractor.infrastructure.storage.settings import S3StorageSettings
from doc_extractor.storage import StorageBackend, StoredArtifact
from doc_extractor.tenancy import ensure_inside_tenant, require_path_safe_tenant_id, tenant_storage_key

logger = logging.getLogger("doc_extractor.infrastructure.storage")


class S3Storage(StorageBackend):
    """
    Durable, tenant-isolated S3-compatible object storage backend.

    Key structure in S3:
    {key_prefix}{tenant_key}/{document_id}/source/original.pdf
    {key_prefix}{tenant_key}/{document_id}/pages/page_{page_number:04d}.png
    {key_prefix}{tenant_key}/{document_id}/preprocessed/page_{page_number:04d}_preprocessed.png
    {key_prefix}{tenant_key}/{document_id}/preprocessed/page_{page_number:04d}_metadata.json
    {key_prefix}{tenant_key}/{document_id}/document_record.json
    {key_prefix}{tenant_key}/{document_id}/module_a_handoff.json

    Logical storage URIs:
    {tenant_id}/{document_id}/source/original.pdf
    {tenant_id}/{document_id}/pages/page_{page_number:04d}.png
    """

    def __init__(
        self,
        settings: S3StorageSettings,
        tenant_id: str = "default_tenant",
        client: Optional[Any] = None,
        kms_provider: Optional[KmsProvider] = None,
    ) -> None:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise InvalidTenantIdentifierError("empty")
        require_path_safe_tenant_id(tenant_id)
        self.settings = settings
        self.tenant_id = tenant_id
        self._tenant_key = tenant_storage_key(tenant_id)
        self.bucket_name = settings.bucket_name
        self._prefix = settings.key_prefix  # Ends with "/" or is empty

        # Injected or lazily constructed boto3 client
        self._injected_client = client
        self._client_instance: Optional[Any] = None

        # KMS provider initialization (Phase 4B)
        self.kms_provider: Optional[KmsProvider] = kms_provider
        if self.kms_provider is None and self.settings.encryption_enabled:
            if self.settings.kms_provider_type == "aws_kms":
                self.kms_provider = AwsKmsProvider(
                    kek_id=self.settings.kms_kek_id or "default-kek",
                    key_version=self.settings.kms_key_version,
                    endpoint_url=self.settings.kms_endpoint_url,
                    region_name=self.settings.region_name,
                    connect_timeout_seconds=self.settings.connect_timeout_seconds,
                    read_timeout_seconds=self.settings.read_timeout_seconds,
                    max_retries=self.settings.max_retries,
                    verify_ssl=self.settings.verify_ssl,
                    ca_bundle=self.settings.ca_bundle,
                    app_env=self.settings.app_env,
                )
            elif self.settings.kms_provider_type == "fake":
                self.kms_provider = FakeKmsProvider(
                    kek_id=self.settings.kms_kek_id or "fake-kek-dev",
                    key_version=self.settings.kms_key_version,
                    app_env=self.settings.app_env,
                )
            else:
                raise ObjectStorageConfigurationError("unknown_kms_provider_type", setting="kms_provider_type")

        # Local tenant-scoped spool directory for resolving URIs to paths (descriptor compatibility)
        base_spool = (settings.spool_dir or Path(tempfile.gettempdir()) / "doc_extractor_s3_spool").resolve()
        self._tenant_spool_dir = base_spool / self._tenant_key
        try:
            self._tenant_spool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise TenantStorageUnavailableError("directory_not_creatable") from exc

    @property
    def client(self) -> Any:
        """Lazily initialized boto3 S3 client with bounded timeouts and retry configuration."""
        if self._injected_client is not None:
            return self._injected_client
        if self._client_instance is None:
            config = Config(
                connect_timeout=self.settings.connect_timeout_seconds,
                read_timeout=self.settings.read_timeout_seconds,
                retries={"max_attempts": self.settings.max_retries, "mode": "standard"},
                s3={"addressing_style": "path" if self.settings.effective_force_path_style else "virtual"},
            )
            session = boto3.session.Session()
            self._client_instance = session.client(
                service_name="s3",
                endpoint_url=self.settings.endpoint_url,
                region_name=self.settings.region_name,
                aws_access_key_id=self.settings.get_effective_access_key_id(),
                aws_secret_access_key=self.settings.get_effective_secret_access_key(),
                aws_session_token=self.settings.get_effective_session_token(),
                verify=self.settings.ca_bundle if self.settings.ca_bundle else self.settings.verify_ssl,
                config=config,
            )
        return self._client_instance

    def _map_error(self, exc: Exception, operation: str = "operation") -> Exception:
        """Map AWS/botocore client errors into typed, sanitized domain exceptions without leaking secrets."""
        if isinstance(exc, ClientError):
            error_response = exc.response.get("Error", {})
            code = error_response.get("Code", "")
            status_code = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("NoSuchBucket", "BucketNotFound") or (status_code == 404 and "bucket" in code.lower()):
                return ObjectStorageUnavailableError("bucket_not_found", details={"operation": operation})
            if code in ("NoSuchKey", "NotFound") or status_code == 404:
                return ObjectNotFoundError("object_not_found", details={"operation": operation})
            if code in ("AccessDenied", "Forbidden") or status_code == 403:
                return ObjectStorageUnavailableError("access_denied", details={"operation": operation})
            if code in ("InternalError", "ServiceUnavailable", "SlowDown") or (status_code and status_code >= 500):
                return ObjectStorageUnavailableError("storage_server_error", details={"operation": operation})
            return ObjectStorageError("client_error", details={"operation": operation})
        if isinstance(exc, EndpointConnectionError):
            return ObjectStorageUnavailableError("endpoint_unreachable", details={"operation": operation})
        if isinstance(exc, ConnectTimeoutError):
            return ObjectStorageUnavailableError("connect_timeout", details={"operation": operation})
        if isinstance(exc, ReadTimeoutError):
            return ObjectStorageUnavailableError("read_timeout", details={"operation": operation})
        if isinstance(exc, SSLError):
            return ObjectStorageUnavailableError("ssl_validation_failed", details={"operation": operation})
        if isinstance(exc, BotoCoreError):
            return ObjectStorageUnavailableError("botocore_error", details={"operation": operation})
        if isinstance(exc, (ObjectStorageError, TenantStorageUnavailableError, CrossTenantStorageAccessError, InvalidTenantIdentifierError)):
            return exc
        return StorageError(f"Storage operation failed ({operation}).")

    def _validate_doc_id(self, document_id: str) -> str:
        """Validate document identifier to strictly prevent path and object key traversal."""
        if "/" in document_id or "\\" in document_id or ".." in document_id:
            raise StorageError(
                message="Path traversal attempt detected in document storage path.",
                details={"document_id": document_id},
            )
        safe_doc_id = Path(document_id).name
        if not safe_doc_id or safe_doc_id in (".", ".."):
            raise StorageError(
                message="Invalid document ID format.",
                details={"document_id": document_id},
            )
        return safe_doc_id

    def _s3_key(self, relative_path: str) -> str:
        """Derive the tenant-scoped S3 key from relative path."""
        rel = relative_path.lstrip("/")
        return f"{self._prefix}{self._tenant_key}/{rel}"

    def _logical_uri(self, relative_path: str) -> str:
        """Derive the logical storage URI ({tenant_id}/{relative_path})."""
        rel = relative_path.lstrip("/")
        return f"{self.tenant_id}/{rel}"

    def _read_data(self, data: Union[bytes, BinaryIO]) -> bytes:
        """Helper to read bytes from bytes or file-like binary stream."""
        if isinstance(data, bytes):
            return data
        data.seek(0)
        return data.read()

    def _spool_file(self, relative_path: str, data: bytes) -> Path:
        """Atomically persist data to the tenant spool directory for descriptor operations with restricted permissions (0600)."""
        target_path = self._tenant_spool_dir / relative_path.lstrip("/")
        target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ensure_inside_tenant(self._tenant_spool_dir, target_path)

        tmp_path = target_path.parent / f".tmp_{uuid.uuid4().hex}"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(fd, "wb") as f:
                f.write(data)
            tmp_path.replace(target_path)
            try:
                os.chmod(target_path, 0o600)
            except OSError:
                pass
            return target_path
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def store_original_document(
        self,
        document_id: str,
        document_data: Union[bytes, BinaryIO],
    ) -> StoredArtifact:
        """Upload the accepted original PDF to S3 under the tenant/document boundary."""
        safe_doc_id = self._validate_doc_id(document_id)
        data = self._read_data(document_data)
        rel_path = f"{safe_doc_id}/source/original.pdf"
        s3_key = self._s3_key(rel_path)
        storage_uri = self._logical_uri(rel_path)

        # Secure metadata: only identifiers, never document contents or credentials
        metadata = {
            "tenant-marker": self._tenant_key,
            "document-id": safe_doc_id,
        }

        body_to_upload = data
        if self.settings.encryption_enabled:
            if not self.kms_provider:
                raise ObjectStorageError("kms_provider_not_configured")
            body_to_upload = encrypt_envelope(
                plaintext=data,
                tenant_id=self.tenant_id,
                logical_uri=storage_uri,
                kms_provider=self.kms_provider,
                content_type="application/pdf",
                kek_id=self.settings.kms_kek_id,
            )
            metadata["x-amz-meta-encryption"] = "envelope-aes256-gcm"

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=body_to_upload,
                ContentType="application/octet-stream" if self.settings.encryption_enabled else "application/pdf",
                Metadata=metadata,
            )
        except Exception as exc:
            raise self._map_error(exc, "store_original_document") from exc

        spool_path = self._spool_file(rel_path, data)

        return StoredArtifact(
            artifact_id="original.pdf",
            document_id=safe_doc_id,
            page_number=None,
            file_path=spool_path,
            file_size_bytes=len(data),
            mime_type="application/pdf",
            storage_uri=storage_uri,
        )

    def store_page_image(
        self,
        document_id: str,
        page_number: int,
        image_data: Union[bytes, BinaryIO],
        format_extension: str = "png",
    ) -> StoredArtifact:
        """Stores a rendered page image in S3."""
        if page_number < 1:
            raise StorageError(
                message=f"Page number must be one-based (got {page_number}).",
                details={"page_number": page_number},
            )
        safe_doc_id = self._validate_doc_id(document_id)
        data = self._read_data(image_data)
        rel_path = f"{safe_doc_id}/pages/page_{page_number:04d}.{format_extension}"
        s3_key = self._s3_key(rel_path)
        storage_uri = self._logical_uri(rel_path)
        artifact_id = f"art_{uuid.uuid4().hex[:12]}"

        metadata = {
            "tenant-marker": self._tenant_key,
            "document-id": safe_doc_id,
            "page-number": str(page_number),
        }

        body_to_upload = data
        if self.settings.encryption_enabled:
            if not self.kms_provider:
                raise ObjectStorageError("kms_provider_not_configured")
            body_to_upload = encrypt_envelope(
                plaintext=data,
                tenant_id=self.tenant_id,
                logical_uri=storage_uri,
                kms_provider=self.kms_provider,
                content_type=f"image/{format_extension}",
                kek_id=self.settings.kms_kek_id,
            )
            metadata["x-amz-meta-encryption"] = "envelope-aes256-gcm"

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=body_to_upload,
                ContentType="application/octet-stream" if self.settings.encryption_enabled else f"image/{format_extension}",
                Metadata=metadata,
            )
        except Exception as exc:
            raise self._map_error(exc, "store_page_image") from exc

        spool_path = self._spool_file(rel_path, data)

        return StoredArtifact(
            artifact_id=artifact_id,
            document_id=safe_doc_id,
            page_number=page_number,
            file_path=spool_path,
            file_size_bytes=len(data),
            mime_type=f"image/{format_extension}",
            storage_uri=storage_uri,
        )

    def store_preprocessed_page(
        self,
        document_id: str,
        page_number: int,
        image_data: Union[bytes, BinaryIO],
        metadata_dict: dict,
        format_extension: str = "png",
    ) -> StoredArtifact:
        """Stores a preprocessed page image and its transformation metadata sidecar in S3."""
        if page_number < 1:
            raise StorageError(
                message=f"Page number must be one-based (got {page_number}).",
                details={"page_number": page_number},
            )
        safe_doc_id = self._validate_doc_id(document_id)
        data = self._read_data(image_data)
        meta_json_bytes = json.dumps(metadata_dict, indent=2).encode("utf-8")

        img_rel_path = f"{safe_doc_id}/preprocessed/page_{page_number:04d}_preprocessed.{format_extension}"
        meta_rel_path = f"{safe_doc_id}/preprocessed/page_{page_number:04d}_metadata.json"

        img_key = self._s3_key(img_rel_path)
        meta_key = self._s3_key(meta_rel_path)
        img_uri = self._logical_uri(img_rel_path)
        meta_uri = self._logical_uri(meta_rel_path)
        artifact_id = f"prep_{uuid.uuid4().hex[:12]}"

        img_body = data
        meta_body = meta_json_bytes
        img_meta = {"tenant-marker": self._tenant_key, "document-id": safe_doc_id, "page-number": str(page_number)}
        meta_meta = {"tenant-marker": self._tenant_key, "document-id": safe_doc_id, "page-number": str(page_number)}

        if self.settings.encryption_enabled:
            if not self.kms_provider:
                raise ObjectStorageError("kms_provider_not_configured")
            img_body = encrypt_envelope(
                plaintext=data,
                tenant_id=self.tenant_id,
                logical_uri=img_uri,
                kms_provider=self.kms_provider,
                content_type=f"image/{format_extension}",
                kek_id=self.settings.kms_kek_id,
            )
            meta_body = encrypt_envelope(
                plaintext=meta_json_bytes,
                tenant_id=self.tenant_id,
                logical_uri=meta_uri,
                kms_provider=self.kms_provider,
                content_type="application/json",
                kek_id=self.settings.kms_kek_id,
            )
            img_meta["x-amz-meta-encryption"] = "envelope-aes256-gcm"
            meta_meta["x-amz-meta-encryption"] = "envelope-aes256-gcm"

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=img_key,
                Body=img_body,
                ContentType="application/octet-stream" if self.settings.encryption_enabled else f"image/{format_extension}",
                Metadata=img_meta,
            )
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=meta_key,
                Body=meta_body,
                ContentType="application/octet-stream" if self.settings.encryption_enabled else "application/json",
                Metadata=meta_meta,
            )
        except Exception as exc:
            raise self._map_error(exc, "store_preprocessed_page") from exc

        spool_path = self._spool_file(img_rel_path, data)
        self._spool_file(meta_rel_path, meta_json_bytes)

        return StoredArtifact(
            artifact_id=artifact_id,
            document_id=safe_doc_id,
            page_number=page_number,
            file_path=spool_path,
            file_size_bytes=len(data),
            mime_type=f"image/{format_extension}",
            storage_uri=self._logical_uri(img_rel_path),
        )

    def get_page_metadata(self, document_id: str, page_number: int) -> Optional[dict]:
        """Loads transformation metadata for a preprocessed page from S3."""
        safe_doc_id = self._validate_doc_id(document_id)
        meta_rel_path = f"{safe_doc_id}/preprocessed/page_{page_number:04d}_metadata.json"
        meta_key = self._s3_key(meta_rel_path)
        meta_uri = self._logical_uri(meta_rel_path)
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=meta_key)
            with resp["Body"] as stream:
                raw_bytes = stream.read()
            if is_envelope_encrypted(raw_bytes):
                if not self.kms_provider:
                    raise ObjectStorageError("kms_provider_required_for_decryption")
                plaintext, _ = decrypt_envelope(
                    container=raw_bytes,
                    expected_tenant_id=self.tenant_id,
                    expected_uri=meta_uri,
                    kms_provider=self.kms_provider,
                )
                return json.loads(plaintext.decode("utf-8"))
            elif self.settings.encryption_enabled and self.settings.app_env in ("staging", "production"):
                raise ObjectStorageError("unencrypted_data_refused_in_hardened_environment")
            else:
                return json.loads(raw_bytes.decode("utf-8"))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NotFound") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise self._map_error(exc, "get_page_metadata") from exc
        except (ObjectStorageError, CrossTenantStorageAccessError):
            raise
        except Exception as exc:
            raise self._map_error(exc, "get_page_metadata") from exc

    def list_page_images(self, document_id: str) -> List[StoredArtifact]:
        """Lists all stored page images for a given document in ascending page order."""
        safe_doc_id = self._validate_doc_id(document_id)
        prefix = self._s3_key(f"{safe_doc_id}/pages/")

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            pages = paginator.paginate(Bucket=self.bucket_name, Prefix=prefix)
            artifacts: List[StoredArtifact] = []

            for page in pages:
                for item in page.get("Contents", []):
                    key = item["Key"]
                    filename = Path(key).name
                    if filename.startswith(".tmp_"):
                        continue
                    parts = filename.split(".")[0].split("_")
                    try:
                        p_num = int(parts[1]) if len(parts) > 1 else None
                    except ValueError:
                        p_num = None

                    ext = Path(key).suffix.lstrip(".").lower()
                    size = item["Size"]
                    rel_path = f"{safe_doc_id}/pages/{filename}"

                    artifacts.append(
                        StoredArtifact(
                            artifact_id=filename,
                            document_id=safe_doc_id,
                            page_number=p_num,
                            file_path=self._tenant_spool_dir / rel_path,
                            file_size_bytes=size,
                            mime_type=f"image/{ext}",
                            storage_uri=self._logical_uri(rel_path),
                        )
                    )

            # Sort ascending by page number
            artifacts.sort(key=lambda a: (a.page_number if a.page_number is not None else 0))
            return artifacts
        except Exception as exc:
            raise self._map_error(exc, "list_page_images") from exc

    def _delete_prefix(self, prefix: str) -> None:
        """Helper to delete all objects under an S3 prefix."""
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if objects:
                    self.client.delete_objects(Bucket=self.bucket_name, Delete={"Objects": objects})
        except Exception as exc:
            raise self._map_error(exc, "delete_prefix") from exc

    def cleanup_preprocessed(self, document_id: str) -> None:
        """Deletes only preprocessed artifacts and metadata for a given document in S3 and spool."""
        safe_doc_id = self._validate_doc_id(document_id)
        prefix = self._s3_key(f"{safe_doc_id}/preprocessed/")
        self._delete_prefix(prefix)

        # Spool cleanup
        spool_prep = self._tenant_spool_dir / safe_doc_id / "preprocessed"
        if spool_prep.exists():
            shutil.rmtree(spool_prep, ignore_errors=True)

    def cleanup_document(self, document_id: str) -> None:
        """Deletes all stored artifacts and directories for a given document in S3 and spool."""
        safe_doc_id = self._validate_doc_id(document_id)
        prefix = self._s3_key(f"{safe_doc_id}/")
        self._delete_prefix(prefix)

        # Spool cleanup
        spool_doc = self._tenant_spool_dir / safe_doc_id
        if spool_doc.exists():
            shutil.rmtree(spool_doc, ignore_errors=True)

    def resolve_uri_to_path(self, storage_uri: str) -> Path:
        """
        Safely resolves a logical URI ({tenant_id}/{doc_id}/...) to a local filesystem path.

        Guarantees:
        - Prevents directory traversal (e.g. '..', leading slashes).
        - The first segment must be exactly self.tenant_id (cross-tenant access raises CrossTenantStorageAccessError).
        - Verifies object existence in S3.
        - Spools the file locally in the tenant's isolated directory and returns the Path.
        """
        if (
            "\\" in storage_uri
            or storage_uri.startswith("/")
            or ".." in storage_uri.split("/")
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in storage_uri)
        ):
            raise StorageError(
                message="Unsafe storage URI detected.",
                details={"storage_uri": storage_uri},
            )

        tenant_segment, _, relative = storage_uri.partition("/")
        if tenant_segment != self.tenant_id or relative.startswith("/"):
            raise CrossTenantStorageAccessError(details={"storage_uri": storage_uri, "tenant_id": self.tenant_id})

        s3_key = self._s3_key(relative)
        spool_path = self._tenant_spool_dir / relative
        ensure_inside_tenant(self._tenant_spool_dir, spool_path)

        # Download from S3 if not cached or verify existence
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                data = stream.read()

            if is_envelope_encrypted(data):
                if not self.kms_provider:
                    raise ObjectStorageError("kms_provider_required_for_decryption")
                plaintext, _ = decrypt_envelope(
                    container=data,
                    expected_tenant_id=self.tenant_id,
                    expected_uri=storage_uri,
                    kms_provider=self.kms_provider,
                )
                self._spool_file(relative, plaintext)
            else:
                if self.settings.encryption_enabled and self.settings.app_env in ("staging", "production"):
                    raise ObjectStorageError("unencrypted_data_refused_in_hardened_environment")
                self._spool_file(relative, data)

            return spool_path.resolve()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NotFound") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                raise StorageError(
                    message="Requested storage artifact does not exist.",
                    details={"storage_uri": storage_uri},
                ) from exc
            raise self._map_error(exc, "resolve_uri_to_path") from exc
        except (ObjectStorageError, CrossTenantStorageAccessError, StorageError):
            raise
        except Exception as exc:
            raise self._map_error(exc, "resolve_uri_to_path") from exc

    def save_document_record(self, record: Any) -> None:
        """Persists canonical document record to S3."""
        safe_doc_id = self._validate_doc_id(record.document_id)
        rel_path = f"{safe_doc_id}/document_record.json"
        s3_key = self._s3_key(rel_path)
        storage_uri = self._logical_uri(rel_path)
        rec_dict = record.to_dict() if hasattr(record, "to_dict") else dict(record)
        data = json.dumps(rec_dict, indent=2).encode("utf-8")

        metadata = {"tenant-marker": self._tenant_key, "document-id": safe_doc_id}
        body_to_upload = data
        if self.settings.encryption_enabled:
            if not self.kms_provider:
                raise ObjectStorageError("kms_provider_not_configured")
            body_to_upload = encrypt_envelope(
                plaintext=data,
                tenant_id=self.tenant_id,
                logical_uri=storage_uri,
                kms_provider=self.kms_provider,
                content_type="application/json",
                kek_id=self.settings.kms_kek_id,
            )
            metadata["x-amz-meta-encryption"] = "envelope-aes256-gcm"

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=body_to_upload,
                ContentType="application/octet-stream" if self.settings.encryption_enabled else "application/json",
                Metadata=metadata,
            )
        except Exception as exc:
            raise self._map_error(exc, "save_document_record") from exc

        self._spool_file(rel_path, data)

    def get_document_record(self, document_id: str) -> Optional[Any]:
        """Loads canonical document record from S3 by document ID."""
        safe_doc_id = self._validate_doc_id(document_id)
        rel_path = f"{safe_doc_id}/document_record.json"
        s3_key = self._s3_key(rel_path)
        storage_uri = self._logical_uri(rel_path)
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                raw_bytes = stream.read()
            if is_envelope_encrypted(raw_bytes):
                if not self.kms_provider:
                    raise ObjectStorageError("kms_provider_required_for_decryption")
                plaintext, _ = decrypt_envelope(
                    container=raw_bytes,
                    expected_tenant_id=self.tenant_id,
                    expected_uri=storage_uri,
                    kms_provider=self.kms_provider,
                )
                return json.loads(plaintext.decode("utf-8"))
            elif self.settings.encryption_enabled and self.settings.app_env in ("staging", "production"):
                raise ObjectStorageError("unencrypted_data_refused_in_hardened_environment")
            else:
                return json.loads(raw_bytes.decode("utf-8"))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NotFound") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise self._map_error(exc, "get_document_record") from exc
        except (ObjectStorageError, CrossTenantStorageAccessError):
            raise
        except Exception as exc:
            raise self._map_error(exc, "get_document_record") from exc

    def find_document_by_hash(self, file_hash: str) -> Optional[Any]:
        """Looks up existing document within the current tenant by SHA-256 hash in S3."""
        prefix = self._s3_key("")
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    if key.endswith("/document_record.json"):
                        try:
                            resp = self.client.get_object(Bucket=self.bucket_name, Key=key)
                            with resp["Body"] as stream:
                                raw_bytes = stream.read()
                            if is_envelope_encrypted(raw_bytes):
                                if not self.kms_provider:
                                    continue
                                parts = key.split("/")
                                doc_id = parts[-2]
                                storage_uri = f"{self.tenant_id}/{doc_id}/document_record.json"
                                plaintext, _ = decrypt_envelope(
                                    container=raw_bytes,
                                    expected_tenant_id=self.tenant_id,
                                    expected_uri=storage_uri,
                                    kms_provider=self.kms_provider,
                                )
                                data = json.loads(plaintext.decode("utf-8"))
                            else:
                                data = json.loads(raw_bytes.decode("utf-8"))
                            if data.get("file_hash_sha256") == file_hash:
                                return data
                        except Exception:
                            continue
            return None
        except Exception as exc:
            raise self._map_error(exc, "find_document_by_hash") from exc

    def save_handoff_payload(self, payload: Any) -> None:
        """Persists the terminal Module A handoff payload in S3."""
        safe_doc_id = self._validate_doc_id(payload.document_id)
        rel_path = f"{safe_doc_id}/module_a_handoff.json"
        s3_key = self._s3_key(rel_path)
        storage_uri = self._logical_uri(rel_path)
        payload_dict = payload.to_dict() if hasattr(payload, "to_dict") else dict(payload)
        data = json.dumps(payload_dict, indent=2).encode("utf-8")

        metadata = {"tenant-marker": self._tenant_key, "document-id": safe_doc_id}
        body_to_upload = data
        if self.settings.encryption_enabled:
            if not self.kms_provider:
                raise ObjectStorageError("kms_provider_not_configured")
            body_to_upload = encrypt_envelope(
                plaintext=data,
                tenant_id=self.tenant_id,
                logical_uri=storage_uri,
                kms_provider=self.kms_provider,
                content_type="application/json",
                kek_id=self.settings.kms_kek_id,
            )
            metadata["x-amz-meta-encryption"] = "envelope-aes256-gcm"

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=body_to_upload,
                ContentType="application/octet-stream" if self.settings.encryption_enabled else "application/json",
                Metadata=metadata,
            )
        except Exception as exc:
            raise self._map_error(exc, "save_handoff_payload") from exc

        self._spool_file(rel_path, data)

    def get_handoff_payload(self, document_id: str) -> Optional[dict]:
        """Retrieves the persisted Module A handoff payload from S3."""
        safe_doc_id = self._validate_doc_id(document_id)
        rel_path = f"{safe_doc_id}/module_a_handoff.json"
        s3_key = self._s3_key(rel_path)
        storage_uri = self._logical_uri(rel_path)
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                raw_bytes = stream.read()
            if is_envelope_encrypted(raw_bytes):
                if not self.kms_provider:
                    raise ObjectStorageError("kms_provider_required_for_decryption")
                plaintext, _ = decrypt_envelope(
                    container=raw_bytes,
                    expected_tenant_id=self.tenant_id,
                    expected_uri=storage_uri,
                    kms_provider=self.kms_provider,
                )
                return json.loads(plaintext.decode("utf-8"))
            elif self.settings.encryption_enabled and self.settings.app_env in ("staging", "production"):
                raise ObjectStorageError("unencrypted_data_refused_in_hardened_environment")
            else:
                return json.loads(raw_bytes.decode("utf-8"))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NotFound") or exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise self._map_error(exc, "get_handoff_payload") from exc
        except (ObjectStorageError, CrossTenantStorageAccessError):
            raise
        except Exception as exc:
            raise self._map_error(exc, "get_handoff_payload") from exc

    # Direct first-class S3 primitives
    def get_object_bytes(self, relative_path: str) -> bytes:
        """Retrieves raw object bytes from S3 for a tenant-scoped relative path, decrypting if encrypted."""
        s3_key = self._s3_key(relative_path)
        storage_uri = self._logical_uri(relative_path)
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                raw = stream.read()
            if is_envelope_encrypted(raw):
                if not self.kms_provider:
                    raise ObjectStorageError("kms_provider_required_for_decryption")
                plaintext, _ = decrypt_envelope(
                    container=raw,
                    expected_tenant_id=self.tenant_id,
                    expected_uri=storage_uri,
                    kms_provider=self.kms_provider,
                )
                return plaintext
            elif self.settings.encryption_enabled and self.settings.app_env in ("staging", "production"):
                raise ObjectStorageError("unencrypted_data_refused_in_hardened_environment")
            return raw
        except (ObjectStorageError, CrossTenantStorageAccessError):
            raise
        except Exception as exc:
            raise self._map_error(exc, "get_object_bytes") from exc

    def put_object_bytes(
        self,
        relative_path: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[Dict[str, str]] = None,
    ) -> None:
        """Puts raw object bytes to S3 under the tenant boundary, encrypting if encryption enabled."""
        s3_key = self._s3_key(relative_path)
        storage_uri = self._logical_uri(relative_path)
        safe_meta = {"tenant-marker": self._tenant_key}
        if metadata:
            safe_meta.update(metadata)

        body_to_upload = data
        effective_mime = content_type
        if self.settings.encryption_enabled:
            if not self.kms_provider:
                raise ObjectStorageError("kms_provider_not_configured")
            body_to_upload = encrypt_envelope(
                plaintext=data,
                tenant_id=self.tenant_id,
                logical_uri=storage_uri,
                kms_provider=self.kms_provider,
                content_type=content_type,
                kek_id=self.settings.kms_kek_id,
            )
            safe_meta["x-amz-meta-encryption"] = "envelope-aes256-gcm"
            effective_mime = "application/octet-stream"

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=body_to_upload,
                ContentType=effective_mime,
                Metadata=safe_meta,
            )
        except Exception as exc:
            raise self._map_error(exc, "put_object_bytes") from exc

    def rewrap_object(
        self,
        relative_path: str,
        target_kek_id: str,
        target_version: Optional[str] = None,
    ) -> None:
        """
        Rewraps the encrypted DEK of a stored object under a new KEK or version without decrypting data.
        Performs server-side / provider rewrapping, updating the container header in S3.
        """
        s3_key = self._s3_key(relative_path)
        if not self.kms_provider:
            raise ObjectStorageError("kms_provider_not_configured")

        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                container = stream.read()
        except Exception as exc:
            raise self._map_error(exc, "rewrap_object_fetch") from exc

        if not is_envelope_encrypted(container):
            raise ObjectStorageError("object_not_envelope_encrypted", details={"relative_path": relative_path})

        new_container = rewrap_envelope(
            container=container,
            target_kek_id=target_kek_id,
            kms_provider=self.kms_provider,
            target_version=target_version,
        )

        try:
            self.client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=new_container,
                ContentType="application/octet-stream",
                Metadata={"tenant-marker": self._tenant_key, "x-amz-meta-encryption": "envelope-aes256-gcm"},
            )
        except Exception as exc:
            raise self._map_error(exc, "rewrap_object_put") from exc

    def cleanup_spool(self, document_id: Optional[str] = None) -> None:
        """
        Removes decrypted temporary files from the local tenant spool directory.
        If document_id is provided, removes only that document's spooled files;
        otherwise removes the entire tenant spool directory contents.
        """
        if document_id:
            safe_doc_id = self._validate_doc_id(document_id)
            target = self._tenant_spool_dir / safe_doc_id
        else:
            target = self._tenant_spool_dir

        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
            if not document_id:
                target.mkdir(parents=True, exist_ok=True, mode=0o700)

    def object_exists(self, relative_path: str) -> bool:
        """Checks if an object exists in S3 under the tenant boundary."""
        s3_key = self._s3_key(relative_path)
        try:
            self.client.head_object(Bucket=self.bucket_name, Key=s3_key)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise self._map_error(exc, "object_exists") from exc
        except Exception as exc:
            raise self._map_error(exc, "object_exists") from exc

    def head_object(self, relative_path: str) -> Optional[Dict[str, Any]]:
        """Returns object metadata headers from S3, or None if not found."""
        s3_key = self._s3_key(relative_path)
        try:
            resp = self.client.head_object(Bucket=self.bucket_name, Key=s3_key)
            return {
                "content_type": resp.get("ContentType"),
                "content_length": resp.get("ContentLength"),
                "etag": resp.get("ETag"),
                "metadata": resp.get("Metadata", {}),
            }
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise self._map_error(exc, "head_object") from exc
        except Exception as exc:
            raise self._map_error(exc, "head_object") from exc

    def delete_object(self, relative_path: str) -> None:
        """Deletes an object from S3 under the tenant boundary."""
        s3_key = self._s3_key(relative_path)
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=s3_key)
        except Exception as exc:
            raise self._map_error(exc, "delete_object") from exc

    def check_health(self) -> Dict[str, Any]:
        """Checks connectivity and bucket existence on S3."""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            return {"status": "ok", "bucket": self.bucket_name}
        except Exception as exc:
            raise self._map_error(exc, "check_health") from exc

    def close(self) -> None:
        """Cleanly closes client connections if active."""
        if self._client_instance is not None and hasattr(self._client_instance, "close"):
            try:
                self._client_instance.close()
            except Exception:
                pass
