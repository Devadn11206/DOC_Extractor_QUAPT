"""
Centralised index registration and idempotent index bootstrap (Team Member 3, Phase 2A-2).

Every index of every MongoDB collection is DECLARED here, in one registry, and nowhere else. Repositories do not create
indexes ad hoc. ``ensure_indexes`` applies the registry and is safe to run any number of times: an index that already exists
with identical options is left alone; one that exists with the same name but different options raises ``IndexConflictError``.
The bootstrap NEVER drops, renames or rebuilds an index: changing one is a deliberate migration, not a start-up side effect.

TENANT RULE (SEC-006). The first key of every index is ``tenant_id``. The one exception is declared explicitly with
``tenant_exempt=True`` and documented at its definition.

WHAT IS, AND IS NOT, UNIQUE
Only constraints that are intrinsic to a record's identity are unique. A unique constraint that would enforce a product rule
that has not been decided is NOT declared:

- ``review_tasks``: ``(tenant_id, task_id)`` is unique (a task identifier identifies one task). The tenant and document
  lookup ``(tenant_id, document_id, created_at)`` is deliberately NON-unique: whether a document may ever have more than one
  review task, or only one active task with history kept, is an open product question (DR-2 of the Phase 1.2 approval package).
- ``api_keys``: the credential hash is unique across all tenants (the credential identifies the tenant, so two records with one
  hash would make authentication ambiguous), and ``(tenant_id, key_id)`` is unique. This is the lookup contract of the
  approval package (3.3), pending Team 2's interface approval.

- ``audit_events``: ``(tenant_id, seq)`` is unique. It IS the append-only chain: two writers cannot both append at one sequence number
  of one tenant, which is what keeps the per-tenant hash chain free of forks and gaps across processes. The other audit indexes
  serve the existing query filters and are non-unique.

- ``processing_jobs`` (Phase 3, ``processing.py``): ``(tenant_id, job_key)`` is unique. It IS the job's identity: two workers cannot both create
  the record of one job, which is what makes the first claim race-free across processes. ``(tenant_id, state, lease_expires_at_us)`` serves
  operational lookups of expired leases and is non-unique.
- ``idempotency_records`` (Phase 3, ``idempotency.py``): ``(tenant_id, key_hash)`` is unique. It is the durable source of truth of idempotency:
  one live record per tenant and key, whichever replica wrote it and whatever happened to a Redis lock.
- Both collections also declare a TTL index on ``expires_at`` (a date that is only set on records that may be forgotten: completed jobs and
  idempotency records). A TTL index must be single-field, so it is the second declared ``tenant_exempt`` kind (the first is a unique credential
  hash). It never decides correctness: readers also check the expiry themselves, because the server removes expired documents only about once a minute.

Nothing here creates a collection's repository. The review-task repository (``review_tasks.py``, Phase 2A-3) uses these indexes
and declares none of its own, and so does the API-key repository (``api_keys.py``, Phase 2A-4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from doc_extractor.infrastructure.mongo.errors import (
    IndexConflictError,
    PersistenceError,
    translate_driver_error,
)

logger = logging.getLogger("doc_extractor.infrastructure.mongo")

ASCENDING = 1
DESCENDING = -1

REVIEW_TASKS = "review_tasks"
API_KEYS = "api_keys"
AUDIT_EVENTS = "audit_events"
PROCESSING_JOBS = "processing_jobs"
IDEMPOTENCY_RECORDS = "idempotency_records"

KeyPattern = Tuple[Tuple[str, int], ...]


@dataclass(frozen=True)
class IndexSpec:
    """One declared index. Immutable; validated on construction."""

    collection: str
    keys: KeyPattern
    name: str
    unique: bool = False
    partial_filter: Optional[Mapping[str, Any]] = None
    expire_after_seconds: Optional[int] = None
    tenant_exempt: bool = False

    def __post_init__(self) -> None:
        if not self.collection or not self.name or not self.keys:
            raise ValueError("An index needs a collection, a name and at least one key.")
        for key, direction in self.keys:
            if not isinstance(key, str) or not key or direction not in (ASCENDING, DESCENDING):
                raise ValueError("Index keys are (field, 1 or -1) pairs.")
        if not self.tenant_exempt and self.keys[0][0] != "tenant_id":
            raise ValueError("The first key of every index must be tenant_id (SEC-006).")
        if self.expire_after_seconds is not None and (len(self.keys) != 1 or self.unique):
            raise ValueError("A TTL index is a single-field, non-unique index.")
        if self.tenant_exempt and not (self.unique or self.expire_after_seconds is not None):
            raise ValueError("Only a unique or a TTL index may be exempt from the tenant-first rule.")

    def create_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"name": self.name, "unique": self.unique}
        if self.partial_filter is not None:
            kwargs["partialFilterExpression"] = dict(self.partial_filter)
        if self.expire_after_seconds is not None:
            kwargs["expireAfterSeconds"] = self.expire_after_seconds
        return kwargs


class IndexRegistry:
    """The single place where indexes are declared."""

    def __init__(self, specs: Iterable[IndexSpec] = ()) -> None:
        self._specs: List[IndexSpec] = []
        for spec in specs:
            self.register(spec)

    def register(self, spec: IndexSpec) -> None:
        for existing in self._specs:
            if existing.collection == spec.collection and existing.name == spec.name:
                raise ValueError("An index name is declared once per collection.")
            if existing.collection == spec.collection and existing.keys == spec.keys and existing.unique == spec.unique:
                raise ValueError("The same key pattern is declared twice.")
        self._specs.append(spec)

    def all(self) -> Tuple[IndexSpec, ...]:
        return tuple(self._specs)

    def for_collection(self, collection: str) -> Tuple[IndexSpec, ...]:
        return tuple(spec for spec in self._specs if spec.collection == collection)

    def collections(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for spec in self._specs:
            if spec.collection not in seen:
                seen.append(spec.collection)
        return tuple(seen)


def build_default_registry() -> IndexRegistry:
    """The indexes the foundation declares today. Later slices add their collections here, and only here."""
    return IndexRegistry(
        [
            # --- review_tasks (repository: ``review_tasks.py``) ---
            IndexSpec(REVIEW_TASKS, (("tenant_id", ASCENDING), ("task_id", ASCENDING)), "rt_tenant_task_uq", unique=True),
            # NON-unique on purpose: see the module docstring (open product rule).
            IndexSpec(
                REVIEW_TASKS,
                (("tenant_id", ASCENDING), ("document_id", ASCENDING), ("created_at", ASCENDING)),
                "rt_tenant_document_created",
            ),
            IndexSpec(
                REVIEW_TASKS,
                (("tenant_id", ASCENDING), ("status", ASCENDING), ("created_at", ASCENDING), ("task_id", ASCENDING)),
                "rt_tenant_status_created_task",
            ),
            IndexSpec(
                REVIEW_TASKS,
                (
                    ("tenant_id", ASCENDING),
                    ("assigned_reviewer_id", ASCENDING),
                    ("status", ASCENDING),
                    ("created_at", ASCENDING),
                    ("task_id", ASCENDING),
                ),
                "rt_tenant_reviewer_status_created_task",
            ),
            # --- api_keys (repository: ``api_keys.py``) ---
            # The credential hash identifies the tenant, so it is unique across tenants. This is the one index that is
            # not tenant-first, and the reason is the lookup itself: authentication has only the hash.
            IndexSpec(API_KEYS, (("key_hash", ASCENDING),), "ak_key_hash_uq", unique=True, tenant_exempt=True),
            IndexSpec(API_KEYS, (("tenant_id", ASCENDING), ("key_id", ASCENDING)), "ak_tenant_key_uq", unique=True),
            # --- audit_events (repository: ``audit.py``) ---
            # The unique tenant and sequence constraint is the correctness mechanism of the append-only hash chain (see the module
            # docstring and ``audit.py``). The query indexes end in ``seq`` so every result is already in chain order.
            IndexSpec(AUDIT_EVENTS, (("tenant_id", ASCENDING), ("seq", ASCENDING)), "audit_tenant_seq_uq", unique=True),
            IndexSpec(AUDIT_EVENTS, (("tenant_id", ASCENDING), ("resource_id", ASCENDING), ("seq", ASCENDING)), "audit_tenant_resource_seq"),
            IndexSpec(AUDIT_EVENTS, (("tenant_id", ASCENDING), ("action", ASCENDING), ("seq", ASCENDING)), "audit_tenant_action_seq"),
            IndexSpec(AUDIT_EVENTS, (("tenant_id", ASCENDING), ("timestamp_us", ASCENDING), ("seq", ASCENDING)), "audit_tenant_time_seq"),
            # --- processing_jobs (repository: ``processing.py``) ---
            IndexSpec(PROCESSING_JOBS, (("tenant_id", ASCENDING), ("job_key", ASCENDING)), "pj_tenant_job_uq", unique=True),
            IndexSpec(
                PROCESSING_JOBS,
                (("tenant_id", ASCENDING), ("state", ASCENDING), ("lease_expires_at_us", ASCENDING)),
                "pj_tenant_state_lease",
            ),
            IndexSpec(PROCESSING_JOBS, (("expires_at", ASCENDING),), "pj_expires_ttl", expire_after_seconds=0, tenant_exempt=True),
            # --- idempotency_records (repository: ``idempotency.py``) ---
            IndexSpec(IDEMPOTENCY_RECORDS, (("tenant_id", ASCENDING), ("key_hash", ASCENDING)), "idem_tenant_key_uq", unique=True),
            IndexSpec(IDEMPOTENCY_RECORDS, (("expires_at", ASCENDING),), "idem_expires_ttl", expire_after_seconds=0, tenant_exempt=True),
        ]
    )


DEFAULT_REGISTRY = build_default_registry()


@dataclass(frozen=True)
class IndexBootstrapReport:
    """What a bootstrap run did. Names only; no data."""

    created: Tuple[str, ...] = ()
    existing: Tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.created)


async def _existing_names(collection: Any) -> List[str]:
    """Names of the indexes already on ``collection`` (an absent collection has none)."""
    try:
        cursor = await collection.list_indexes()
        return [entry["name"] async for entry in cursor]
    except Exception as exc:                                              # noqa: BLE001
        error = translate_driver_error(exc, collection=getattr(collection, "name", None))
        # A collection that does not exist yet is reported by the server as an error code 26 (NamespaceNotFound).
        if getattr(exc, "code", None) == 26:
            return []
        raise error from None


async def unique_index_exists(collection: Any, keys: Sequence[Tuple[str, int]]) -> bool:
    """
    True when ``collection`` has a UNIQUE index with exactly the key pattern ``keys`` (an absent collection has none). Raises a typed
    persistence error on a database failure. A repository whose correctness rests on a unique index (the audit chain, the job
    record, the idempotency record) uses it to refuse to run without that index, even when start-up bootstrap is switched off.
    """
    wanted = [(field, direction) for field, direction in keys]
    try:
        cursor = await collection.list_indexes()
        entries = [entry async for entry in cursor]
    except Exception as exc:                                              # noqa: BLE001 - mapped to a typed error
        if getattr(exc, "code", None) == 26:                              # NamespaceNotFound: no collection, so no index
            return False
        raise translate_driver_error(exc, collection=getattr(collection, "name", None)) from None
    return any(entry.get("unique") is True and list(dict(entry.get("key", {})).items()) == wanted for entry in entries)


async def ensure_indexes(
    database: Any,
    registry: IndexRegistry = DEFAULT_REGISTRY,
    *,
    only_collections: Optional[Sequence[str]] = None,
) -> IndexBootstrapReport:
    """
    Create every declared index that is missing. Idempotent. Raises ``IndexConflictError`` if an index of the same name
    exists with different options, and any other typed persistence error on a database failure. Never drops an index.
    """
    created: List[str] = []
    existing: List[str] = []
    for collection_name in registry.collections():
        if only_collections is not None and collection_name not in only_collections:
            continue
        collection = database[collection_name]
        present = await _existing_names(collection)
        for spec in registry.for_collection(collection_name):
            qualified = f"{collection_name}.{spec.name}"
            try:
                await collection.create_index(list(spec.keys), **spec.create_kwargs())
            except PersistenceError:
                raise
            except Exception as exc:                                      # noqa: BLE001
                error = translate_driver_error(exc, collection=collection_name)
                if isinstance(error, IndexConflictError):
                    logger.error("Index conflict (collection=%s index=%s)", collection_name, spec.name)
                raise error from None
            (existing if spec.name in present else created).append(qualified)
    report = IndexBootstrapReport(created=tuple(created), existing=tuple(existing))
    logger.info("MongoDB index bootstrap finished (created=%d existing=%d)", len(created), len(existing))
    return report
