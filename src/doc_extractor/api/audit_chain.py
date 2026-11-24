"""
Durable, tamper-evident audit trail (PRD SEC-008, FR-AUD-001, section 21).

What this module guarantees:
- One append-only chain per tenant, stored under that tenant's verified directory (SEC-006). A chain never reads or
  writes another tenant's events.
- Every line stores ``prev_hash`` and ``hash = SHA-256(prev_hash | seq | canonical event JSON)``. Changing, deleting,
  reordering or inserting a line breaks verification at the first affected sequence number (``verify_chain``).
- Each append is fsynced before it is reported as successful. A failed write RAISES, so a mandatory operation can
  refuse to continue unaudited (``DurableAuditLogger.propagates_failures`` is True).
- A chain must have a single writer. A lock serializes appends in one process. Across replicas pass ``lock_factory``
  backed by a distributed lock (``doc_extractor.distributed_state``); two unsynchronized writers would fork the chain.
- Events are the identifiers-and-codes ``AuditEventRecord`` only (SEC-012): this module never stores document content.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, ContextManager, Dict, List, Optional, Tuple, Union
from uuid import uuid4

from doc_extractor.api.contracts.records import AuditEventRecord
from doc_extractor.api.exceptions import AuditUnavailableError
from doc_extractor.api.ports.audit import sanitize_audit_metadata
from doc_extractor.exceptions import StorageError
from doc_extractor.tenancy import TenantDirectoryResolver, ensure_inside_tenant

GENESIS_HASH = "0" * 64
_CHAIN_FILE_NAME = "events.jsonl"
_BLOCK_SIZE = 4096


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


# Public name for adapters (the MongoDB audit repository) that must store the EXACT text that was hashed. The format is unchanged.
canonical_json = _canonical_json


def compute_event_hash(prev_hash: str, seq: int, event: Dict[str, Any]) -> str:
    """SHA-256 over the previous hash, the sequence number and the canonical event JSON."""
    material = f"{prev_hash}|{seq}|{_canonical_json(event)}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


@dataclass(frozen=True)
class ChainVerification:
    """Outcome of verifying one tenant's chain. ``reason`` names the defect and never echoes event content."""

    tenant_id: str
    valid: bool
    checked_events: int
    first_bad_seq: Optional[int] = None
    reason: Optional[str] = None


def _read_chain_head(path: Path) -> Tuple[str, int]:
    """Hash and sequence number of the last record. An unreadable last record raises (writes are then refused)."""
    if not path.exists() or path.stat().st_size == 0:
        return GENESIS_HASH, 0
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        position = fh.tell()
        tail = b""
        while True:
            step = min(_BLOCK_SIZE, position)
            position -= step
            fh.seek(position)
            tail = fh.read(step) + tail
            stripped = tail.rstrip(b"\n")
            if position == 0 or b"\n" in stripped:
                break
    last_line = stripped.rsplit(b"\n", 1)[-1]
    record = json.loads(last_line)
    return str(record["hash"]), int(record["seq"])


def _truncate_quietly(path: Path, size: int) -> None:
    try:
        os.truncate(path, size)
    except OSError:
        # Could not roll back. The next append re-reads the head, so the chain stays linked, but this event exists
        # although its write was reported as failed; the operator must be alerted (SEC-021 audit-chain alert).
        pass


