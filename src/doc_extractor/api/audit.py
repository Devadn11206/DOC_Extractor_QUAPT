"""
Audit and Tamper-Evident Event Logging (SEC-008, FR-AUD-001, API-008, API-024).
Maintains an append-only, SHA-256 hash-chained log of system events.
"""

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Dict, List, Optional
import uuid
from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    event_type: str
    tenant_id: str
    actor_id: str
    resource_type: str
    resource_id: str
    details: Dict[str, Any] = Field(default_factory=dict)
    previous_hash: str = "0" * 64
    event_hash: str = ""

    def calculate_hash(self) -> str:
        payload = {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "tenant_id": self.tenant_id,
            "actor_id": self.actor_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "details": self.details,
            "previous_hash": self.previous_hash,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class TamperEvidentAuditLog:
    def __init__(self) -> None:
        self._events: List[AuditEvent] = []

    def record_event(
        self,
        event_type: str,
        tenant_id: str,
        actor_id: str,
        resource_type: str,
        resource_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditEvent:
        prev_hash = self._events[-1].event_hash if self._events else "0" * 64
        event = AuditEvent(
            event_type=event_type,
            tenant_id=tenant_id,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details or {},
            previous_hash=prev_hash,
        )
        event.event_hash = event.calculate_hash()
        self._events.append(event)
        return event

    def list_events(
        self,
        tenant_id: Optional[str] = None,
        event_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> List[AuditEvent]:
        res = self._events
        if tenant_id:
            res = [e for e in res if e.tenant_id == tenant_id]
        if event_type:
            res = [e for e in res if e.event_type == event_type]
        if resource_id:
            res = [e for e in res if e.resource_id == resource_id]
        return res

    def get_event(self, event_id: str) -> Optional[AuditEvent]:
        for e in self._events:
            if e.event_id == event_id:
                return e
        return None

    def verify_integrity(self) -> bool:
        """Verifies the hash chain integrity across all events."""
        expected_prev = "0" * 64
        for event in self._events:
            if event.previous_hash != expected_prev:
                return False
            if event.event_hash != event.calculate_hash():
                return False
            expected_prev = event.event_hash
        return True

