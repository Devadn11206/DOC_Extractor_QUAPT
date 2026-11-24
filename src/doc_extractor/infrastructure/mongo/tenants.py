"""
MongoDB-backed tenant repository (Team Member 3, Phase 5C).

Provides durable, indexed persistence for tenant accounts and lifecycle state.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Sequence

from doc_extractor.infrastructure.mongo.errors import DuplicateKeyPersistenceError, PersistenceError, translate_driver_error
from doc_extractor.infrastructure.mongo.indexes import (
    ASCENDING,
    IndexBootstrapReport,
    IndexRegistry,
    IndexSpec,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.repository import MAX_PAGE_SIZE, validate_tenant
from doc_extractor.infrastructure.tenancy.models import TenantRecord, TenantStatus

logger = logging.getLogger("doc_extractor.infrastructure.mongo.tenants")

TENANTS_COLLECTION = "tenants"

TENANT_INDEXES: Sequence[IndexSpec] = (
    IndexSpec(
        TENANTS_COLLECTION,
        (("tenant_id", ASCENDING),),
        "tnt_id_uq",
        unique=True,
        tenant_exempt=True,
    ),
)


async def ensure_tenant_indexes(database: Any) -> IndexBootstrapReport:
    """Idempotently bootstrap indexes for tenants collection."""
    registry = IndexRegistry(TENANT_INDEXES)
    return await ensure_indexes(database, registry=registry)


def _run_coroutine_sync(coro: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


class _ConnWrapper:
    def __init__(self, db: Any) -> None:
        self._db = db

    def collection(self, name: str) -> Any:
        return self._db[name]

    @property
    def database(self) -> Any:
        return self._db


class MongoTenantRepository:
    """
    Durable MongoDB repository managing provisioned tenants.
    """

    def __init__(self, connection: Any) -> None:
        if hasattr(connection, "collection"):
            self._connection = connection
        else:
            self._connection = _ConnWrapper(connection)
        self._col = self._connection.collection(TENANTS_COLLECTION)

    def _doc_to_record(self, doc: Dict[str, Any]) -> TenantRecord:
        return TenantRecord(
            tenant_id=doc["tenant_id"],
            name=doc["name"],
            status=TenantStatus(doc["status"]),
            config=doc.get("config", {}),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
            created_by=doc.get("created_by", "system"),
            suspended_at=doc.get("suspended_at"),
            deactivated_at=doc.get("deactivated_at"),
            suspension_reason=doc.get("suspension_reason"),
        )

    async def async_create_tenant(self, tenant: TenantRecord) -> TenantRecord:
        validate_tenant(tenant.tenant_id)
        doc = {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "status": tenant.status.value,
            "config": tenant.config,
            "created_at": tenant.created_at,
            "updated_at": tenant.updated_at,
            "created_by": tenant.created_by,
            "suspended_at": tenant.suspended_at,
            "deactivated_at": tenant.deactivated_at,
            "suspension_reason": tenant.suspension_reason,
        }
        try:
            await self._col.insert_one(doc)
        except Exception as exc:
            raise translate_driver_error(exc, collection=TENANTS_COLLECTION) from None
        return tenant

    def create_tenant(self, tenant: TenantRecord) -> TenantRecord:
        return _run_coroutine_sync(self.async_create_tenant(tenant))

    async def async_get_tenant(self, tenant_id: str) -> Optional[TenantRecord]:
        validate_tenant(tenant_id)
        try:
            doc = await self._col.find_one({"tenant_id": tenant_id})
        except Exception as exc:
            raise translate_driver_error(exc, collection=TENANTS_COLLECTION) from None
        return self._doc_to_record(doc) if doc else None

    def get_tenant(self, tenant_id: str) -> Optional[TenantRecord]:
        return _run_coroutine_sync(self.async_get_tenant(tenant_id))

    async def async_update_tenant(
        self,
        tenant_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[TenantRecord]:
        validate_tenant(tenant_id)
        allowed: Dict[str, Any] = {}
        for k in ("name", "status", "config", "suspended_at", "deactivated_at", "suspension_reason"):
            if k in update_data:
                val = update_data[k]
                if k == "status" and isinstance(val, TenantStatus):
                    allowed[k] = val.value
                else:
                    allowed[k] = val
        if not allowed:
            return await self.async_get_tenant(tenant_id)

        allowed["updated_at"] = datetime.now(timezone.utc)
        try:
            res = await self._col.update_one({"tenant_id": tenant_id}, {"$set": allowed})
            if res.matched_count == 0:
                return None
        except Exception as exc:
            raise translate_driver_error(exc, collection=TENANTS_COLLECTION) from None
        return await self.async_get_tenant(tenant_id)

    def update_tenant(
        self,
        tenant_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[TenantRecord]:
        return _run_coroutine_sync(self.async_update_tenant(tenant_id, update_data))

    async def async_delete_tenant(self, tenant_id: str) -> bool:
        validate_tenant(tenant_id)
        try:
            res = await self._col.delete_one({"tenant_id": tenant_id})
            return bool(res.deleted_count > 0)
        except Exception as exc:
            raise translate_driver_error(exc, collection=TENANTS_COLLECTION) from None

    def delete_tenant(self, tenant_id: str) -> bool:
        return _run_coroutine_sync(self.async_delete_tenant(tenant_id))

    async def async_list_tenants(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[TenantRecord]:
        query: Dict[str, Any] = {}
        if status:
            query["status"] = status
        capped = min(max(1, limit), MAX_PAGE_SIZE)
        try:
            cursor = self._col.find(query).sort("created_at", ASCENDING).skip(offset).limit(capped)
            docs = await cursor.to_list(length=capped)
        except Exception as exc:
            raise translate_driver_error(exc, collection=TENANTS_COLLECTION) from None
        return [self._doc_to_record(d) for d in docs]

    def list_tenants(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[TenantRecord]:
        return _run_coroutine_sync(self.async_list_tenants(status, limit, offset))
