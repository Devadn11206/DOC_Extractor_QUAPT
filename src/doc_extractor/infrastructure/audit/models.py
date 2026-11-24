"""
Data models and canonical serialization for signed audit checkpoints (Team Member 3, Phase 4C).

Adheres to:
- PRD SEC-008: Independently verifiable signed audit checkpoints.
- Canonical serialization: Strict deterministic JSON ordering for asymmetric signatures.
- Cryptographic binding: Signatures cover all checkpoint metadata fields.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from doc_extractor.api.audit_chain import canonical_json
from doc_extractor.infrastructure.audit.errors import CheckpointVerificationError
from doc_extractor.infrastructure.mongo.serialization import ensure_utc
from doc_extractor.tenancy import require_path_safe_tenant_id

CHECKPOINT_FORMAT_VERSION = 1
SUPPORTED_ALGORITHMS = ("RSASSA_PSS_SHA_256", "ECDSA_SHA_256")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def _format_iso8601_utc(dt: datetime) -> str:
    """Format UTC datetime as strict ISO 8601 with 6 fractional digits and trailing 'Z'."""
    utc_dt = ensure_utc(dt)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_iso8601_utc(dt_str: str) -> datetime:
    """Parse UTC datetime from strict ISO 8601 string."""
    try:
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        parsed = datetime.fromisoformat(dt_str)
        return ensure_utc(parsed)
    except Exception as exc:
        raise CheckpointVerificationError("malformed_checkpoint_timestamp") from exc


@dataclass(frozen=True)
class AuditCheckpoint:
    """
    Independently signed audit checkpoint.
    Binds a specific sequence and hash of a tenant's audit trail under an asymmetric KMS key.
    """

    tenant_id: str
    seq: int
    chain_hash: str
    timestamp: datetime
    key_id: str
    key_version: str
    version: int = CHECKPOINT_FORMAT_VERSION
    algorithm: str = "RSASSA_PSS_SHA_256"
    signature: Optional[str] = None  # Base64-encoded signature string

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, str) or not self.tenant_id.strip():
            raise CheckpointVerificationError("tenant_id_empty")
        try:
            require_path_safe_tenant_id(self.tenant_id)
        except Exception as exc:
            raise CheckpointVerificationError(f"invalid_tenant_id: {exc}") from exc

        if not isinstance(self.seq, int) or isinstance(self.seq, bool) or self.seq < 0:
            raise CheckpointVerificationError("invalid_sequence_number")

        if not isinstance(self.chain_hash, str) or not _SHA256_HEX.match(self.chain_hash):
            raise CheckpointVerificationError("invalid_chain_hash_format")

        if not isinstance(self.timestamp, datetime):
            raise CheckpointVerificationError("timestamp_must_be_datetime")

        if not isinstance(self.key_id, str) or not self.key_id.strip():
            raise CheckpointVerificationError("key_id_empty")

        if not isinstance(self.key_version, str) or not self.key_version.strip():
            raise CheckpointVerificationError("key_version_empty")

        if self.version != CHECKPOINT_FORMAT_VERSION:
            raise CheckpointVerificationError(f"unsupported_checkpoint_version_{self.version}")

        if self.algorithm not in SUPPORTED_ALGORITHMS:
            raise CheckpointVerificationError(f"unsupported_signing_algorithm_{self.algorithm}")

    def canonical_dict(self) -> Dict[str, Any]:
        """Dictionary of security-relevant fields that are covered by the asymmetric signature."""
        return {
            "alg": self.algorithm,
            "chain_hash": self.chain_hash,
            "key_id": self.key_id,
            "key_ver": self.key_version,
            "seq": self.seq,
            "tenant_id": self.tenant_id,
            "timestamp": _format_iso8601_utc(self.timestamp),
            "v": self.version,
        }

    def canonical_bytes(self) -> bytes:
        """Deterministic canonical UTF-8 bytes for signing and signature verification."""
        return canonical_json(self.canonical_dict()).encode("utf-8")

    def raw_signature_bytes(self) -> bytes:
        """Decodes the base64 signature into raw binary bytes."""
        if not self.signature:
            raise CheckpointVerificationError("checkpoint_not_signed")
        try:
            return base64.b64decode(self.signature)
        except Exception as exc:
            raise CheckpointVerificationError("invalid_base64_signature") from exc

    def with_signature(self, signature_bytes: bytes) -> AuditCheckpoint:
        """Returns a copy of this checkpoint with the base64 signature attached."""
        b64_sig = base64.b64encode(signature_bytes).decode("ascii")
        return AuditCheckpoint(
            tenant_id=self.tenant_id,
            seq=self.seq,
            chain_hash=self.chain_hash,
            timestamp=self.timestamp,
            key_id=self.key_id,
            key_version=self.key_version,
            version=self.version,
            algorithm=self.algorithm,
            signature=b64_sig,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes full checkpoint including signature to a JSON-compatible dictionary."""
        d = self.canonical_dict()
        d["sig"] = self.signature
        return d

    def to_json(self) -> str:
        """Canonical JSON string representation of the signed checkpoint."""
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> AuditCheckpoint:
        """Deserializes a checkpoint from a dictionary."""
        if not isinstance(data, dict):
            raise CheckpointVerificationError("checkpoint_must_be_dict")

        required = ("tenant_id", "seq", "chain_hash", "timestamp", "key_id", "key_ver", "v", "alg")
        for key in required:
            if key not in data:
                raise CheckpointVerificationError(f"missing_required_checkpoint_field_{key}")

        return cls(
            tenant_id=data["tenant_id"],
            seq=data["seq"],
            chain_hash=data["chain_hash"],
            timestamp=_parse_iso8601_utc(data["timestamp"]),
            key_id=data["key_id"],
            key_version=data["key_ver"],
            version=data["v"],
            algorithm=data["alg"],
            signature=data.get("sig"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> AuditCheckpoint:
        """Deserializes a checkpoint from JSON text."""
        try:
            data = json.loads(json_str)
        except Exception as exc:
            raise CheckpointVerificationError("invalid_checkpoint_json") from exc
        return cls.from_dict(data)


class CheckpointVerificationStatus(str, Enum):
    """Categorical verification outcomes for signed audit checkpoint verification."""

    VALID = "valid"
    CHAIN_TRUNCATED = "chain_truncated"
    HASH_MISMATCH = "hash_mismatch"
    CHAIN_FORKED = "chain_forked"
    SIGNATURE_INVALID = "signature_invalid"
    CHECKPOINT_NOT_FOUND = "checkpoint_not_found"
    CHECKPOINT_TAMPERED = "checkpoint_tampered"
    DB_CHAIN_BROKEN = "db_chain_broken"
    SINK_UNAVAILABLE = "sink_unavailable"
    ERROR = "error"


@dataclass(frozen=True)
class CheckpointVerificationReport:
    """
    Comprehensive verification report comparing MongoDB audit records, checkpoints, signatures, and external sinks.
    """

    tenant_id: str
    status: CheckpointVerificationStatus
    valid: bool
    checkpoint_verified: bool  # True ONLY if an asymmetric signature on a checkpoint was verified
    database_chain_verified: bool  # True ONLY if internal hash chain verified from seq 1 to head
    checked_events: int
    checkpoint_seq: Optional[int] = None
    checkpoint_hash: Optional[str] = None
    db_head_seq: Optional[int] = None
    db_head_hash: Optional[str] = None
    first_bad_seq: Optional[int] = None
    reason: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "status": self.status.value,
            "valid": self.valid,
            "checkpoint_verified": self.checkpoint_verified,
            "database_chain_verified": self.database_chain_verified,
            "checked_events": self.checked_events,
            "checkpoint_seq": self.checkpoint_seq,
            "checkpoint_hash": self.checkpoint_hash,
            "db_head_seq": self.db_head_seq,
            "db_head_hash": self.db_head_hash,
            "first_bad_seq": self.first_bad_seq,
            "reason": self.reason,
            "details": dict(self.details),
        }
