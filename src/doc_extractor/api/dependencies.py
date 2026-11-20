"""
FastAPI dependency injection providers and authorization guards (Module M5).

Adheres to:
- PRD §10.1 SEC-001, SEC-003, SEC-005: Strict tenant boundary isolation.
- Tenant context originates EXCLUSIVELY from the authenticated principal.
- Client headers, query parameters, or body fields asserting tenant identity are rejected.
- Provider-independent injection of repositories, queue brokers, and audit loggers.
"""

import threading
from typing import Any, Callable, Optional, Sequence
from fastapi import Depends, Header, Request

from doc_extractor.api.composition import ApplicationContainer, TenantComponents
from doc_extractor.api.config import ApiSettings, get_api_settings
from doc_extractor.api.exceptions import (
    AuthenticationRequiredError,
    ContainerUnavailableError,
    ForbiddenError,
    PortNotConfiguredError,
)
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.ports.authentication import (
    ApiPrincipal,
    AuthenticationProvider,
    TenantContext,
)
from doc_extractor.api.ports.queue import QueueConsumer, QueueProducer
from doc_extractor.api.ports.repositories import (
    ApiKeyRepository,
    AuditRepository,
    DocumentMetadataRepository,
    ReviewTaskRepository,
    WebhookDeliveryRepository,
    WebhookSubscriptionRepository,
)
from doc_extractor.api.services.document_read_service import DocumentReadService
from doc_extractor.api.services.document_extraction_service import ExtractionGate
from doc_extractor.api.services.document_service import DocumentService
from doc_extractor.api.services.document_upload_service import DocumentUploadService
from doc_extractor.processing_service import DocumentProcessingService


def get_settings(request: Request) -> ApiSettings:
    """Retrieve application configuration from app state or singleton."""
    if hasattr(request.app.state, "settings") and request.app.state.settings is not None:
        return request.app.state.settings
    return get_api_settings()


def get_request_id(request: Request) -> str:
    """Retrieve correlated request ID established by RequestIdMiddleware."""
    req_id = getattr(request.state, "request_id", None)
    if not req_id:
        from doc_extractor.api.middleware import get_request_id_from_context
        req_id = get_request_id_from_context()
    return req_id or "unknown"


def get_auth_provider(request: Request) -> AuthenticationProvider:
    """Retrieve the registered AuthenticationProvider from application state."""
    provider = getattr(request.app.state, "auth_provider", None)
    if provider is not None:
        return provider

    # If api_key_repo is configured, auto-construct ApiKeyAuthenticationProvider (TASK-5.7-B). The audit logger must be
    # configured explicitly: nothing is substituted for a missing logger.
    api_key_repo = getattr(request.app.state, "api_key_repo", None)
    if api_key_repo is not None:
        audit_logger = getattr(request.app.state, "audit_logger", None)
        if audit_logger is None:
            raise PortNotConfiguredError("AuditLogger")
        from doc_extractor.api.providers.api_key_auth import ApiKeyAuthenticationProvider

        provider = ApiKeyAuthenticationProvider(repository=api_key_repo, audit_logger=audit_logger)
        request.app.state.auth_provider = provider
        return provider

    # Fail closed: no provider configured and no repository to construct one. Typed 503, not a bare 500.
    raise PortNotConfiguredError("AuthenticationProvider")


async def get_current_principal(
    request: Request,
    auth_header: Optional[str] = Header(None, alias="Authorization"),
    auth_provider: AuthenticationProvider = Depends(get_auth_provider),
) -> ApiPrincipal:
    """
    Authenticate the incoming request principal.

    Fail-closed: Returns an authoritative ApiPrincipal or raises AuthenticationRequiredError.
    Never falls back to an anonymous principal or default tenant.
    """
    if not auth_header:
        raise AuthenticationRequiredError(
            message="Authorization header is required.",
            code="AUTHENTICATION_REQUIRED",
        )

    principal = await auth_provider.authenticate(auth_header, request=request)
    if principal is None or not principal.is_active:
        raise AuthenticationRequiredError(
            message="Invalid or revoked authentication credentials.",
            code="INVALID_CREDENTIALS",
        )
    return principal


