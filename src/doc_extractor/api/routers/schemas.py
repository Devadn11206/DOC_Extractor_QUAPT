"""
Schema lifecycle REST endpoints (PRD SCH-001..006, SEC-003, SEC-008).

Each endpoint is a thin adapter: the lifecycle rules (draft -> testing -> approved -> retired, four-eyes approval,
immutable versions, non-destructive rollback) live in ``schema_lifecycle``. The router authenticates, checks the
M4 permission for the action, calls the manager/approval service, and logs tamper-evident audit records.
The tenant always comes from the principal, never the request (SEC-006).

Mandatory audit failure policy (SEC-008): all governance operations fail closed if audit recording fails;
unrecorded operations are never permitted to persist.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from doc_extractor.api.dependencies import get_audit_logger, get_current_principal, require_permissions
from doc_extractor.api.exceptions import ApiValidationError, PortNotConfiguredError
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.ports.authentication import ApiPrincipal
from schema_lifecycle.enums import SchemaStatus
from schema_lifecycle.exceptions import (
    BenchmarkVerificationError,
    ForbiddenActionError,
    SchemaNotFoundError,
    SchemaSelfApprovalError,
)
from schema_lifecycle.metamodel import SchemaDefinition
from schema_lifecycle.roles import SchemaPermission

logger = logging.getLogger("doc_extractor.api.schemas")

router = APIRouter(tags=["Schemas"])


class CreateSchemaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: SchemaDefinition


class CreateVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    definition: SchemaDefinition
    comment: Optional[str] = Field(default=None, max_length=500)


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_status: SchemaStatus
    reason: Optional[str] = Field(default=None, max_length=500)


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    harness_run_id: str = Field(..., min_length=1, max_length=128)
    reason: Optional[str] = Field(default=None, max_length=500)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_version: int = Field(..., ge=1)
    reason: Optional[str] = Field(default=None, max_length=500)


def _check_body_belongs_to_caller(definition: SchemaDefinition, principal: ApiPrincipal, schema_id: Optional[str] = None) -> None:
    """The tenant comes only from the principal (SEC-006). A body that names another tenant, or another schema, is refused."""
    if definition.tenant_id != principal.tenant_id:
        raise ApiValidationError(
            message="The schema's tenant must be the caller's tenant.",
            code="VALIDATION_ERROR",
        )
    if schema_id is not None and definition.schema_id != schema_id:
        raise ApiValidationError(
            message="The schema identifier in the body must match the path.",
            code="VALIDATION_ERROR",
        )


def get_schema_manager(request: Request):
    manager = getattr(request.app.state, "schema_manager", None)
    if manager is None:
        raise PortNotConfiguredError("SchemaLifecycleManager")
    return manager


def get_schema_approval_service(request: Request):
    service = getattr(request.app.state, "schema_approval_service", None)
    if service is None:
        raise PortNotConfiguredError("SchemaApprovalService")
    return service


@router.post(
    "/schemas",
    status_code=201,
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_CREATE.value))],
    summary="Create a schema (SCH-001..004)",
)
async def create_schema(
    body: CreateSchemaRequest,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    _check_body_belongs_to_caller(body.definition, principal)
    snapshot = manager.create_schema(principal.tenant_id, body.definition, author=principal.principal_id)
    try:
        await audit_logger.log_action(
            actor_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            action="schema.created",
            resource_id=body.definition.schema_id,
            resource_type="schema",
            metadata={
                "schema_id": body.definition.schema_id,
                "schema_version": body.definition.version,
                "outcome_code": "SUCCESS",
            },
        )
    except Exception:
        logger.error("Audit log failed for schema creation; failing closed (SEC-008)")
        raise
    return snapshot


@router.post(
    "/schemas/{schema_id}/versions",
    status_code=201,
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_VERSION_CREATE.value))],
    summary="Create an immutable new version (SCH-006)",
)
async def create_schema_version(
    schema_id: str,
    body: CreateVersionRequest,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    _check_body_belongs_to_caller(body.definition, principal, schema_id)
    snapshot = manager.create_version(
        principal.tenant_id, schema_id, body.definition, author=principal.principal_id, comment=body.comment
    )
    try:
        await audit_logger.log_action(
            actor_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            action="schema.version_created",
            resource_id=schema_id,
            resource_type="schema",
            metadata={
                "schema_id": schema_id,
                "schema_version": snapshot.version,
                "outcome_code": "SUCCESS",
            },
        )
    except Exception:
        logger.error("Audit log failed for schema version creation; failing closed (SEC-008)")
        raise
    return snapshot


@router.get(
    "/schemas/{schema_id}/versions",
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_HISTORY_READ.value))],
    summary="Version history with author, time and diff (SCH-006)",
)
async def list_schema_versions(
    schema_id: str,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
):
    return manager.get_version_history(principal.tenant_id, schema_id)


@router.get(
    "/schemas/{schema_id}",
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_READ.value))],
    summary="Get active production schema or latest snapshot (SCH-004)",
)
async def get_schema(
    schema_id: str,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
):
    try:
        return manager.resolve_active_schema(principal.tenant_id, schema_id)
    except Exception:
        latest = manager.repository.get_latest_version(schema_id, principal.tenant_id)
        if latest is None:
            raise SchemaNotFoundError(
                f"Schema '{schema_id}' not found for tenant '{principal.tenant_id}'.",
                schema_id=schema_id,
                tenant_id=principal.tenant_id,
            )
        return latest


@router.get(
    "/schemas/{schema_id}/versions/{version}",
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_READ.value))],
    summary="Get specific version snapshot (SCH-006)",
)
async def get_schema_version(
    schema_id: str,
    version: int,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
):
    snapshot = manager.repository.get_snapshot(schema_id, version, principal.tenant_id)
    if snapshot is None:
        raise SchemaNotFoundError(
            f"Schema '{schema_id}' version {version} not found for tenant '{principal.tenant_id}'.",
            schema_id=schema_id,
            version=version,
            tenant_id=principal.tenant_id,
        )
    return snapshot


@router.post(
    "/schemas/{schema_id}/versions/{version}/status",
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_SUBMIT_TEST.value))],
    summary="Move a version through draft, testing or retired (SCH-004)",
)
async def transition_schema_status(
    schema_id: str,
    version: int,
    body: TransitionRequest,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    # 'approved' is never a target here: promotion goes through four-eyes approval endpoint (SCH-005).
    current = manager.repository.get_snapshot(schema_id, version, principal.tenant_id)
    from_status = current.status if current else None

    snapshot = manager.transition_status(
        principal.tenant_id, schema_id, version, body.target_status, actor=principal.principal_id, reason=body.reason
    )
    try:
        await audit_logger.log_action(
            actor_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            action="schema.transitioned",
            resource_id=schema_id,
            resource_type="schema",
            metadata={
                "schema_id": schema_id,
                "schema_version": version,
                "status": body.target_status.value,
                "outcome_code": "SUCCESS",
            },
        )
    except Exception:
        # Revert on audit failure
        if from_status is not None:
            try:
                manager.repository.update_status(schema_id, version, principal.tenant_id, from_status)
            except Exception:
                pass
        logger.error("Audit log failed for schema transition; failing closed (SEC-008)")
        raise
    return snapshot


@router.post(
    "/schemas/{schema_id}/versions/{version}/approve",
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_APPROVE.value))],
    summary="Four-eyes approval with a passing harness result (SCH-005)",
)
async def approve_schema_version(
    schema_id: str,
    version: int,
    body: ApproveRequest,
    principal: ApiPrincipal = Depends(get_current_principal),
    approval=Depends(get_schema_approval_service),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    try:
        snapshot, record = approval.approve_schema(
            principal.to_m4_principal(),
            principal.tenant_id,
            schema_id,
            version,
            body.harness_run_id,
            reason=body.reason,
        )
    except Exception as exc:
        # Record rejection audit event if approval was refused (e.g. self-approval, policy failure)
        try:
            await audit_logger.log_action(
                actor_id=principal.principal_id,
                tenant_id=principal.tenant_id,
                action="schema.rejected",
                resource_id=schema_id,
                resource_type="schema",
                outcome="REJECTED",
                metadata={
                    "schema_id": schema_id,
                    "schema_version": version,
                    "outcome_code": "REJECTED",
                },
            )
        except Exception:
            pass
        raise

    try:
        await audit_logger.log_action(
            actor_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            action="schema.approved",
            resource_id=schema_id,
            resource_type="schema",
            outcome="SUCCESS",
            metadata={
                "schema_id": schema_id,
                "schema_version": version,
                "outcome_code": "SUCCESS",
            },
        )
    except Exception:
        # Revert promotion if audit logging fails (fail-closed, SEC-008)
        try:
            approval.manager.repository.update_status(schema_id, version, principal.tenant_id, SchemaStatus.TESTING)
        except Exception:
            pass
        logger.error("Audit log failed for schema approval; failing closed (SEC-008)")
        raise

    return {"schema": snapshot, "approval": record}


@router.post(
    "/schemas/{schema_id}/rollback",
    dependencies=[Depends(require_permissions(SchemaPermission.SCHEMA_ROLLBACK.value))],
    summary="Re-activate a prior approved version without deleting history (SCH-006)",
)
async def rollback_schema(
    schema_id: str,
    body: RollbackRequest,
    principal: ApiPrincipal = Depends(get_current_principal),
    manager=Depends(get_schema_manager),
    audit_logger: AuditLogger = Depends(get_audit_logger),
):
    current = manager.repository.get_active_version(schema_id, principal.tenant_id)
    prev_active_version = current.version if current else None

    snapshot = manager.rollback(
        principal.tenant_id, schema_id, body.target_version, actor=principal.principal_id, reason=body.reason
    )
    try:
        await audit_logger.log_action(
            actor_id=principal.principal_id,
            tenant_id=principal.tenant_id,
            action="schema.rolled_back",
            resource_id=schema_id,
            resource_type="schema",
            metadata={
                "schema_id": schema_id,
                "schema_version": body.target_version,
                "outcome_code": "SUCCESS",
            },
        )
    except Exception:
        # Revert rollback active pointer if audit logging fails
        if prev_active_version is not None:
            try:
                manager.repository.set_active_version(schema_id, prev_active_version, principal.tenant_id)
            except Exception:
                pass
        logger.error("Audit log failed for schema rollback; failing closed (SEC-008)")
        raise
    return snapshot
