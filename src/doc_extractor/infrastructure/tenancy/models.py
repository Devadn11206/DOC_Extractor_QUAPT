"""
Tenant provisioning and lifecycle models (Team Member 3, Phase 5C).

Adheres to:
- PRD §10.1 SEC-001 (MFA required for admin operations).
- PRD §10.1 SEC-003 (Least-privilege role boundaries).
- PRD §10.1 SEC-006 (Strict tenant isolation).
- PRD §10.1 SEC-008 (Tamper-evident audit logging).
- Open Decision M5-D16 (Tenant-ID policy: path-safe, non-mutating).
- Open Decision M5-D17 (Explicit tenant provisioning policy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional

from doc_extractor.tenancy import require_path_safe_tenant_id


class TenantStatus(str, Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


@dataclass
class TenantRecord:
    """Authoritative persistent record of a provisioned tenant."""

    tenant_id: str
    name: str
    status: TenantStatus = TenantStatus.ACTIVE
    config: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "system"
    suspended_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None
    suspension_reason: Optional[str] = None

    def __post_init__(self) -> None:
        require_path_safe_tenant_id(self.tenant_id)
        if not self.name or not self.name.strip():
            raise ValueError("Tenant name must be non-empty.")
        if isinstance(self.status, str):
            self.status = TenantStatus(self.status)

    def is_active(self) -> bool:
        return self.status == TenantStatus.ACTIVE

    def is_suspended(self) -> bool:
        return self.status == TenantStatus.SUSPENDED

    def is_deactivated(self) -> bool:
        return self.status == TenantStatus.DEACTIVATED