def get_tenant_context(
    principal: ApiPrincipal = Depends(get_current_principal),
) -> TenantContext:
    """
    Derives TenantContext strictly and exclusively from the authenticated principal.

    SEC-005 Guardrail: Under no circumstances is tenant identity parsed from
    query params, headers (e.g. X-Tenant-ID), or request bodies.
    """
    return principal.tenant_context


def require_scopes(*required_scopes: str) -> Callable:
    """Factory creating dependency to enforce required scopes on an endpoint."""

    async def _scope_guard(principal: ApiPrincipal = Depends(get_current_principal)) -> ApiPrincipal:
        for scope in required_scopes:
            if not principal.has_scope(scope):
                raise ForbiddenError(
                    message=f"Access denied. Missing required scope: {scope}",
                    code="FORBIDDEN_SCOPE",
                    details={"required_scope": scope},
                )
        return principal

    return _scope_guard


def require_permissions(*required_permissions: str) -> Callable:
    """Factory creating dependency to enforce required permissions on an endpoint."""

    async def _permission_guard(principal: ApiPrincipal = Depends(get_current_principal)) -> ApiPrincipal:
        for perm in required_permissions:
            if not principal.has_permission(perm):
                raise ForbiddenError(
                    message=f"Access denied. Missing required permission: {perm}",
                    code="FORBIDDEN_PERMISSION",
                    details={"required_permission": perm},
                )
        return principal

    return _permission_guard


# ============================================================================
# Hexagonal Port Dependencies (an unconfigured port answers 503 SERVICE_UNAVAILABLE, never a bare 500)
# ============================================================================

def get_document_metadata_repository(request: Request) -> DocumentMetadataRepository:
    """Dependency injection provider for DocumentMetadataRepository."""
    repo = getattr(request.app.state, "document_metadata_repo", None)
    if repo is None:
        raise PortNotConfiguredError("DocumentMetadataRepository")
    return repo


def get_review_task_repository(request: Request) -> ReviewTaskRepository:
    """Dependency injection provider for ReviewTaskRepository."""
    repo = getattr(request.app.state, "review_task_repo", None)
    if repo is None:
        raise PortNotConfiguredError("ReviewTaskRepository")
    return repo


def get_api_key_repository(request: Request) -> ApiKeyRepository:
    """Dependency injection provider for ApiKeyRepository."""
    repo = getattr(request.app.state, "api_key_repo", None)
    if repo is None:
        raise PortNotConfiguredError("ApiKeyRepository")
    return repo


def get_webhook_subscription_repository(request: Request) -> WebhookSubscriptionRepository:
    """Dependency injection provider for WebhookSubscriptionRepository."""
    repo = getattr(request.app.state, "webhook_subscription_repo", None)
    if repo is None:
        raise PortNotConfiguredError("WebhookSubscriptionRepository")
    return repo


def get_webhook_delivery_repository(request: Request) -> WebhookDeliveryRepository:
    """Dependency injection provider for WebhookDeliveryRepository."""
    repo = getattr(request.app.state, "webhook_delivery_repo", None)
    if repo is None:
        raise PortNotConfiguredError("WebhookDeliveryRepository")
    return repo


def get_audit_repository(request: Request) -> AuditRepository:
    """Dependency injection provider for AuditRepository."""
    repo = getattr(request.app.state, "audit_repo", None)
    if repo is None:
        raise PortNotConfiguredError("AuditRepository")
    return repo


def get_queue_producer(request: Request) -> QueueProducer:
    """Dependency injection provider for durable QueueProducer."""
    producer = getattr(request.app.state, "queue_producer", None)
    if producer is None:
        raise PortNotConfiguredError("QueueProducer")
    return producer


def get_queue_consumer(request: Request) -> QueueConsumer:
    """Dependency injection provider for durable QueueConsumer."""
    consumer = getattr(request.app.state, "queue_consumer", None)
    if consumer is None:
        raise PortNotConfiguredError("QueueConsumer")
    return consumer


def get_audit_logger(request: Request) -> AuditLogger:
    """
    Dependency injection provider for the structured AuditLogger. Audit is mandatory for the operations that need it, so
    an unconfigured logger is a typed 503: nothing is substituted. Configuring SafeStructuredAuditLogger explicitly is a
    deliberate choice of a BEST-EFFORT sink whose write failures do not propagate, so those operations are then not
    fail-closed; a durable AuditRepository-backed logger that raises on failure is Team Member 3's.
    """
    logger = getattr(request.app.state, "audit_logger", None)
    if logger is None:
        raise PortNotConfiguredError("AuditLogger")
    return logger