class HashChainedAuditRepository:
    """
    Durable ``AuditRepository`` with a per-tenant SHA-256 hash chain. Implements ``append_event`` and
    ``query_events`` (the port in ``doc_extractor.api.ports.repositories``) plus ``verify_chain``.
    """

    def __init__(
        self,
        base_dir: Union[str, Path],
        lock_factory: Optional[Callable[[str], ContextManager[Any]]] = None,
    ) -> None:
        self._resolver = TenantDirectoryResolver(base_dir)
        self._local_lock = threading.Lock()
        self._lock_factory = lock_factory or (lambda _tenant_id: self._local_lock)

    def _chain_path(self, tenant_id: str, *, create: bool) -> Path:
        tenant_dir = self._resolver.resolve(tenant_id, create=create)
        audit_dir = ensure_inside_tenant(tenant_dir, tenant_dir / "audit")
        if create:
            try:
                audit_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(message="Audit directory is not creatable.", details={"tenant_id": tenant_id}) from exc
        return ensure_inside_tenant(tenant_dir, audit_dir / _CHAIN_FILE_NAME)

    # -- AuditRepository port ----------------------------------------------------------------------------------

    async def append_event(self, event: AuditEventRecord) -> None:
        """Append one event. Raises ``StorageError`` if it was not durably written (never a silent loss)."""
        await asyncio.to_thread(self._append_sync, event)

    async def query_events(
        self,
        tenant_id: str,
        resource_id: Optional[str] = None,
        action: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[AuditEventRecord]:
        return await asyncio.to_thread(
            self._query_sync, tenant_id, resource_id, action, start_time, end_time, limit, offset
        )

    # -- chain operations ---------------------------------------------------------------------------------------

    def verify_chain(self, tenant_id: str) -> ChainVerification:
        """Recompute every hash for one tenant. Returns the first defect; a missing chain is valid and empty."""
        path = self._chain_path(tenant_id, create=False)
        if not path.exists():
            return ChainVerification(tenant_id=tenant_id, valid=True, checked_events=0)
        previous = GENESIS_HASH
        checked = 0
        with open(path, "rb") as fh:
            for line_number, raw in enumerate(fh, start=1):
                try:
                    record = json.loads(raw)
                    seq = int(record["seq"])
                    stored_prev = record["prev_hash"]
                    stored_hash = record["hash"]
                    body = record["event"]
                except (ValueError, KeyError, TypeError):
                    return ChainVerification(tenant_id, False, checked, line_number, "malformed record")
                if seq != line_number or stored_prev != previous:
                    return ChainVerification(tenant_id, False, checked, line_number, "sequence or link broken")
                if compute_event_hash(previous, seq, body) != stored_hash:
                    return ChainVerification(tenant_id, False, checked, line_number, "hash mismatch")
                if body.get("tenant_id") != tenant_id:
                    return ChainVerification(tenant_id, False, checked, line_number, "event belongs to another tenant")
                previous = stored_hash
                checked += 1
        return ChainVerification(tenant_id=tenant_id, valid=True, checked_events=checked)

    def _append_sync(self, event: AuditEventRecord) -> None:
        try:
            path = self._chain_path(event.tenant_id, create=True)
            with self._lock_factory(event.tenant_id):
                previous, last_seq = _read_chain_head(path)
                seq = last_seq + 1
                body = event.model_dump(mode="json")
                record_hash = compute_event_hash(previous, seq, body)
                line = _canonical_json({"seq": seq, "prev_hash": previous, "hash": record_hash, "event": body})
                size_before = path.stat().st_size if path.exists() else 0
                try:
                    with open(path, "ab") as fh:
                        fh.write(line.encode("utf-8") + b"\n")
                        fh.flush()
                        os.fsync(fh.fileno())
                except OSError:
                    # The event was not durably recorded, so the operation must not look audited: drop the partial line.
                    _truncate_quietly(path, size_before)
                    raise
        except StorageError:
            raise
        except (OSError, ValueError, KeyError) as exc:
            # Type only: the text of an OS or parse error can contain a path or record content.
            raise StorageError(
                message="Audit event could not be durably written.",
                details={"tenant_id": event.tenant_id, "error_type": type(exc).__name__},
            ) from exc

    def _query_sync(
        self,
        tenant_id: str,
        resource_id: Optional[str],
        action: Optional[str],
        start_time: Optional[datetime],
        end_time: Optional[datetime],
        limit: int,
        offset: int,
    ) -> List[AuditEventRecord]:
        path = self._chain_path(tenant_id, create=False)
        if not path.exists():
            return []
        matches: List[AuditEventRecord] = []
        with open(path, "rb") as fh:
            for raw in fh:
                event = AuditEventRecord.model_validate(json.loads(raw)["event"])
                if resource_id is not None and event.resource_id != resource_id:
                    continue
                if action is not None and event.action != action:
                    continue
                if start_time is not None and event.timestamp < start_time:
                    continue
                if end_time is not None and event.timestamp > end_time:
                    continue
                matches.append(event)
        return matches[offset : offset + limit]


class DurableAuditLogger:
    """
    ``AuditLogger`` over a durable ``AuditRepository``. A write failure propagates: mandatory operations (API-key
    creation and revocation, SEC-008) fail closed rather than continue unaudited.
    """

    propagates_failures = True

    def __init__(self, repository: Any) -> None:
        self._repository = repository

    async def log_event(self, event: AuditEventRecord) -> None:
        try:
            await self._repository.append_event(event)
        except StorageError as exc:
            raise AuditUnavailableError(operation=event.action) from exc

    async def log_action(
        self,
        actor_id: str,
        tenant_id: str,
        action: str,
        resource_id: str,
        resource_type: str,
        request_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        outcome: str = "SUCCESS",
    ) -> None:
        event = AuditEventRecord(
            event_id=f"audit_{uuid4().hex[:16]}",
            actor_id=actor_id,
            tenant_id=tenant_id,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            request_id=request_id,
            outcome=outcome,
            metadata=sanitize_audit_metadata(metadata),
        )
        await self.log_event(event)
