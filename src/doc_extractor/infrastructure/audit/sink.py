"""
Tamper-resistant external audit checkpoint storage (Team Member 3, Phase 4C).

Adheres to:
- PRD SEC-008: Tamper-resistant external audit sink with immutable retention (WORM).
- S3 Object Lock: COMPLIANCE/GOVERNANCE retention modes prevent deletion or overwriting during retention period.
- Idempotent Duplicates: Re-storing an identical checkpoint succeeds idempotently; conflicting data raises CheckpointConflictError.
- No Silent Fallback: Outside development, failure of Object Lock on the destination bucket raises ObjectLockNotSupportedError.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

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

from doc_extractor.infrastructure.audit.errors import (
    CheckpointConflictError,
    CheckpointSinkError,
    CheckpointSinkUnavailableError,
    ObjectLockNotSupportedError,
)
from doc_extractor.infrastructure.audit.models import AuditCheckpoint
from doc_extractor.tenancy import require_path_safe_tenant_id, tenant_storage_key

logger = logging.getLogger("doc_extractor.infrastructure.audit.sink")

HARDENED_ENVIRONMENTS = ("staging", "production")


class AuditCheckpointSink(ABC):
    """Abstract interface for external, tamper-resistant audit checkpoint storage."""

    @abstractmethod
    async def store_checkpoint(self, checkpoint: AuditCheckpoint) -> None:
        """
        Durably stores an independently signed checkpoint in the external sink with immutable retention.
        Idempotent for identical checkpoints; raises CheckpointConflictError if conflicting data exists.
        """
        pass

    @abstractmethod
    async def get_checkpoint(self, tenant_id: str, seq: int) -> Optional[AuditCheckpoint]:
        """Retrieves a specific checkpoint by tenant ID and sequence number."""
        pass

    @abstractmethod
    async def get_latest_checkpoint(self, tenant_id: str) -> Optional[AuditCheckpoint]:
        """Retrieves the highest-sequence checkpoint for a tenant, or None if none exist."""
        pass

    @abstractmethod
    async def list_checkpoints(self, tenant_id: str, limit: int = 100) -> List[AuditCheckpoint]:
        """Lists checkpoints for a tenant in ascending sequence order."""
        pass

    @abstractmethod
    async def check_health(self) -> Dict[str, Any]:
        """Verifies connectivity and retention readiness of the external sink."""
        pass


class EphemeralCheckpointSink(AuditCheckpointSink):
    """
    Ephemeral test double for external checkpoint storage with WORM simulation.
    """

    def __init__(self) -> None:
        # Map: (tenant_id, seq) -> canonical JSON string
        self._checkpoints: Dict[tuple[str, int], str] = {}
        self._simulate_outage = False

    def simulate_outage(self, active: bool = True) -> None:
        """Simulate external sink outage."""
        self._simulate_outage = active

    def tamper_checkpoint(self, tenant_id: str, seq: int, tampered_json: str) -> None:
        """Test hook to simulate external sink tampering."""
        self._checkpoints[(tenant_id, seq)] = tampered_json

    async def store_checkpoint(self, checkpoint: AuditCheckpoint) -> None:
        if self._simulate_outage:
            raise CheckpointSinkUnavailableError("external_sink_unreachable")

        key = (checkpoint.tenant_id, checkpoint.seq)
        new_json = checkpoint.to_json()

        if key in self._checkpoints:
            existing_json = self._checkpoints[key]
            if existing_json == new_json:
                return  # Idempotent write
            raise CheckpointConflictError(
                "checkpoint_conflict_at_existing_sequence",
                details={"tenant_id": checkpoint.tenant_id, "seq": checkpoint.seq},
            )

        self._checkpoints[key] = new_json

    async def get_checkpoint(self, tenant_id: str, seq: int) -> Optional[AuditCheckpoint]:
        if self._simulate_outage:
            raise CheckpointSinkUnavailableError("external_sink_unreachable")
        raw = self._checkpoints.get((tenant_id, seq))
        return AuditCheckpoint.from_json(raw) if raw else None

    async def get_latest_checkpoint(self, tenant_id: str) -> Optional[AuditCheckpoint]:
        if self._simulate_outage:
            raise CheckpointSinkUnavailableError("external_sink_unreachable")
        tenant_items = [
            (seq, raw) for (tid, seq), raw in self._checkpoints.items() if tid == tenant_id
        ]
        if not tenant_items:
            return None
        _, latest_raw = max(tenant_items, key=lambda item: item[0])
        return AuditCheckpoint.from_json(latest_raw)

    async def list_checkpoints(self, tenant_id: str, limit: int = 100) -> List[AuditCheckpoint]:
        if self._simulate_outage:
            raise CheckpointSinkUnavailableError("external_sink_unreachable")
        tenant_items = sorted(
            [(seq, raw) for (tid, seq), raw in self._checkpoints.items() if tid == tenant_id],
            key=lambda item: item[0],
        )
        return [AuditCheckpoint.from_json(raw) for _, raw in tenant_items[:limit]]

    async def check_health(self) -> Dict[str, Any]:
        if self._simulate_outage:
            raise CheckpointSinkUnavailableError("external_sink_unreachable")
        return {"status": "ok", "sink": "in_memory", "checkpoints_count": len(self._checkpoints)}


class S3ObjectLockCheckpointSink(AuditCheckpointSink):
    """
    S3-compatible immutable audit checkpoint sink using S3 Object Lock (WORM).
    """

    def __init__(
        self,
        bucket_name: str,
        endpoint_url: Optional[str] = None,
        region_name: str = "us-east-1",
        access_key_id: Optional[str] = None,
        secret_access_key: Optional[str] = None,
        session_token: Optional[str] = None,
        key_prefix: str = "audit-checkpoints/",
        retention_mode: Literal["COMPLIANCE", "GOVERNANCE"] = "COMPLIANCE",
        retention_period_days: int = 365,
        enable_object_lock: bool = True,
        require_object_lock: bool = True,
        client: Optional[Any] = None,
        connect_timeout_seconds: float = 5.0,
        read_timeout_seconds: float = 10.0,
        max_retries: int = 3,
        verify_ssl: bool = True,
        ca_bundle: Optional[str] = None,
        app_env: str = "production",
    ) -> None:
        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url
        self.region_name = region_name
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.session_token = session_token
        self.key_prefix = key_prefix if key_prefix.endswith("/") else f"{key_prefix}/"
        self.retention_mode = retention_mode
        self.retention_period_days = retention_period_days
        self.enable_object_lock = enable_object_lock
        self.require_object_lock = require_object_lock
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
                service_name="s3",
                endpoint_url=self.endpoint_url,
                region_name=self.region_name,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                aws_session_token=self.session_token,
                verify=self._ca_bundle if self._ca_bundle else self._verify_ssl,
                config=config,
            )
        return self._client_instance

    def _s3_key(self, tenant_id: str, seq: int) -> str:
        require_path_safe_tenant_id(tenant_id)
        tenant_key = tenant_storage_key(tenant_id)
        return f"{self.key_prefix}{tenant_key}/checkpoints/checkpoint_{seq:010d}.json"

    def _map_error(self, exc: Exception, operation: str = "sink_operation") -> Exception:
        if isinstance(exc, (CheckpointConflictError, CheckpointSinkUnavailableError, ObjectLockNotSupportedError)):
            return exc
        if isinstance(exc, ClientError):
            code = exc.response.get("Error", {}).get("Code", "")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in ("ObjectLockConfigurationNotFoundError", "InvalidRequest") and "object lock" in str(exc).lower():
                return ObjectLockNotSupportedError("object_lock_not_enabled_on_bucket", setting="bucket_name")
            if code in ("AccessDenied", "Forbidden") or status == 403:
                return CheckpointSinkUnavailableError("sink_access_denied", details={"operation": operation})
            if code in ("NoSuchBucket", "BucketNotFound") or status == 404:
                return CheckpointSinkUnavailableError("sink_bucket_not_found", details={"operation": operation})
            if status and status >= 500:
                return CheckpointSinkUnavailableError("sink_server_error", details={"operation": operation})
            return CheckpointSinkError(f"sink_client_error ({code})", details={"operation": operation})
        if isinstance(exc, (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, SSLError, BotoCoreError)):
            return CheckpointSinkUnavailableError(type(exc).__name__, details={"operation": operation})
        return CheckpointSinkError(f"sink_operation_failed ({operation})")

    async def store_checkpoint(self, checkpoint: AuditCheckpoint) -> None:
        s3_key = self._s3_key(checkpoint.tenant_id, checkpoint.seq)
        body_bytes = checkpoint.to_json().encode("utf-8")
        tenant_key = tenant_storage_key(checkpoint.tenant_id)

        # 1. Idempotency check: see if checkpoint already exists at this key
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                existing_bytes = stream.read()
            if existing_bytes == body_bytes:
                return  # Identical checkpoint: succeed idempotently
            raise CheckpointConflictError(
                "checkpoint_conflict_at_existing_sequence",
                details={"tenant_id": checkpoint.tenant_id, "seq": checkpoint.seq},
            )
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status != 404 and exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise self._map_error(exc, "check_existing_checkpoint") from exc

        # 2. Put object with Object Lock retention headers
        kwargs: Dict[str, Any] = {
            "Bucket": self.bucket_name,
            "Key": s3_key,
            "Body": body_bytes,
            "ContentType": "application/json",
            "Metadata": {
                "tenant-marker": tenant_key,
                "checkpoint-seq": str(checkpoint.seq),
                "key-id": checkpoint.key_id,
            },
        }

        if self.enable_object_lock:
            retain_until = datetime.now(timezone.utc) + timedelta(days=self.retention_period_days)
            kwargs["ObjectLockMode"] = self.retention_mode
            kwargs["ObjectLockRetainUntilDate"] = retain_until

        try:
            self.client.put_object(**kwargs)
        except Exception as exc:
            raise self._map_error(exc, "store_checkpoint") from exc

    async def get_checkpoint(self, tenant_id: str, seq: int) -> Optional[AuditCheckpoint]:
        s3_key = self._s3_key(tenant_id, seq)
        try:
            resp = self.client.get_object(Bucket=self.bucket_name, Key=s3_key)
            with resp["Body"] as stream:
                data = stream.read().decode("utf-8")
            return AuditCheckpoint.from_json(data)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise self._map_error(exc, "get_checkpoint") from exc
        except Exception as exc:
            raise self._map_error(exc, "get_checkpoint") from exc

    async def get_latest_checkpoint(self, tenant_id: str) -> Optional[AuditCheckpoint]:
        require_path_safe_tenant_id(tenant_id)
        tenant_key = tenant_storage_key(tenant_id)
        prefix = f"{self.key_prefix}{tenant_key}/checkpoints/checkpoint_"

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            highest_key: Optional[str] = None
            highest_seq = -1

            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for item in page.get("Contents", []):
                    key = item["Key"]
                    try:
                        filename = Path(key).stem  # e.g. "checkpoint_0000000042"
                        seq_str = filename.split("_")[1]
                        seq_val = int(seq_str)
                        if seq_val > highest_seq:
                            highest_seq = seq_val
                            highest_key = key
                    except (IndexError, ValueError):
                        continue

            if highest_key is None:
                return None

            resp = self.client.get_object(Bucket=self.bucket_name, Key=highest_key)
            with resp["Body"] as stream:
                data = stream.read().decode("utf-8")
            return AuditCheckpoint.from_json(data)
        except Exception as exc:
            raise self._map_error(exc, "get_latest_checkpoint") from exc

    async def list_checkpoints(self, tenant_id: str, limit: int = 100) -> List[AuditCheckpoint]:
        require_path_safe_tenant_id(tenant_id)
        tenant_key = tenant_storage_key(tenant_id)
        prefix = f"{self.key_prefix}{tenant_key}/checkpoints/checkpoint_"

        try:
            paginator = self.client.get_paginator("list_objects_v2")
            keys: List[str] = []

            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                for item in page.get("Contents", []):
                    keys.append(item["Key"])

            keys.sort()
            results: List[AuditCheckpoint] = []
            for k in keys[:limit]:
                resp = self.client.get_object(Bucket=self.bucket_name, Key=k)
                with resp["Body"] as stream:
                    results.append(AuditCheckpoint.from_json(stream.read().decode("utf-8")))

            return results
        except Exception as exc:
            raise self._map_error(exc, "list_checkpoints") from exc

    async def check_health(self) -> Dict[str, Any]:
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            lock_config = None
            if self.enable_object_lock:
                try:
                    resp = self.client.get_object_lock_configuration(Bucket=self.bucket_name)
                    lock_config = resp.get("ObjectLockConfiguration", {}).get("ObjectLockEnabled")
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if self.require_object_lock and self.app_env in HARDENED_ENVIRONMENTS:
                        raise ObjectLockNotSupportedError("object_lock_not_enabled_on_bucket", setting="bucket_name")
                    lock_config = f"unavailable ({code})"
            return {
                "status": "ok",
                "bucket": self.bucket_name,
                "object_lock_enabled": lock_config,
                "retention_mode": self.retention_mode,
            }
        except Exception as exc:
            raise self._map_error(exc, "check_health") from exc
