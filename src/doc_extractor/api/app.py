"""
FastAPI application factory and lifecycle management (Module M5).

Adheres to:
- PRD §7.2: Canonical REST API architecture.
- Centralized configuration and decoupled application initialization.
- Startup/shutdown lifecycle via modern async lifespan context manager.
- Request ID middleware and API-010 error handling registration.
- Health endpoint integration reusing get_module_health.
- Schema-compliant OpenAPI generation.
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings, load_api_settings
from doc_extractor.api.exceptions import register_exception_handlers
from doc_extractor.api import middleware as api_middleware
from doc_extractor.api.middleware import RequestIdMiddleware, UploadSizeLimitMiddleware
from doc_extractor.api.routers.documents import router as documents_router
from doc_extractor.api.routers.health import router as health_router
from doc_extractor.api.routers.review_tasks import router as review_tasks_router
from doc_extractor.api.routers.schemas import router as schemas_router
from doc_extractor.logging import setup_logging

logger = logging.getLogger("doc_extractor.api")


STARTUP_CONFIGURED_PORTS = (
    "review_task_repo",
    "api_key_repo",
    "audit_repo",
    "audit_logger",
    "queue_producer",
    "queue_consumer",
    "webhook_subscription_repo",
    "webhook_delivery_repo",
)
_PUBLISHED_ATTRIBUTE = "_startup_published_ports"


def publish_started_ports(state: Any, container: Any) -> None:
    """
    Publish the ports that ``container.startup()`` configured (the MongoDB repositories and the durable audit logger) to the application state, which is
    where the request dependencies read them: ``create_app`` copied the container's ports onto the state BEFORE start-up ran.

    Called only after a SUCCESSFUL start-up, so a failed start-up publishes nothing. A port is replaced only when its slot is
    empty or still holds what an earlier start-up published here (an app-owned container is rebuilt on a second lifespan, and
    the previous adapter is bound to a connection that was closed); a port that was set on the state explicitly is never replaced.
    """
    published = getattr(state, _PUBLISHED_ATTRIBUTE, None)
    if not isinstance(published, dict):
        published = {}
    for slot in STARTUP_CONFIGURED_PORTS:
        adapter = getattr(container, slot, None)
        current = getattr(state, slot, None)
        if adapter is not None and (current is None or current is published.get(slot)):
            setattr(state, slot, adapter)
            published[slot] = adapter
    if getattr(container, "_idempotency_replaced_at_startup", False):
        state.idempotency_store = container.idempotency_store        # informational slot: it must not keep naming the replaced store
    setattr(state, _PUBLISHED_ATTRIBUTE, published)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifecycle context manager.
    Coordinates startup validation, composition initialization, and clean resource shutdown.
    """
    settings: ApiSettings = app.state.settings
    try:
        # Configure structured logging once at application startup (preserves externally registered handlers)
        setup_logging(
            level=settings.log_level,
            service_name=settings.service_name,
            environment=settings.app_env,
            clear_existing=False,
        )

        logger.info(
            "Initializing Document Extraction Engine API (env=%s, service=%s, egress_isolated=%s)",
            settings.app_env,
            settings.service_name,
            not settings.allow_external_egress,
        )

        # Ensure ApplicationContainer is active on app.state
        # If application-owned and shut down from a previous lifespan, safely reinitialize
        container: Optional[ApplicationContainer] = getattr(app.state, "container", None)
        owns_container: bool = getattr(app.state, "owns_container", True)
        if container is None or (owns_container and getattr(container, "_is_shutdown", False)):
            container = ApplicationContainer(api_settings=settings)
            app.state.container = container
            app.state.owns_container = True
            app.state.malware_scanner = container.malware_scanner
            app.state.routed_extractor = container.routed_extractor
            app.state.module_c = container.module_c
            app.state.tenant_cache = container.tenant_cache
            app.state.idempotency_store = container.idempotency_store

        # Verification: Private VPC / Zero-egress boundary (ARC-001 / SEC-011)
        if settings.allow_external_egress:
            raise RuntimeError("FATAL: External egress is enabled. System must run in isolated VPC/on-prem.")

        # MongoDB (when configured): connect, verify the replica set and apply the declared indexes. A failure stops the
        # application here with a typed error whose message holds no connection detail (no fallback to another store).
        await container.startup()
        publish_started_ports(app.state, container)

        yield
    finally:
        logger.info("Shutting down Document Extraction Engine API. Releasing resources.")
        # Only shut down application-owned containers; caller-owned containers are preserved
        if getattr(app.state, "owns_container", False):
            active_container = getattr(app.state, "container", None)
            if active_container is not None and not getattr(active_container, "_is_shutdown", False):
                try:
                    await active_container.ashutdown()
                except Exception as exc:
                    logger.error("Error while closing the database connection (%s)", type(exc).__name__)
                try:
                    active_container.shutdown()
                except Exception as exc:
                    logger.error("Error during application container shutdown: %s", exc)


