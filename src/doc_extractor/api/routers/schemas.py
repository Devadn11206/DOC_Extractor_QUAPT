"""
Schema lifecycle REST endpoints (PRD SCH-001..006, SEC-003).

Each endpoint is a thin adapter: the lifecycle rules (draft -> testing -> approved -> retired, four-eyes approval,
immutable versions, non-destructive rollback) live in ``schema_lifecycle``. The router only authenticates, checks the
M4 permission for the action, and calls the manager. The tenant always comes from the principal, never the request.

The manager and the approval service are application-scoped and are injected on app state. When they are not
configured every route answers 503 SERVICE_UNAVAILABLE: the router never substitutes the in-memory repository.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from doc_extractor.api.dependencies import get_current_principal, require_permissions
from doc_extractor.api.exceptions import ApiValidationError, PortNotConfiguredError
from doc_extractor.api.ports.authentication import ApiPrincipal
from schema_lifecycle.enums import SchemaStatus
from schema_lifecycle.metamodel import SchemaDefinition
from schema_lifecycle.roles import SchemaPermission

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
):
    _check_body_belongs_to_caller(body.definition, principal)
    return manager.create_schema(principal.tenant_id, body.definition, author=principal.principal_id)


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
):
    _check_body_belongs_to_caller(body.definition, principal, schema_id)
    return manager.create_version(
        principal.tenant_id, schema_id, body.definition, author=principal.principal_id, comment=body.comment
    )


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
):
    # 'approved' is never a target here: promotion goes through the four-eyes approval endpoint (SCH-005).
    return manager.transition_status(
        principal.tenant_id, schema_id, version, body.target_status, actor=principal.principal_id, reason=body.reason
    )


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
):
    snapshot, record = approval.approve_schema(
        principal.to_m4_principal(),
        principal.tenant_id,
        schema_id,
        version,
        body.harness_run_id,
        reason=body.reason,
    )
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
):
    return manager.rollback(
        principal.tenant_id, schema_id, body.target_version, actor=principal.principal_id, reason=body.reason
    )
