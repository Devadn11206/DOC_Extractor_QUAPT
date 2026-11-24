"""
MongoDB ``AuditRepository`` (Team Member 3, Phase 2B): a durable, tenant-isolated, append-only, hash-chained audit trail.

THE FORMAT IS THE EXISTING ONE. ``HashChainedAuditRepository`` (``api/audit_chain.py``) stores one chain per tenant where each
record carries ``seq``, ``prev_hash``, ``hash`` and the event, and ``hash = SHA-256(prev_hash | seq | canonical event JSON)``
(``compute_event_hash``, ``GENESIS_HASH``). This module uses those very functions: a chain written here and a chain written
there hash identically (a test appends the same events to both). Nothing is a second, incompatible format.

DOCUMENT (collection ``audit_events``, one per event)::

    tenant_id, seq (1, 2, 3 ...), prev_hash, hash, hash_version (1),
    event_json   the EXACT canonical JSON text of the event, which is what the hash covers,
    event_id, resource_id, action, timestamp_us   copies of the event's fields for the query indexes.

The event is stored as its canonical JSON text rather than as nested BSON so that the bytes that were hashed are the bytes that
are stored, whatever BSON would do to a nested field name or number. The copies are not trusted: ``verify_chain`` checks that each
still equals the hashed event, so editing a copy to change what a query returns is detected. ``timestamp_us`` is the exact
microsecond count (a BSON date would truncate to milliseconds and make time filters inexact).

SEQUENCE ALLOCATION (correct across processes; no process-local lock is involved). A writer reads the tenant's highest record,
computes ``seq + 1`` and ``hash`` from that record's hash, and inserts. The UNIQUE ``(tenant_id, seq)`` index (declared in
``indexes.py``) lets exactly one writer win each sequence number; a loser gets a duplicate-key error, re-reads the new head and
tries again, up to ``max_attempts`` times with a small random pause. Because a record is only ever inserted at the number after
the highest existing one, the chain has no gaps; because that number can be taken once, it cannot fork. Every round of a
contended append gives one writer success, so ``n`` simultaneous writers on ONE tenant need up to ``n`` attempts: the default bound
is generous, and when it is exhausted the append FAILS (typed, retryable) rather than being dropped. Any other driver failure fails
the append at once, with no blind retry: after an ambiguous failure a retry could record the event twice.

FAIL CLOSED. ``append_event`` returns only once the event is durably recorded (majority write concern with journal, imposed by the
client) and RAISES otherwise, always a ``PersistenceError`` (a ``StorageError``), so ``DurableAuditLogger`` turns it into
``AuditUnavailableError`` and a mandatory operation refuses to continue unaudited. The append also refuses to run unless the
unique index exists (without it two writers could fork the chain).

APPEND-ONLY. The repository's whole public surface is ``append_event``, ``query_events``, ``verify_chain`` and ``chain_head``.
It holds the tenant-scoped store by composition, not inheritance, so no update, delete or raw insert method exists to be misused.
This is an application-level guarantee: the database user should also hold no update or delete privilege on the collection.

LIMITS (stated, not hidden; see docs/team3-mongodb-foundation.md). The hash chain detects edits, reordering and removal of a record
that has a successor; it CANNOT detect the removal of the newest records, nor a rewrite of the whole tail by someone who can write
to the database, because nothing outside the database vouches for the chain head. That needs independently signed checkpoints and
an external tamper-resistant sink, which are future security work (SEC-008): this chain alone is NOT a claim of SEC-008 compliance.
Decision M5-D15 (best-effort versus durable sink) stays Open.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from pydantic import ValidationError

from doc_extractor.api.audit_chain import GENESIS_HASH, ChainVerification, canonical_json, compute_event_hash
from doc_extractor.api.contracts.records import AuditEventRecord
from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    PersistenceConfigurationError,
    PersistenceDataError,
    PersistenceError,
    PersistenceUnavailableError,
    UnsafeQueryError,
    translate_driver_error,
)
from doc_extractor.infrastructure.mongo.indexes import AUDIT_EVENTS
from doc_extractor.infrastructure.mongo.repository import MAX_PAGE_SIZE, TenantScopedRepository, validate_tenant
from doc_extractor.infrastructure.mongo.serialization import ensure_utc

HASH_VERSION = 1                                   # SHA-256(prev_hash | seq | canonical event JSON), as in audit_chain.py
DEFAULT_MAX_APPEND_ATTEMPTS = 64
MAX_EVENT_JSON_BYTES = 256 * 1024                  # sanitised audit metadata is tiny; this only refuses a runaway event
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ONE_MICROSECOND = timedelta(microseconds=1)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_CHAIN_ORDER = [("seq", 1)]
_REQUIRED_FIELDS = ("seq", "prev_hash", "hash", "hash_version", "event_json", "event_id", "resource_id", "action", "timestamp_us")

Pause = Callable[[float], Awaitable[Any]]


def microseconds(value: datetime) -> int:
    """Exact microseconds since the epoch, UTC (a naive datetime is read as UTC, the application-wide rule)."""
    return (ensure_utc(value) - _EPOCH) // _ONE_MICROSECOND


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class MongoAuditRepository:
    """The ``AuditRepository`` port over MongoDB, plus ``verify_chain`` and ``chain_head``. See the module docstring."""

    def __init__(
        self,
        connection: MongoConnection,
        *,
        max_attempts: int = DEFAULT_MAX_APPEND_ATTEMPTS,
        sleep: Pause = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if not _is_int(max_attempts) or not 1 <= max_attempts <= 1000:
            raise ValueError("max_attempts must be between 1 and 1000.")
        self._connection = connection
        self._store = TenantScopedRepository(connection, AUDIT_EVENTS)
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._jitter = jitter
        self._index_verified = False

    @property
    def collection_name(self) -> str:
        return AUDIT_EVENTS

    # -- the unique index is the correctness mechanism: it must exist ---------------------------------------------------

    async def _unique_sequence_index_exists(self) -> bool:
        collection = self._connection.collection(AUDIT_EVENTS)
        try:
            cursor = await collection.list_indexes()
            entries = [entry async for entry in cursor]
        except Exception as exc:                                         # noqa: BLE001 - mapped to a typed error
            if getattr(exc, "code", None) == 26:                         # NamespaceNotFound: no collection, so no index
                return False
            raise translate_driver_error(exc, collection=AUDIT_EVENTS) from None
        return any(
            entry.get("unique") is True and list(dict(entry.get("key", {})).items()) == [("tenant_id", 1), ("seq", 1)]
            for entry in entries
        )

    async def require_unique_sequence_index(self) -> None:
        """
        Start-up gate: raise ``PersistenceConfigurationError`` unless the unique ``(tenant_id, seq)`` index exists. Index bootstrap
        may be switched off in a deployment that migrates indexes separately; the audit chain is still unsafe without this one.
        """
        if not await self._unique_sequence_index_exists():
            raise PersistenceConfigurationError("audit_sequence_index_is_missing", setting="mongo_ensure_indexes")
        self._index_verified = True

    async def _require_index_once(self) -> None:
        if not self._index_verified:
            if not await self._unique_sequence_index_exists():
                raise PersistenceError("audit_sequence_index_is_missing", collection=AUDIT_EVENTS)
            self._index_verified = True

    # -- the AuditRepository port -------------------------------------------------------------------------------------------

    async def append_event(self, event: AuditEventRecord) -> None:
        """
        Durably append ``event`` to its tenant's chain. Returns only when it is recorded; raises a ``PersistenceError`` otherwise
        (an exhausted retry budget is ``PersistenceUnavailableError`` with reason ``audit_append_contended``).
        """
        if not isinstance(event, AuditEventRecord):
            raise UnsafeQueryError("event_is_not_an_audit_event_record")
        tenant = validate_tenant(event.tenant_id)
        await self._require_index_once()
        body = event.model_dump(mode="json")
        event_json = canonical_json(body)
        if len(event_json.encode("utf-8")) > MAX_EVENT_JSON_BYTES:
            raise PersistenceDataError("audit_event_is_too_large")
        for attempt in range(self._max_attempts):
            head_seq, previous = await self._head(tenant)
            seq = head_seq + 1
            document = {
                "tenant_id": tenant,
                "seq": seq,
                "prev_hash": previous,
                "hash": compute_event_hash(previous, seq, body),
                "hash_version": HASH_VERSION,
                "event_json": event_json,
                "event_id": event.event_id,
                "resource_id": event.resource_id,
                "action": event.action,
                "timestamp_us": microseconds(event.timestamp),
            }
            try:
                await self._store.insert_one(tenant, document)
                return
            except DuplicateKeyPersistenceError:
                # Another writer took this sequence number. If the record there is THIS event (same hash), it is already recorded.
                if await self._is_this_record(tenant, seq, document["hash"], event.event_id):
                    return
                await self._sleep(self._jitter() * min(0.002 * (attempt + 1), 0.05))
        raise PersistenceUnavailableError("audit_append_contended", collection=AUDIT_EVENTS)

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
        """The tenant's events in chain order, filtered as the port states; ``limit``/``offset`` page the filtered list."""
        tenant = validate_tenant(tenant_id)
        if not _is_int(limit) or not _is_int(offset) or limit < 0 or offset < 0:
            raise UnsafeQueryError("page_bounds_are_not_valid")
        selector: Dict[str, Any] = {}
        for name, value in (("resource_id", resource_id), ("action", action)):
            if value is not None:
                if not isinstance(value, str):                  # an exact value, never an operator expression
                    raise UnsafeQueryError("filter_value_is_not_a_string")
                selector[name] = value
        window: Dict[str, int] = {}
        for operator, bound in (("$gte", start_time), ("$lte", end_time)):
            if bound is not None:
                if not isinstance(bound, datetime):
                    raise UnsafeQueryError("time_bound_is_not_a_datetime")
                window[operator] = microseconds(bound)
        if window:
            selector["timestamp_us"] = window
        if limit == 0:
            return []
        documents = await self._store.find_window(tenant, selector, sort=_CHAIN_ORDER, skip=offset, limit=limit)
        return [self._event_of(tenant, document) for document in documents]

    # -- chain operations ---------------------------------------------------------------------------------------------------

    async def chain_head(self, tenant_id: str) -> Tuple[int, str]:
        """``(seq, hash)`` of the tenant's newest record; ``(0, GENESIS_HASH)`` for an empty chain."""
        return await self._head(validate_tenant(tenant_id))

    async def verify_chain(self, tenant_id: str, *, batch_size: int = 500) -> ChainVerification:
        """
        Recompute one tenant's whole chain, in batches. Returns the first defect (``reason`` never echoes event content); an empty
        chain is valid. Detects an edited, reordered, inserted, duplicated or removed record that has a successor, and any record
        that is not part of the chain; it cannot detect removal of the newest records (see the module docstring).

        Each batch re-reads the last verified record, so a second record at the same sequence number (a fork) is seen even when it
        falls across a batch boundary; and the number of records is compared with the number verified, so a record that sorts
        outside the numeric sequence (a damaged ``seq``) cannot hide.
        """
        tenant = validate_tenant(tenant_id)
        if not _is_int(batch_size) or not 1 <= batch_size <= MAX_PAGE_SIZE - 1:
            raise UnsafeQueryError("page_bounds_are_not_valid")
        previous, expected, checked = GENESIS_HASH, 1, 0
        while True:
            overlap = 1 if expected > 1 else 0
            batch = await self._store.find_many(
                tenant, {"seq": {"$gte": max(expected - 1, 1)}}, sort=_CHAIN_ORDER, skip=0, limit=batch_size + overlap
            )
            progressed = False
            for position, document in enumerate(batch):
                if overlap and position == 0 and document.get("seq") == expected - 1 and document.get("hash") == previous:
                    continue                                   # the record verified last, read again
                defect = self._defect(tenant, document, expected, previous)
                if defect is not None:
                    return ChainVerification(tenant, False, checked, expected, defect)
                previous = document["hash"]
                expected += 1
                checked += 1
                progressed = True
            if not progressed:
                break
        if await self._store.count(tenant) != checked:         # a record outside the numeric sequence, or one that was never reached
            return ChainVerification(tenant, False, checked, checked + 1, "sequence or link broken")
        return ChainVerification(tenant_id=tenant, valid=True, checked_events=checked)

    # -- internals ------------------------------------------------------------------------------------------------------------

    async def _head(self, tenant: str) -> Tuple[int, str]:
        document = await self._store.find_one(tenant, None, sort=[("seq", -1)])
        if document is None:
            return 0, GENESIS_HASH
        seq, record_hash = document.get("seq"), document.get("hash")
        if not _is_int(seq) or seq < 1 or not isinstance(record_hash, str) or not _SHA256_HEX.fullmatch(record_hash):
            raise PersistenceDataError("audit_chain_head_is_not_valid")     # never append onto a record that cannot be read
        return seq, record_hash

    async def _is_this_record(self, tenant: str, seq: int, record_hash: str, event_id: str) -> bool:
        document = await self._store.find_one(tenant, {"seq": seq})
        return document is not None and document.get("hash") == record_hash and document.get("event_id") == event_id

    @staticmethod
    def _event_of(tenant: str, document: Mapping[str, Any]) -> AuditEventRecord:
        try:
            event = AuditEventRecord.model_validate(json.loads(document["event_json"]))
        except (KeyError, TypeError, ValueError, ValidationError):
            raise PersistenceDataError("stored_audit_event_is_not_valid", collection=AUDIT_EVENTS) from None
        if event.tenant_id != tenant:
            raise PersistenceDataError("stored_audit_event_is_not_valid", collection=AUDIT_EVENTS)
        return event

    @staticmethod
    def _defect(tenant: str, document: Mapping[str, Any], expected: int, previous: str) -> Optional[str]:
        """The reason this record breaks the chain at ``expected``, or ``None``. Reasons are fixed texts."""
        if any(field not in document for field in _REQUIRED_FIELDS):
            return "malformed record"
        seq, prev_hash, record_hash = document["seq"], document["prev_hash"], document["hash"]
        if not _is_int(seq) or not _is_int(document["hash_version"]) or not _is_int(document["timestamp_us"]):
            return "malformed record"
        if not all(isinstance(document[field], str) for field in ("prev_hash", "hash", "event_json", "event_id", "resource_id", "action")):
            return "malformed record"
        if seq != expected or prev_hash != previous:
            return "sequence or link broken"
        if document["hash_version"] != HASH_VERSION:
            return "unsupported hash version"
        try:
            body = json.loads(document["event_json"])
            if not isinstance(body, dict):
                return "malformed record"
        except ValueError:
            return "malformed record"
        if compute_event_hash(previous, seq, body) != record_hash:
            return "hash mismatch"
        if body.get("tenant_id") != tenant:
            return "event belongs to another tenant"
        try:
            event = AuditEventRecord.model_validate(body)
        except ValidationError:
            return "event is not a valid audit event"
        if (
            event.event_id != document["event_id"] or event.resource_id != document["resource_id"]
            or event.action != document["action"] or microseconds(event.timestamp) != document["timestamp_us"]
        ):
            return "query fields differ from the event"
        return None
