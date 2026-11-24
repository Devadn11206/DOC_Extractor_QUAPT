"""
MongoDB-backed webhook subscription and delivery repositories (Team Member 3, Phase 5C).

Implements:
- WebhookSubscriptionRepository (PRD §7.1, API-008).
- WebhookDeliveryRepository (PRD §7.1, SCR-07).
- Strict tenant isolation (PRD §10.1 SEC-006).
- Secret reference persistence (M5-D09).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Sequence

from doc_extractor.api.contracts.records import WebhookDeliveryRecord, WebhookSubscriptionRecord
from doc_extractor.api.ports.repositories import WebhookDeliveryRepository, WebhookSubscriptionRepository
from doc_extractor.infrastructure.mongo.errors import PersistenceError, translate_driver_error
from doc_extractor.infrastructure.mongo.indexes import (
    ASCENDING,
    DESCENDING,
    IndexBootstrapReport,
    IndexRegistry,
    IndexSpec,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.repository import MAX_PAGE_SIZE, TenantScopedRepository, validate_tenant
from doc_extractor.infrastructure.webhook.security import validate_webhook_destination

logger = logging.getLogger("doc_extractor.infrastructure.mongo.webhooks")

WEBHOOK_SUBSCRIPTIONS = "webhook_subscriptions"
WEBHOOK_DELIVERIES = "webhook_deliveries"

WEBHOOK_INDEXES: Sequence[IndexSpec] = (
    # Subscriptions
    IndexSpec(
        WEBHOOK_SUBSCRIPTIONS,
        (("tenant_id", ASCENDING), ("subscription_id", ASCENDING)),
        "whs_tenant_sub_uq",
        unique=True,
    ),
    IndexSpec(
        WEBHOOK_SUBSCRIPTIONS,
        (("tenant_id", ASCENDING), ("is_active", ASCENDING)),
        "whs_tenant_active",
    ),
    IndexSpec(
        WEBHOOK_SUBSCRIPTIONS,
        (("tenant_id", ASCENDING), ("event_types", ASCENDING)),
        "whs_tenant_events",
    ),
    # Deliveries
    IndexSpec(
        WEBHOOK_DELIVERIES,
        (("tenant_id", ASCENDING), ("delivery_id", ASCENDING)),
        "whd_tenant_delivery_uq",
        unique=True,
    ),
    IndexSpec(
        WEBHOOK_DELIVERIES,
        (("tenant_id", ASCENDING), ("subscription_id", ASCENDING), ("created_at", ASCENDING)),
        "whd_tenant_sub_created",
    ),
    IndexSpec(
        WEBHOOK_DELIVERIES,
        (("tenant_id", ASCENDING), ("status", ASCENDING)),
        "whd_tenant_status",
    ),
    IndexSpec(
        WEBHOOK_DELIVERIES,
        (("tenant_id", ASCENDING), ("event_id", ASCENDING)),
        "whd_tenant_event",
    ),
)


async def ensure_webhook_indexes(database: Any) -> IndexBootstrapReport:
    """Idempotently bootstrap indexes for webhook subscriptions and deliveries."""
    registry = IndexRegistry(WEBHOOK_INDEXES)
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


class MongoWebhookSubscriptionRepository(TenantScopedRepository, WebhookSubscriptionRepository):
    """
    Durable MongoDB-backed WebhookSubscriptionRepository with strict tenant isolation.
    """

    def __init__(self, connection: Any, *, validate_destination: bool = True, allow_loopback: bool = False) -> None:
        conn = connection if hasattr(connection, "collection") else _ConnWrapper(connection)
        super().__init__(conn, WEBHOOK_SUBSCRIPTIONS)
        self._validate_destination = validate_destination
        self._allow_loopback = allow_loopback

    def _doc_to_record(self, doc: Dict[str, Any]) -> WebhookSubscriptionRecord:
        return WebhookSubscriptionRecord(
            subscription_id=doc["subscription_id"],
            tenant_id=doc["tenant_id"],
            target_url=doc["target_url"],
            event_types=doc["event_types"],
            secret_hash=doc["secret_hash"],
            secret_ref=doc.get("secret_ref"),
            is_active=bool(doc.get("is_active", True)),
            created_at=doc["created_at"],
            updated_at=doc["updated_at"],
            metadata=doc.get("metadata", {}),
        )

    async def get_by_id(self, tenant_id: str, subscription_id: str) -> Optional[WebhookSubscriptionRecord]:
        doc = await self.find_one(tenant_id, {"subscription_id": subscription_id})
        return self._doc_to_record(doc) if doc else None

    async def list_by_tenant(
        self,
        tenant_id: str,
        event_type: Optional[str] = None,
    ) -> List[WebhookSubscriptionRecord]:
        query: Dict[str, Any] = {"is_active": True}
        if event_type:
            query["event_types"] = event_type
        docs = await self.find_many(tenant_id, query, limit=MAX_PAGE_SIZE)
        records = [self._doc_to_record(d) for d in docs]
        if event_type:
            records = [r for r in records if event_type in r.event_types]
        return records

    async def create(
        self,
        tenant_id: str,
        subscription: WebhookSubscriptionRecord,
    ) -> WebhookSubscriptionRecord:
        validate_tenant(tenant_id)
        if subscription.tenant_id != tenant_id:
            raise ValueError(f"Subscription tenant '{subscription.tenant_id}' does not match scope '{tenant_id}'.")

        # Validate destination URL against SSRF rules if configured
        if self._validate_destination:
            validate_webhook_destination(
                subscription.target_url,
                allow_loopback=self._allow_loopback,
                resolve_dns=False,  # Skip DNS at insert, check at dispatch time
            )

        doc = {
            "subscription_id": subscription.subscription_id,
            "tenant_id": tenant_id,
            "target_url": subscription.target_url,
            "event_types": subscription.event_types,
            "secret_hash": subscription.secret_hash,
            "secret_ref": subscription.secret_ref,
            "is_active": subscription.is_active,
            "created_at": subscription.created_at,
            "updated_at": subscription.updated_at,
            "metadata": subscription.metadata,
        }
        await self.insert_one(tenant_id, doc)
        return subscription

    async def update(
        self,
        tenant_id: str,
        subscription_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[WebhookSubscriptionRecord]:
        validate_tenant(tenant_id)
        allowed: Dict[str, Any] = {}
        for k in ("target_url", "event_types", "is_active", "metadata", "secret_hash", "secret_ref"):
            if k in update_data:
                allowed[k] = update_data[k]

        if "target_url" in allowed and self._validate_destination:
            validate_webhook_destination(allowed["target_url"], allow_loopback=self._allow_loopback, resolve_dns=False)

        if not allowed:
            return await self.get_by_id(tenant_id, subscription_id)

        allowed["updated_at"] = datetime.now(timezone.utc)
        res = await self.update_one(tenant_id, {"subscription_id": subscription_id}, {"$set": allowed})
        if res == 0:
            return None
        return await self.get_by_id(tenant_id, subscription_id)

    async def delete(self, tenant_id: str, subscription_id: str) -> bool:
        validate_tenant(tenant_id)
        res = await self.delete_one(tenant_id, {"subscription_id": subscription_id})
        return bool(res > 0)

    # Sync bridge methods
    def get_by_id_sync(self, tenant_id: str, subscription_id: str) -> Optional[WebhookSubscriptionRecord]:
        return _run_coroutine_sync(self.get_by_id(tenant_id, subscription_id))

    def list_by_tenant_sync(self, tenant_id: str, event_type: Optional[str] = None) -> List[WebhookSubscriptionRecord]:
        return _run_coroutine_sync(self.list_by_tenant(tenant_id, event_type))

    def create_sync(self, tenant_id: str, subscription: WebhookSubscriptionRecord) -> WebhookSubscriptionRecord:
        return _run_coroutine_sync(self.create(tenant_id, subscription))

    def update_sync(self, tenant_id: str, subscription_id: str, update_data: Dict[str, Any]) -> Optional[WebhookSubscriptionRecord]:
        return _run_coroutine_sync(self.update(tenant_id, subscription_id, update_data))

    def delete_sync(self, tenant_id: str, subscription_id: str) -> bool:
        return _run_coroutine_sync(self.delete(tenant_id, subscription_id))


class MongoWebhookDeliveryRepository(TenantScopedRepository, WebhookDeliveryRepository):
    """
    Durable MongoDB-backed WebhookDeliveryRepository tracking dispatch attempts and outcomes.
    """

    def __init__(self, connection: Any) -> None:
        conn = connection if hasattr(connection, "collection") else _ConnWrapper(connection)
        super().__init__(conn, WEBHOOK_DELIVERIES)

    def _doc_to_record(self, doc: Dict[str, Any]) -> WebhookDeliveryRecord:
        return WebhookDeliveryRecord(
            delivery_id=doc["delivery_id"],
            subscription_id=doc["subscription_id"],
            tenant_id=doc["tenant_id"],
            event_id=doc["event_id"],
            event_type=doc["event_type"],
            attempt_number=int(doc.get("attempt_number", 1)),
            status=doc.get("status", "pending"),
            response_status_code=doc.get("response_status_code"),
            error_message=doc.get("error_message"),
            created_at=doc["created_at"],
            completed_at=doc.get("completed_at"),
            next_retry_at=doc.get("next_retry_at"),
        )

    async def record_delivery(
        self,
        tenant_id: str,
        delivery: WebhookDeliveryRecord,
    ) -> WebhookDeliveryRecord:
        validate_tenant(tenant_id)
        if delivery.tenant_id != tenant_id:
            raise ValueError(f"Delivery tenant '{delivery.tenant_id}' does not match scope '{tenant_id}'.")

        doc = {
            "delivery_id": delivery.delivery_id,
            "subscription_id": delivery.subscription_id,
            "tenant_id": tenant_id,
            "event_id": delivery.event_id,
            "event_type": delivery.event_type,
            "attempt_number": delivery.attempt_number,
            "status": delivery.status,
            "response_status_code": delivery.response_status_code,
            "error_message": delivery.error_message,
            "created_at": delivery.created_at,
            "completed_at": delivery.completed_at,
            "next_retry_at": delivery.next_retry_at,
        }
        await self.insert_one(tenant_id, doc)
        return delivery

    async def get_by_id(
        self,
        tenant_id: str,
        delivery_id: str,
    ) -> Optional[WebhookDeliveryRecord]:
        doc = await self.find_one(tenant_id, {"delivery_id": delivery_id})
        return self._doc_to_record(doc) if doc else None

    async def list_by_subscription(
        self,
        tenant_id: str,
        subscription_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[WebhookDeliveryRecord]:
        capped = min(max(1, limit), MAX_PAGE_SIZE)
        docs = (await self.find_many(
            tenant_id,
            {"subscription_id": subscription_id},
            limit=capped + offset,
            sort=[("created_at", DESCENDING)],
        ))[offset:]
        return [self._doc_to_record(d) for d in docs]

    async def update_delivery_status(
        self,
        tenant_id: str,
        delivery_id: str,
        status: str,
        response_status_code: Optional[int] = None,
        error_message: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
    ) -> Optional[WebhookDeliveryRecord]:
        validate_tenant(tenant_id)
        now = datetime.now(timezone.utc)
        update_doc: Dict[str, Any] = {"status": status}
        if response_status_code is not None:
            update_doc["response_status_code"] = response_status_code
        if error_message is not None:
            update_doc["error_message"] = error_message
        if next_retry_at is not None:
            update_doc["next_retry_at"] = next_retry_at
        if status in ("delivered", "dead_letter", "failed_permanent"):
            update_doc["completed_at"] = now

        res = await self.update_one(tenant_id, {"delivery_id": delivery_id}, {"$set": update_doc})
        if res == 0:
            return None
        return await self.get_by_id(tenant_id, delivery_id)

    # Sync bridge methods
    def record_delivery_sync(self, tenant_id: str, delivery: WebhookDeliveryRecord) -> WebhookDeliveryRecord:
        return _run_coroutine_sync(self.record_delivery(tenant_id, delivery))

    def get_by_id_sync(self, tenant_id: str, delivery_id: str) -> Optional[WebhookDeliveryRecord]:
        return _run_coroutine_sync(self.get_by_id(tenant_id, delivery_id))

    def list_by_subscription_sync(
        self,
        tenant_id: str,
        subscription_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[WebhookDeliveryRecord]:
        return _run_coroutine_sync(self.list_by_subscription(tenant_id, subscription_id, limit, offset))

    def update_delivery_status_sync(
        self,
        tenant_id: str,
        delivery_id: str,
        status: str,
        response_status_code: Optional[int] = None,
        error_message: Optional[str] = None,
        next_retry_at: Optional[datetime] = None,
    ) -> Optional[WebhookDeliveryRecord]:
        return _run_coroutine_sync(
            self.update_delivery_status(tenant_id, delivery_id, status, response_status_code, error_message, next_retry_at)
        )