def get_api_key_service(
    request: Request,
    api_key_repo: ApiKeyRepository = Depends(get_api_key_repository),
    audit_logger: AuditLogger = Depends(get_audit_logger),
) -> Any:
    """Dependency injection provider for ApiKeyService."""
    service = getattr(request.app.state, "api_key_service", None)
    if service is not None:
        return service
    from doc_extractor.api.services.api_key_service import ApiKeyService

    return ApiKeyService(repository=api_key_repo, audit_logger=audit_logger)


# ============================================================================
# Application Composition and Tenant Service Dependencies
# ============================================================================

def get_application_container(request: Request) -> ApplicationContainer:
    """Dependency injection provider for ApplicationContainer."""
    container = getattr(request.app.state, "container", None)
    if container is None or getattr(container, "_is_shutdown", False):
        raise ContainerUnavailableError("ApplicationContainer is unavailable or has been shut down.")
    return container


def get_tenant_components(
    tenant_context: TenantContext = Depends(get_tenant_context),
    container: ApplicationContainer = Depends(get_application_container),
) -> TenantComponents:
    """
    Dependency injection provider for tenant-bound components.
    Strictly isolated by the authenticated principal's tenant identity.
    """
    return container.get_tenant_components(tenant_context.tenant_id)


def get_document_processing_service(
    components: TenantComponents = Depends(get_tenant_components),
) -> DocumentProcessingService:
    """Dependency injection provider for tenant-bound DocumentProcessingService."""
    return components.processing_service


def get_document_service(
    request: Request,
    tenant_context: TenantContext = Depends(get_tenant_context),
    container: ApplicationContainer = Depends(get_application_container),
) -> DocumentService:
    """
    Dependency injection provider for tenant-bound DocumentService.
    Coordinates between API/transports and domain processing service.
    """
    doc_repo = getattr(request.app.state, "document_metadata_repo", None) or container.document_metadata_repo
    queue_prod = getattr(request.app.state, "queue_producer", None) or container.queue_producer
    audit_log = getattr(request.app.state, "audit_logger", None) or container.audit_logger
    return container.get_document_service(
        tenant_id=tenant_context.tenant_id,
        document_metadata_repo=doc_repo,
        queue_producer=queue_prod,
        audit_logger=audit_log,
    )


def get_document_upload_service(
    tenant_context: TenantContext = Depends(get_tenant_context),
    container: ApplicationContainer = Depends(get_application_container),
) -> DocumentUploadService:
    """
    Dependency injection provider for the tenant-bound API-001 upload service.
    Wraps the principal's own tenant Module A component (``ModuleAPipeline``) from the application container: the tenant
    comes only from the authenticated principal (SEC-005), and no second ingestion pipeline is created.
    """
    components = container.get_tenant_components(tenant_context.tenant_id)
    return DocumentUploadService(
        module_a=components.module_a, tenant_id=tenant_context.tenant_id, storage=components.storage
    )


def get_document_read_service(
    tenant_context: TenantContext = Depends(get_tenant_context),
    container: ApplicationContainer = Depends(get_application_container),
) -> DocumentReadService:
    """
    Dependency injection provider for tenant-bound DocumentReadService.
    Strictly isolated by the authenticated principal's tenant identity.
    Lightweight: does not instantiate heavy extraction or OCR pipelines.
    Tenant context originates exclusively from authenticated principal (SEC-005).
    """
    return container.get_document_read_service(tenant_context.tenant_id)


_EXTRACTION_GATE_LOCK = threading.Lock()


def get_extraction_gate(request: Request) -> ExtractionGate:
    """
    The process-local admission gate and deadline of API-004, created once per application from its settings.
    One gate per application instance: its capacity is shared by every tenant (provisional, process-local).
    """
    gate = getattr(request.app.state, "extraction_gate", None)
    if gate is None:
        with _EXTRACTION_GATE_LOCK:
            gate = getattr(request.app.state, "extraction_gate", None)
            if gate is None:
                settings = request.app.state.settings
                gate = ExtractionGate(
                    max_concurrent=settings.max_concurrent_extractions,
                    timeout_seconds=settings.extract_sync_timeout_seconds,
                )
                request.app.state.extraction_gate = gate
    return gate
