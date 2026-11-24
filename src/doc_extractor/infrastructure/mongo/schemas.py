"""
MongoDB Schema Repository (Team Member 3, Phase 5B).

Durable, tenant-isolated, concurrency-safe persistence adapter for the Schema Lifecycle
(SCH-001..006, SEC-003, SEC-006).

Implements the ``schema_lifecycle.repository.SchemaRepository`` abstract contract over MongoDB:
- Immutable versioned schema snapshots (SCH-006).
- Tenant-scoped active version pointers for production binding (SCH-004).
- Append-only schema lifecycle events ledger (SCH-006).
- Four-eyes approval records with single-use anti-replay tracking for benchmark run IDs (SCH-005).
- Atomic state promotion from TESTING to APPROVED with conditional find-and-update.
- Strict tenant boundary enforcement across all operations via ``TenantScopedRepository`` (SEC-006).
- Zero in-memory fallback in production: failures fail closed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
from datetime import datetime, timezone
import logging
import threading
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    PersistenceError,
    translate_driver_error,
)
from doc_extractor.infrastructure.mongo.indexes import (
    ASCENDING,
    DESCENDING,
    IndexBootstrapReport,
    IndexRegistry,
    IndexSpec,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.repository import (
    MAX_PAGE_SIZE,
    TenantScopedRepository,
    validate_tenant,
)
from schema_lifecycle.enums import SchemaStatus
from schema_lifecycle.events import LifecycleEvent
from schema_lifecycle.exceptions import (
    BenchmarkVerificationError,
    InvalidStateTransitionError,
    SchemaNotFoundError,
    SchemaVersionError,
    TenantAccessError,
)
from schema_lifecycle.repository import (
    SchemaApprovalRecord,
    SchemaRepository,
    SchemaSnapshot,
)

logger = logging.getLogger("doc_extractor.infrastructure.mongo.schemas")

# Collection names
SCHEMA_SNAPSHOTS = "schema_snapshots"
SCHEMA_ACTIVE_POINTERS = "schema_active_pointers"
SCHEMA_EVENTS = "schema_events"
SCHEMA_APPROVAL_RECORDS = "schema_approval_records"

# Declared index specifications (all tenant-first per SEC-006)
SCHEMA_INDEXES: Sequence[IndexSpec] = (
    IndexSpec(
        SCHEMA_SNAPSHOTS,
        (("tenant_id", ASCENDING), ("schema_id", ASCENDING), ("version", ASCENDING)),
        "ss_tenant_schema_version_uq",
        unique=True,
    ),
    IndexSpec(
        SCHEMA_SNAPSHOTS,
        (("tenant_id", ASCENDING), ("schema_id", ASCENDING), ("created_at", ASCENDING)),
        "ss_tenant_schema_created",
    ),
    IndexSpec(
        SCHEMA_ACTIVE_POINTERS,
        (("tenant_id", ASCENDING), ("schema_id", ASCENDING)),
        "sap_tenant_schema_uq",
        unique=True,
    ),
    IndexSpec(
        SCHEMA_EVENTS,
        (("tenant_id", ASCENDING), ("schema_id", ASCENDING), ("timestamp", ASCENDING)),
        "se_tenant_schema_time",
    ),
    IndexSpec(
        SCHEMA_EVENTS,
        (("tenant_id", ASCENDING), ("event_id", ASCENDING)),
        "se_tenant_event_uq",
        unique=True,
    ),
    IndexSpec(
        SCHEMA_APPROVAL_RECORDS,
        (("tenant_id", ASCENDING), ("schema_id", ASCENDING), ("version", ASCENDING)),
        "sar_tenant_schema_version_uq",
        unique=True,
    ),
    IndexSpec(
        SCHEMA_APPROVAL_RECORDS,
        (("tenant_id", ASCENDING), ("harness_run_id", ASCENDING)),
        "sar_tenant_harness_uq",
        unique=True,
    ),
)


async def ensure_schema_indexes(database: Any) -> IndexBootstrapReport:
    """Idempotently bootstrap indexes for schema lifecycle collections."""
    registry = IndexRegistry(SCHEMA_INDEXES)
    return await ensure_indexes(database, registry=registry)


def _run_coroutine_sync(coro: Any) -> Any:
    """
    Safely executes an asynchronous coroutine from a synchronous context.
    If an event loop is already running in the current thread (e.g. FastAPI worker),
    submits execution to a dedicated thread with its own loop to prevent blocking.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


