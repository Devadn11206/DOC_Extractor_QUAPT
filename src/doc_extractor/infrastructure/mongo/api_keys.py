"""
MongoDB ``ApiKeyRepository`` (Team Member 3, Phase 2A-4).

The production adapter of the ``ApiKeyRepository`` port (``doc_extractor.api.ports.repositories``). The port, ``ApiKeyRecord``,
the API-key service, the authentication provider and the reference test double are the contract; this module re-expresses
them over MongoDB and decides nothing new. It stores and returns RECORDS. Whether a key may authenticate (active, revoked,
expired, well-formed tenant) is decided by ``ApiKeyAuthenticationProvider``, exactly as with the reference double, so a lookup
returns a revoked or expired record rather than hiding it.

SECRETS. Only the SHA-256 digest of a credential is ever stored (``key_hash``) next to the visible prefix. ``ApiKeyRecord``
has no field for a secret, and ``create`` additionally refuses a ``key_hash`` that is not a lowercase 64-character hexadecimal
digest, so a raw token (which can also be 64 characters long) cannot be stored by mistake. Nothing here logs, and every error
has a fixed message: no hash, key identifier, tenant or driver text is ever quoted.

TENANTS. Every operation is tenant-scoped through ``TenantScopedRepository`` EXCEPT ``get_by_key_hash``: authentication has
only the credential hash and must learn the tenant from the record, so it uses the base class's single, opt-in, tenant-exempt
read (``key_hash`` is declared as a global unique field). It is a unique equality lookup on the tenant-exempt unique index; the
record's own ``tenant_id`` is what the caller then uses. Nothing else bypasses the tenant filter, and no write ever does.

IDENTITY (indexes declared in ``indexes.py``, none added here): ``key_hash`` is unique across all tenants and
``(tenant_id, key_id)`` is unique. A violation of either is one error type with one fixed reason, so a duplicate credential
hash in another tenant is not distinguishable from a duplicate key identifier.

STATE. ``revoke`` is conditional and idempotent: it never rewrites the time of an earlier revocation and never leaves a key
active once it returns ``True``. ``record_last_used`` touches only an ACTIVE key (the port) and only ever moves the timestamp
FORWARD, so concurrent requests finishing out of order cannot make it go back.

LIMITS (documented, not hidden). ``list_keys`` orders by creation time in the query; no index covers that order, so the server
sorts the tenant's keys (a tenant has few). A stored record that does not match ``ApiKeyRecord`` is a typed
``PersistenceDataError`` on every read that touches it (never silently skipped or returned). ``record_last_used`` is one
acknowledged write per authenticated request.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, List, Mapping, Optional

from doc_extractor.api.contracts.records import ApiKeyRecord, utc_now
from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    DuplicateKeyPersistenceError,
    PersistenceDataError,
    UnsafeQueryError,
)
from doc_extractor.infrastructure.mongo.indexes import API_KEYS
from doc_extractor.infrastructure.mongo.repository import TenantScopedRepository, validate_tenant
from doc_extractor.infrastructure.mongo.serialization import from_document, to_document, to_value

ORDER_BY_CREATION = (("created_at", 1), ("key_id", 1))
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class DuplicateApiKeyError(DuplicateKeyPersistenceError, ValueError):
    """
    ``create`` of a key whose ``(tenant, key_id)`` or ``key_hash`` already exists. One reason for both, so the answer reveals
    nothing about other tenants. A ``ValueError`` as well, like the duplicate error of the review-task adapter.
    """


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise UnsafeQueryError(f"{name}_is_not_a_valid_string")
    return value


def _record(document: Mapping[str, Any]) -> ApiKeyRecord:
    return from_document(ApiKeyRecord, document)


class MongoApiKeyRepository(TenantScopedRepository):
    """The ``ApiKeyRepository`` port over MongoDB. See the module docstring."""

    def __init__(self, connection: MongoConnection, *, clock: Callable[[], datetime] = utc_now) -> None:
        """``clock`` supplies the revocation time (the port has no time argument); the default is the current UTC time."""
        super().__init__(connection, API_KEYS, global_unique_fields=("key_hash",))
        self._now = clock

    # ---- lookups ----------------------------------------------------------------------------------------------------------

    async def get_by_key_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        """
        The key whose credential hash is ``key_hash``, in any state (the authentication provider decides revocation and
        expiry), or ``None``. NOT tenant-scoped by design: the owning tenant is read from the returned record.
        """
        document = await self.find_one_by_global_unique_key("key_hash", _text(key_hash, "key_hash"))
        return None if document is None else _record(document)

    async def get_by_id(self, tenant_id: str, key_id: str) -> Optional[ApiKeyRecord]:
        """The tenant's key ``key_id`` in any state (a revoked key is still inspectable); another tenant's key is absent."""
        document = await self.find_one(tenant_id, {"key_id": _text(key_id, "key_id")})
        return None if document is None else _record(document)

    async def list_keys(self, tenant_id: str, limit: int = 50, offset: int = 0) -> List[ApiKeyRecord]:
        """The tenant's keys of every state, oldest first (``created_at``, then ``key_id``)."""
        if limit < 0 or offset < 0:
            raise UnsafeQueryError("page_bounds_are_not_valid")
        if limit == 0:
            validate_tenant(tenant_id)
            return []
        documents = await self.find_window(tenant_id, {}, sort=ORDER_BY_CREATION, skip=offset, limit=limit)
        return [_record(document) for document in documents]

    # ---- writes -----------------------------------------------------------------------------------------------------------

    async def create(self, tenant_id: str, key_record: ApiKeyRecord) -> ApiKeyRecord:
        """
        Store ``key_record`` for ``tenant_id`` and return it as stored (UTC, millisecond precision). A record naming another
        tenant raises ``TenantScopeError``; a ``key_hash`` that is not a SHA-256 hex digest raises ``PersistenceDataError``
        (nothing is written); a duplicate ``key_hash`` or ``(tenant, key_id)`` raises ``DuplicateApiKeyError``.
        """
        if not isinstance(key_record, ApiKeyRecord):
            raise UnsafeQueryError("key_is_not_an_api_key_record")
        if not _SHA256_HEX.match(key_record.key_hash):
            raise PersistenceDataError("key_hash_is_not_a_sha256_digest")
        document = key_record.model_dump()
        try:
            await self.insert_one(tenant_id, document)
        except DuplicateKeyPersistenceError:
            raise DuplicateApiKeyError(collection=self.collection_name) from None
        return _record(to_document(document))

    async def revoke(self, tenant_id: str, key_id: str) -> bool:
        """
        Revoke the tenant's key at once: ``is_active`` false and ``revoked_at`` set. ``True`` when the key exists (an already
        revoked key stays revoked and keeps the time of its FIRST revocation, so a repeat is safe), ``False`` when the tenant
        has no such key. Two conditional updates, each atomic, so no concurrent writer can leave the key active afterwards.
        """
        key_id = _text(key_id, "key_id")
        moment = to_value(self._now())
        first = await self.update_one(
            tenant_id, {"key_id": key_id, "revoked_at": None}, {"$set": {"revoked_at": moment, "is_active": False}}
        )
        if first:
            return True
        # Already carries a revocation time (or is absent): make sure it is also inactive, keeping the earlier time.
        return bool(await self.update_one(tenant_id, {"key_id": key_id}, {"$set": {"is_active": False}}))

    async def record_last_used(self, tenant_id: str, key_id: str, last_used_at: datetime) -> bool:
        """
        Record ``last_used_at`` for an ACTIVE key, never moving it backwards. ``True`` when the key is active and now holds this
        time or a later one; ``False`` when the tenant has no such key or it is revoked or inactive (nothing is written).
        """
        key_id = _text(key_id, "key_id")
        if not isinstance(last_used_at, datetime):
            raise UnsafeQueryError("timestamp_is_not_a_datetime")
        moment = to_value(last_used_at)
        active = {"key_id": key_id, "is_active": True, "revoked_at": None}
        advanced = await self.update_one(
            tenant_id,
            {**active, "$or": [{"last_used_at": None}, {"last_used_at": {"$lt": moment}}]},
            {"$set": {"last_used_at": moment}},
        )
        if advanced:
            return True
        # Not advanced: either a later time is already recorded (still a success) or there is no active key.
        return await self.find_one(tenant_id, active) is not None