def create_app(
    settings: Optional[ApiSettings] = None,
    container: Optional[ApplicationContainer] = None,
) -> FastAPI:
    """
    Application factory building the FastAPI HTTP application.

    Args:
        settings: Optional pre-loaded ApiSettings. If omitted, loaded from the environment, where DOC_API_ENV is REQUIRED
        (there is no default environment; see ``load_api_settings``).
        container: Optional pre-configured ApplicationContainer composition root.
                   When provided, the caller owns its lifecycle (application will not shut it down).

    Returns:
        Configured FastAPI instance with middleware, routes, and exception handlers.
    """
    resolved_settings = settings or (container.api_settings if container is not None else load_api_settings())
    owns_container = container is None
    resolved_container = container or ApplicationContainer(api_settings=resolved_settings)

    app = FastAPI(
        title=resolved_settings.api_title,
        version=resolved_settings.api_version,
        description=resolved_settings.api_description,
        docs_url=resolved_settings.docs_url,
        redoc_url=resolved_settings.redoc_url,
        openapi_url=resolved_settings.openapi_url,
        lifespan=lifespan,
    )

    # Attach centralized configuration to application state
    app.state.settings = resolved_settings

    # Attach composition container and application-scoped singletons to state
    app.state.container = resolved_container
    app.state.owns_container = owns_container
    app.state.malware_scanner = resolved_container.malware_scanner
    app.state.routed_extractor = resolved_container.routed_extractor
    app.state.module_c = resolved_container.module_c
    app.state.tenant_cache = resolved_container.tenant_cache
    app.state.idempotency_store = resolved_container.idempotency_store

    # Initialize hexagonal adapter slots on state (using container's if available)
    app.state.auth_provider = resolved_container.auth_provider
    app.state.document_metadata_repo = resolved_container.document_metadata_repo
    app.state.review_task_repo = resolved_container.review_task_repo
    app.state.api_key_repo = resolved_container.api_key_repo
    app.state.webhook_subscription_repo = resolved_container.webhook_subscription_repo
    app.state.webhook_delivery_repo = resolved_container.webhook_delivery_repo
    app.state.audit_repo = resolved_container.audit_repo
    app.state.queue_producer = resolved_container.queue_producer
    app.state.queue_consumer = resolved_container.queue_consumer
    app.state.audit_logger = resolved_container.audit_logger

    # API-001 upload body limit: a pure ASGI guard in front of the multipart parser (refuses an over-limit body before it is
    # parsed or spooled). It is registered BEFORE (so inner to) the CORS and request-id middleware: its 413 then carries the request id and CORS headers.
    app.add_middleware(
        UploadSizeLimitMiddleware,
        path=f"{resolved_settings.api_prefix}/documents" if resolved_settings.api_prefix else "/documents",
        max_body_bytes=(
            resolved_container.engine_settings.max_file_size_bytes + api_middleware.UPLOAD_MULTIPART_OVERHEAD_BYTES
        ),
        max_concurrent_uploads=resolved_settings.max_concurrent_uploads,
        body_idle_timeout=resolved_settings.upload_body_idle_timeout_seconds,
        body_total_timeout=resolved_settings.upload_body_total_timeout_seconds,
    )

    # Register CORS if origins configured
    if resolved_settings.cors_origins:
        if "*" in resolved_settings.cors_origins and resolved_settings.cors_allow_credentials:
            raise ValueError(
                "Wildcard origin '*' cannot be combined with allow_credentials=True. "
                "Specify explicit origins to prevent cross-origin credential reflection."
            )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved_settings.cors_origins,
            allow_credentials=resolved_settings.cors_allow_credentials,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
            allow_headers=["*"],
            expose_headers=[resolved_settings.request_id_header],
        )

    # Register Request ID middleware
    app.add_middleware(
        RequestIdMiddleware,
        header_name=resolved_settings.request_id_header,
        allow_client_request_id=resolved_settings.allow_client_request_id,
    )

    # Register canonical API-010 exception handlers
    register_exception_handlers(app)

    # NFR-010: request counts and durations by route template (outermost: every response, including errors, is timed).
    from doc_extractor.api.metrics_middleware import RequestMetricsMiddleware

    app.add_middleware(RequestMetricsMiddleware)

    # Register routers: root /health and versioned /v1/health
    app.include_router(health_router, prefix="", tags=["Health & Status"])
    if resolved_settings.api_prefix:
        app.include_router(health_router, prefix=resolved_settings.api_prefix, tags=["Health & Status"])

    # Register document read router under api_prefix (API-003)
    if resolved_settings.api_prefix:
        app.include_router(documents_router, prefix=resolved_settings.api_prefix, tags=["Documents"])
    else:
        app.include_router(documents_router, prefix="", tags=["Documents"])

    # Register the review-task router (API-005 list, API-016 claim and release; PROVISIONAL, M5-D11 is Open). Without an injected
    # ReviewTaskRepository (no production adapter exists yet) every route answers 503 after authentication and the scope check.
    app.include_router(review_tasks_router, prefix=resolved_settings.api_prefix or "", tags=["Review Tasks"])
    # Schema lifecycle (SCH-001..006): answers 503 until a schema manager is configured on the container.
    app.include_router(schemas_router, prefix=resolved_settings.api_prefix or "", tags=["Schemas"])
    app.state.schema_manager = resolved_container.schema_manager
    app.state.schema_approval_service = resolved_container.schema_approval_service
    app.state.rate_limiter = resolved_container.rate_limiter

    # The API-key management router (API-019) is intentionally not mounted: it is held back until
    # decision M5-D08 is approved. Webhook routers are a later stage.

    # Custom OpenAPI generator ensuring metadata and clean schemas
    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        openapi_schema = get_openapi(
            title=resolved_settings.api_title,
            version=resolved_settings.api_version,
            description=resolved_settings.api_description,
            routes=app.routes,
        )
        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi

    return app