class MongoSchemaRepository(SchemaRepository):
    """
    Durable MongoDB-backed implementation of SchemaRepository.

    Coordinates four tenant-isolated collections:
    - ``schema_snapshots``: Immutable definition snapshots per (tenant, schema, version).
    - ``schema_active_pointers``: Current production-active version pointer per (tenant, schema).
    - ``schema_events``: Immutable audit trail of state transitions and rollbacks.
    - ``schema_approval_records``: Four-eyes sign-offs and anti-replay consumed harness IDs.
    """

    def __init__(self, connection: Any) -> None:
        if hasattr(connection, "collection"):
            self._connection = connection
        else:
            class _ConnWrapper:
                def __init__(self, db: Any) -> None:
                    self._db = db
                def collection(self, name: str) -> Any:
                    return self._db[name]
                @property
                def database(self) -> Any:
                    return self._db
            self._connection = _ConnWrapper(connection)

        self._snapshots = TenantScopedRepository(self._connection, SCHEMA_SNAPSHOTS)
        self._pointers = TenantScopedRepository(self._connection, SCHEMA_ACTIVE_POINTERS)
        self._events = TenantScopedRepository(self._connection, SCHEMA_EVENTS)
        self._approvals = TenantScopedRepository(self._connection, SCHEMA_APPROVAL_RECORDS)
        self._lock = threading.RLock()

    # =========================================================================
    # Synchronous SchemaRepository Protocol Implementation
    # =========================================================================

    def save_snapshot(self, snapshot: SchemaSnapshot) -> None:
        """Persists an immutable schema snapshot. Fails if version already exists."""
        _run_coroutine_sync(self.async_save_snapshot(snapshot))

    def get_snapshot(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
    ) -> Optional[SchemaSnapshot]:
        """Retrieves a specific schema version snapshot for a tenant."""
        return _run_coroutine_sync(self.async_get_snapshot(schema_id, version, tenant_id))

    def get_latest_version(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> Optional[SchemaSnapshot]:
        """Retrieves the highest integer version snapshot for a schema in a tenant."""
        return _run_coroutine_sync(self.async_get_latest_version(schema_id, tenant_id))

    def list_versions(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> List[SchemaSnapshot]:
        """Lists all version snapshots for a schema in a tenant, sorted ascending by version."""
        return _run_coroutine_sync(self.async_list_versions(schema_id, tenant_id))

    def get_active_version(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> Optional[SchemaSnapshot]:
        """Retrieves the currently active approved schema version snapshot for production binding."""
        return _run_coroutine_sync(self.async_get_active_version(schema_id, tenant_id))

    def set_active_version(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
    ) -> None:
        """Sets the active version pointer for a schema in a tenant."""
        _run_coroutine_sync(self.async_set_active_version(schema_id, version, tenant_id))

    def update_status(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
        new_status: SchemaStatus,
    ) -> SchemaSnapshot:
        """Updates the lifecycle status of a specific schema version snapshot."""
        return _run_coroutine_sync(self.async_update_status(schema_id, version, tenant_id, new_status))

    def record_event(self, event: LifecycleEvent) -> None:
        """Appends a lifecycle event to the tenant-scoped audit ledger."""
        _run_coroutine_sync(self.async_record_event(event))

    def list_events(
        self,
        schema_id: str,
        tenant_id: str,
        version: Optional[int] = None,
    ) -> List[LifecycleEvent]:
        """Lists all audit events for a schema in a tenant, optionally filtered by version."""
        return _run_coroutine_sync(self.async_list_events(schema_id, tenant_id, version=version))

    def promote_and_activate(
        self,
        tenant_id: str,
        schema_id: str,
        version: int,
        approved_by: str,
        harness_run_id: str,
        event: LifecycleEvent,
        approval_record: Optional[SchemaApprovalRecord] = None,
    ) -> SchemaSnapshot:
        """
        Atomically promotes a schema version from TESTING to APPROVED, sets approved_by
        and harness_run_id, activates production pointer, and records approval records.
        """
        return _run_coroutine_sync(
            self.async_promote_and_activate(
                tenant_id=tenant_id,
                schema_id=schema_id,
                version=version,
                approved_by=approved_by,
                harness_run_id=harness_run_id,
                event=event,
                approval_record=approval_record,
            )
        )

    def get_approval_record(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
    ) -> Optional[SchemaApprovalRecord]:
        """Retrieves the approval record for a specific approved schema version."""
        return _run_coroutine_sync(self.async_get_approval_record(schema_id, version, tenant_id))

    def list_approval_records(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> List[SchemaApprovalRecord]:
        """Lists all approval records for a schema in a tenant."""
        return _run_coroutine_sync(self.async_list_approval_records(schema_id, tenant_id))

    def is_harness_run_consumed(
        self,
        harness_run_id: str,
        tenant_id: str,
    ) -> bool:
        """Checks if a harness run has already been consumed to approve a schema."""
        return _run_coroutine_sync(self.async_is_harness_run_consumed(harness_run_id, tenant_id))

    # =========================================================================
    # Asynchronous Implementation
    # =========================================================================

    async def async_save_snapshot(self, snapshot: SchemaSnapshot) -> None:
        tid = validate_tenant(snapshot.tenant_id)
        existing = await self._snapshots.find_one(
            tid,
            {"schema_id": snapshot.schema_id, "version": snapshot.version},
        )
        if existing is not None:
            raise SchemaVersionError(
                f"Schema '{snapshot.schema_id}' version {snapshot.version} already exists for tenant '{tid}'.",
                version=snapshot.version,
            )
        doc = snapshot.model_dump(mode="json")
        try:
            await self._snapshots.insert_one(tid, doc)
        except DuplicateKeyPersistenceError:
            raise SchemaVersionError(
                f"Schema '{snapshot.schema_id}' version {snapshot.version} already exists for tenant '{tid}'.",
                version=snapshot.version,
            )

    async def async_get_snapshot(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
    ) -> Optional[SchemaSnapshot]:
        tid = validate_tenant(tenant_id)
        doc = await self._snapshots.find_one(tid, {"schema_id": schema_id, "version": version})
        if not doc:
            return None
        return SchemaSnapshot.model_validate(doc)

    async def async_get_latest_version(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> Optional[SchemaSnapshot]:
        tid = validate_tenant(tenant_id)
        docs = await self._snapshots.find_many(
            tid,
            {"schema_id": schema_id},
            sort=(("version", DESCENDING),),
            limit=1,
        )
        if not docs:
            return None
        return SchemaSnapshot.model_validate(docs[0])

    async def async_list_versions(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> List[SchemaSnapshot]:
        tid = validate_tenant(tenant_id)
        docs = await self._snapshots.find_many(
            tid,
            {"schema_id": schema_id},
            sort=(("version", ASCENDING),),
            limit=MAX_PAGE_SIZE,
        )
        return [SchemaSnapshot.model_validate(d) for d in docs]

    async def async_get_active_version(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> Optional[SchemaSnapshot]:
        tid = validate_tenant(tenant_id)
        pointer = await self._pointers.find_one(tid, {"schema_id": schema_id})
        if not pointer:
            return None
        active_ver = pointer.get("active_version")
        if not active_ver:
            return None
        return await self.async_get_snapshot(schema_id, active_ver, tid)

    async def async_set_active_version(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
    ) -> None:
        tid = validate_tenant(tenant_id)
        now = datetime.now(timezone.utc).isoformat()
        existing = await self._pointers.find_one(tid, {"schema_id": schema_id})
        if existing is not None:
            await self._pointers.update_one(
                tid,
                {"schema_id": schema_id},
                {"$set": {"active_version": version, "updated_at": now}},
            )
        else:
            await self._pointers.insert_one(
                tid,
                {"schema_id": schema_id, "active_version": version, "updated_at": now},
            )

    async def async_update_status(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
        new_status: SchemaStatus,
    ) -> SchemaSnapshot:
        tid = validate_tenant(tenant_id)
        status_val = new_status.value if hasattr(new_status, "value") else str(new_status)
        existing = await self.async_get_snapshot(schema_id, version, tid)
        if existing is None:
            raise SchemaNotFoundError(
                f"Schema '{schema_id}' version {version} not found for tenant '{tid}'.",
                schema_id=schema_id,
                version=version,
                tenant_id=tid,
            )
        await self._snapshots.update_one(
            tid,
            {"schema_id": schema_id, "version": version},
            {"$set": {"status": status_val}},
        )
        updated = await self.async_get_snapshot(schema_id, version, tid)
        if updated is None:
            raise SchemaNotFoundError(f"Schema '{schema_id}' version {version} not found after update.")
        return updated

    async def async_record_event(self, event: LifecycleEvent) -> None:
        tid = validate_tenant(event.tenant_id)
        doc = event.model_dump(mode="json")
        await self._events.insert_one(tid, doc)

    async def async_list_events(
        self,
        schema_id: str,
        tenant_id: str,
        version: Optional[int] = None,
    ) -> List[LifecycleEvent]:
        tid = validate_tenant(tenant_id)
        flt: Dict[str, Any] = {"schema_id": schema_id}
        if version is not None:
            flt["version"] = version
        docs = await self._events.find_many(
            tid,
            flt,
            sort=(("timestamp", ASCENDING),),
            limit=MAX_PAGE_SIZE,
        )
        return [LifecycleEvent.model_validate(d) for d in docs]

    async def async_promote_and_activate(
        self,
        tenant_id: str,
        schema_id: str,
        version: int,
        approved_by: str,
        harness_run_id: str,
        event: LifecycleEvent,
        approval_record: Optional[SchemaApprovalRecord] = None,
    ) -> SchemaSnapshot:
        tid = validate_tenant(tenant_id)

        # 1. Verify existence
        existing = await self.async_get_snapshot(schema_id, version, tid)
        if existing is None:
            raise SchemaNotFoundError(
                f"Schema '{schema_id}' version {version} not found for tenant '{tid}'.",
                schema_id=schema_id,
                version=version,
                tenant_id=tid,
            )

        # 2. Verify state machine precondition (SCH-004)
        if existing.status != SchemaStatus.TESTING:
            raise InvalidStateTransitionError(
                message=(
                    f"Schema must be in 'testing' status before it can be approved, "
                    f"got current status '{existing.status.value}'."
                ),
                from_status=existing.status,
                to_status=SchemaStatus.APPROVED,
                schema_id=schema_id,
                version=version,
            )

        # 3. Verify single-use harness run anti-replay
        if harness_run_id and await self.async_is_harness_run_consumed(harness_run_id, tid):
            raise BenchmarkVerificationError(
                message=f"Harness run '{harness_run_id}' has already been consumed to approve another schema version.",
                harness_run_id=harness_run_id,
                schema_id=schema_id,
                version=version,
            )

        # 4. Atomic conditional promotion
        update_result = await self._snapshots.update_one(
            tid,
            {
                "schema_id": schema_id,
                "version": version,
                "status": SchemaStatus.TESTING.value,
            },
            {
                "$set": {
                    "status": SchemaStatus.APPROVED.value,
                    "approved_by": approved_by,
                    "harness_run_id": harness_run_id,
                }
            },
        )
        if getattr(update_result, "modified_count", 1) == 0:
            # Conditional match failed (race condition with concurrent promotion)
            current = await self.async_get_snapshot(schema_id, version, tid)
            raise InvalidStateTransitionError(
                message="Schema version is no longer in 'testing' status (concurrent promotion detected).",
                from_status=current.status if current else None,
                to_status=SchemaStatus.APPROVED,
                schema_id=schema_id,
                version=version,
            )

        # 5. Set production active pointer
        await self.async_set_active_version(schema_id, version, tid)

        # 6. Append lifecycle event
        await self.async_record_event(event)

        # 7. Persist approval record with anti-replay harness run ID
        if approval_record is None and harness_run_id:
            approval_record = SchemaApprovalRecord(
                tenant_id=tid,
                schema_id=schema_id,
                version=version,
                author_id=existing.author,
                approver_id=approved_by,
                harness_run_id=harness_run_id,
                lifecycle_event_id=event.event_id,
            )

        if approval_record is not None:
            try:
                await self._approvals.insert_one(tid, approval_record.model_dump(mode="json"))
            except DuplicateKeyPersistenceError:
                raise BenchmarkVerificationError(
                    message=f"Harness run '{harness_run_id}' has already been consumed.",
                    harness_run_id=harness_run_id,
                    schema_id=schema_id,
                    version=version,
                )

        promoted = await self.async_get_snapshot(schema_id, version, tid)
        if promoted is None:
            raise SchemaNotFoundError(f"Schema '{schema_id}' version {version} not found after promotion.")
        return promoted

    async def async_get_approval_record(
        self,
        schema_id: str,
        version: int,
        tenant_id: str,
    ) -> Optional[SchemaApprovalRecord]:
        tid = validate_tenant(tenant_id)
        doc = await self._approvals.find_one(tid, {"schema_id": schema_id, "version": version})
        if not doc:
            return None
        return SchemaApprovalRecord.model_validate(doc)

    async def async_list_approval_records(
        self,
        schema_id: str,
        tenant_id: str,
    ) -> List[SchemaApprovalRecord]:
        tid = validate_tenant(tenant_id)
        docs = await self._approvals.find_many(
            tid,
            {"schema_id": schema_id},
            sort=(("approval_timestamp", ASCENDING),),
            limit=MAX_PAGE_SIZE,
        )
        return [SchemaApprovalRecord.model_validate(d) for d in docs]

    async def async_is_harness_run_consumed(
        self,
        harness_run_id: str,
        tenant_id: str,
    ) -> bool:
        tid = validate_tenant(tenant_id)
        doc = await self._approvals.find_one(tid, {"harness_run_id": harness_run_id})
        if doc is not None:
            return True
        snap = await self._snapshots.find_one(tid, {"harness_run_id": harness_run_id})
        return snap is not None
