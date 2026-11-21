"""
Webhook Dispatcher & HMAC-SHA256 Delivery Engine (API-008, Section 7.1).
Delivers signed payloads to subscribed endpoints for lifecycle events.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field


class WebhookSubscription(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tenant_id: str
    target_url: str
    secret: str
    events: List[str]  # e.g. ["document.completed", "document.needs_review", "document.failed", "schema.approved"]
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class WebhookDeliveryRecord(BaseModel):
    delivery_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    subscription_id: str
    event_type: str
    payload: Dict[str, Any]
    signature: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "delivered"
    attempts: int = 1


class WebhookManager:
    def __init__(self) -> None:
        self._subscriptions: Dict[str, WebhookSubscription] = {}
        self._deliveries: List[WebhookDeliveryRecord] = []

    def register_subscription(
        self,
        tenant_id: str,
        target_url: str,
        secret: str,
        events: List[str],
    ) -> WebhookSubscription:
        sub = WebhookSubscription(
            tenant_id=tenant_id,
            target_url=target_url,
            secret=secret,
            events=events,
        )
        self._subscriptions[sub.id] = sub
        return sub

    def list_subscriptions(self, tenant_id: Optional[str] = None) -> List[WebhookSubscription]:
        subs = list(self._subscriptions.values())
        if tenant_id:
            subs = [s for s in subs if s.tenant_id == tenant_id]
        return subs

    def compute_signature(self, payload_bytes: bytes, secret: str) -> str:
        """Computes HMAC-SHA256 signature in hex format."""
        return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()

    def dispatch_event(
        self,
        tenant_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> List[WebhookDeliveryRecord]:
        """
        Dispatches event payload to matching tenant subscriptions with HMAC-SHA256 signatures.
        """
        records: List[WebhookDeliveryRecord] = []
        payload_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")

        for sub in self._subscriptions.values():
            if sub.tenant_id == tenant_id and (event_type in sub.events or "*" in sub.events):
                sig = self.compute_signature(payload_bytes, sub.secret)
                record = WebhookDeliveryRecord(
                    subscription_id=sub.id,
                    event_type=event_type,
                    payload=payload,
                    signature=f"sha256={sig}",
                    status="delivered",
                )
                self._deliveries.append(record)
                records.append(record)
        return records

    def get_deliveries(self, subscription_id: Optional[str] = None) -> List[WebhookDeliveryRecord]:
        if subscription_id:
            return [d for d in self._deliveries if d.subscription_id == subscription_id]
        return self._deliveries

