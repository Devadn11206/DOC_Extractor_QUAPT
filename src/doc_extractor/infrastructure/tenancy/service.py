"""
Tenant provisioning and lifecycle service (Team Member 3, Phase 5C).

Coordinates:
- Authorized tenant creation with mandatory MFA (PRD §10.1 SEC-001, SEC-003).
- State transitions (ACTIVE, SUSPENDED, DEACTIVATED).
- Idempotent provisioning behavior.
- Prevention of cross-tenant privilege escalation (PRD §10.1 SEC-006).
- Fail-closed audit logging to the per-tenant tamper-evident hash chain (PRD §10.1 SEC-008).
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional

from doc_extractor.api.exceptions import AuditUnavailableError
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.exceptions import (
    InvalidTenantIdentifierError,
    SecurityCheckFailedError,
)
from doc_extractor.infrastructure.identity.errors import MfaRequiredError
from doc_extractor.infrastructure.tenancy.models import TenantRecord, TenantStatus
from doc_extractor.tenancy import require_path_safe_tenant_id

logger = logging.getLogger("doc_extractor.infrastructure.tenancy.service")


class CrossTenantAccessError(SecurityCheckFailedError):
    """Raised when cross-tenant access is attempted without authorization."""

    def __init__(self, message: str = "Cross-tenant access is forbidden.") -> None:
        super().__init__(message=message, code="CROSS_TENANT_ACCESS_FORBIDDEN")


class PermissionDeniedError(SecurityCheckFailedError):
    """Raised when an operation lacks the required role or permission."""

    def __init__(self, message: str = "Permission denied.") -> None:
        super().__init__(message=message, code="PERMISSION_DENIED")


class TenantLifecycleError(Exception):
    """Raised when an invalid tenant lifecycle transition is requested."""
    pass


class TenantConflictError(Exception):
    """Raised when provisioning collides with an existing non-matching tenant."""
    pass


class TenantProvisioningService:
    """
    Central governance service for tenant lifecycle operations.
    """

    def __init__(
        self,
        tenant_repo: Any,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self._repo = tenant_repo
        self._audit_logger = audit_logger

    def _require_admin_with_mfa(self, principal: ApiPrincipal, operation: str) -> None:
        """Enforces PRD SEC-001 (MFA for Admin) and SEC-003 (Role boundaries)."""
        if "admin" not in principal.roles:
            raise PermissionDeniedError(f"Operation '{operation}' requires the 'admin' role.")
        if not principal.mfa_verified:
            raise MfaRequiredError(f"Operation '{operation}' requires verified Multi-Factor Authentication (SEC-001).")

    async def _audit(
        self,
        tenant_id: str,
        action: str,
        actor_id: str,
        metadata: Dict[str, Any],
    ) -> None:
        """Record audit event; fails closed on failure (SEC-008)."""
        if self._audit_logger is None:
            return
        try:
            await self._audit_logger.record(
                tenant_id=tenant_id,
                action=action,
                actor_id=actor_id,
                resource_type="tenant",
                resource_id=tenant_id,
                metadata=metadata,
            )
        except Exception as exc:
            logger.error("Audit logging failed for action '%s' on tenant '%s': %s", action, tenant_id, exc)
            raise AuditUnavailableError("Audit log failed to record tenant lifecycle event.") from exc

    async def async_provision_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
        name: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> TenantRecord:
        """Explicitly provision a new tenant. Idempotent on identical match."""
        self._require_admin_with_mfa(admin_principal, "provision_tenant")
        require_path_safe_tenant_id(tenant_id)

        # Check existing tenant
        existing = await self._repo.async_get_tenant(tenant_id)
        if existing is not None:
            if existing.name == name:
                logger.info("Idempotent tenant provisioning for '%s'", tenant_id)
                return existing
            raise TenantConflictError(f"Tenant '{tenant_id}' already exists with a different configuration.")

        now = datetime.now(timezone.utc)
        record = TenantRecord(
            tenant_id=tenant_id,
            name=name,
            status=TenantStatus.ACTIVE,
            config=config or {},
            created_at=now,
            updated_at=now,
            created_by=admin_principal.principal_id,
        )

        created = await self._repo.async_create_tenant(record)

        # Fail-closed audit integration
        try:
            await self._audit(
                tenant_id=tenant_id,
                action="tenant.provisioned",
                actor_id=admin_principal.principal_id,
                metadata={"name": name, "created_by": admin_principal.principal_id},
            )
        except Exception:
            # Revert creation on audit failure
            await self._repo.async_delete_tenant(tenant_id)
            raise

        logger.info("Provisioned new tenant '%s' (name='%s') by '%s'", tenant_id, name, admin_principal.principal_id)
        return created

    def provision_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
        name: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> TenantRecord:
        from doc_extractor.infrastructure.mongo.schemas import _run_coroutine_sync
        return _run_coroutine_sync(self.async_provision_tenant(admin_principal, tenant_id, name, config))

    async def async_suspend_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
        reason: Optional[str] = None,
    ) -> TenantRecord:
        """Suspend an active tenant."""
        self._require_admin_with_mfa(admin_principal, "suspend_tenant")
        require_path_safe_tenant_id(tenant_id)

        tenant = await self._repo.async_get_tenant(tenant_id)
        if tenant is None:
            raise TenantLifecycleError(f"Tenant '{tenant_id}' does not exist.")
        if tenant.status == TenantStatus.DEACTIVATED:
            raise TenantLifecycleError(f"Cannot suspend a deactivated tenant ('{tenant_id}').")
        if tenant.status == TenantStatus.SUSPENDED:
            return tenant  # Idempotent

        now = datetime.now(timezone.utc)
        update_data = {
            "status": TenantStatus.SUSPENDED,
            "suspended_at": now,
            "suspension_reason": reason or "Administrative suspension",
        }
        updated = await self._repo.async_update_tenant(tenant_id, update_data)
        if updated is None:
            raise TenantLifecycleError(f"Failed to update tenant '{tenant_id}'.")

        try:
            await self._audit(
                tenant_id=tenant_id,
                action="tenant.suspended",
                actor_id=admin_principal.principal_id,
                metadata={"reason": reason or "Administrative suspension"},
            )
        except Exception:
            # Revert update on audit failure
            await self._repo.async_update_tenant(
                tenant_id,
                {"status": tenant.status, "suspended_at": tenant.suspended_at, "suspension_reason": tenant.suspension_reason},
            )
            raise

        logger.info("Tenant '%s' suspended by '%s' (reason=%s)", tenant_id, admin_principal.principal_id, reason)
        return updated

    def suspend_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
        reason: Optional[str] = None,
    ) -> TenantRecord:
        from doc_extractor.infrastructure.mongo.schemas import _run_coroutine_sync
        return _run_coroutine_sync(self.async_suspend_tenant(admin_principal, tenant_id, reason))

    async def async_activate_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
    ) -> TenantRecord:
        """Re-activate a suspended tenant."""
        self._require_admin_with_mfa(admin_principal, "activate_tenant")
        require_path_safe_tenant_id(tenant_id)

        tenant = await self._repo.async_get_tenant(tenant_id)
        if tenant is None:
            raise TenantLifecycleError(f"Tenant '{tenant_id}' does not exist.")
        if tenant.status == TenantStatus.DEACTIVATED:
            raise TenantLifecycleError(f"Cannot activate a deactivated tenant ('{tenant_id}').")
        if tenant.status == TenantStatus.ACTIVE:
            return tenant  # Idempotent

        update_data = {
            "status": TenantStatus.ACTIVE,
            "suspended_at": None,
            "suspension_reason": None,
        }
        updated = await self._repo.async_update_tenant(tenant_id, update_data)
        if updated is None:
            raise TenantLifecycleError(f"Failed to update tenant '{tenant_id}'.")

        try:
            await self._audit(
                tenant_id=tenant_id,
                action="tenant.activated",
                actor_id=admin_principal.principal_id,
                metadata={"previous_status": tenant.status.value},
            )
        except Exception:
            # Revert update on audit failure
            await self._repo.async_update_tenant(
                tenant_id,
                {"status": tenant.status, "suspended_at": tenant.suspended_at, "suspension_reason": tenant.suspension_reason},
            )
            raise

        logger.info("Tenant '%s' activated by '%s'", tenant_id, admin_principal.principal_id)
        return updated

    def activate_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
    ) -> TenantRecord:
        from doc_extractor.infrastructure.mongo.schemas import _run_coroutine_sync
        return _run_coroutine_sync(self.async_activate_tenant(admin_principal, tenant_id))

    async def async_deactivate_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
        reason: Optional[str] = None,
    ) -> TenantRecord:
        """Permanently deactivate a tenant."""
        self._require_admin_with_mfa(admin_principal, "deactivate_tenant")
        require_path_safe_tenant_id(tenant_id)

        tenant = await self._repo.async_get_tenant(tenant_id)
        if tenant is None:
            raise TenantLifecycleError(f"Tenant '{tenant_id}' does not exist.")
        if tenant.status == TenantStatus.DEACTIVATED:
            return tenant  # Idempotent

        now = datetime.now(timezone.utc)
        update_data = {
            "status": TenantStatus.DEACTIVATED,
            "deactivated_at": now,
        }
        updated = await self._repo.async_update_tenant(tenant_id, update_data)
        if updated is None:
            raise TenantLifecycleError(f"Failed to update tenant '{tenant_id}'.")

        try:
            await self._audit(
                tenant_id=tenant_id,
                action="tenant.deactivated",
                actor_id=admin_principal.principal_id,
                metadata={"reason": reason or "Administrative deactivation"},
            )
        except Exception:
            await self._repo.async_update_tenant(
                tenant_id,
                {"status": tenant.status, "deactivated_at": tenant.deactivated_at},
            )
            raise

        logger.warning("Tenant '%s' DEACTIVATED by '%s' (reason=%s)", tenant_id, admin_principal.principal_id, reason)
        return updated

    def deactivate_tenant(
        self,
        admin_principal: ApiPrincipal,
        tenant_id: str,
        reason: Optional[str] = None,
    ) -> TenantRecord:
        from doc_extractor.infrastructure.mongo.schemas import _run_coroutine_sync
        return _run_coroutine_sync(self.async_deactivate_tenant(admin_principal, tenant_id, reason))

    async def async_get_tenant(
        self,
        principal: ApiPrincipal,
        tenant_id: str,
    ) -> Optional[TenantRecord]:
        """Retrieve tenant record with cross-tenant authorization checks."""
        require_path_safe_tenant_id(tenant_id)
        if "admin" not in principal.roles and principal.tenant_id != tenant_id:
            raise CrossTenantAccessError("Cross-tenant tenant inspection is forbidden.")
        return await self._repo.async_get_tenant(tenant_id)

    def get_tenant(
        self,
        principal: ApiPrincipal,
        tenant_id: str,
    ) -> Optional[TenantRecord]:
        from doc_extractor.infrastructure.mongo.schemas import _run_coroutine_sync
        return _run_coroutine_sync(self.async_get_tenant(principal, tenant_id))
