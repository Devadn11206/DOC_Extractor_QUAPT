"""
Health and readiness inspection endpoints for Document Extraction Engine (API-009).

Integrates existing module health checking functionality from doc_extractor.main.
"""

import asyncio
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from doc_extractor.api.config import ApiSettings
from doc_extractor.api.dependencies import get_settings
from doc_extractor.main import get_module_health


class HealthResponse(BaseModel):
    """PRD API-009 health inspection response schema."""
    model_config = ConfigDict(extra="allow")

    status: str = Field("healthy", description="Overall service status (healthy, degraded, unhealthy)")
    service: str = Field(..., description="Service identifier")
    version: str = Field(..., description="Package version")
    api_version: str = Field(..., description="REST API semantic version")
    environment: str = Field(..., description="Deployment environment (development, staging, production)")
    egress_isolated: bool = Field(True, description="Strict private boundary confirmation (ARC-001/SEC-011)")
    target_render_dpi: int = Field(200, description="Pipeline rendering target DPI")
    malware_scan_enabled: bool = Field(True, description="Pre-processing scan status")
    malware_scanner_type: str = Field(..., description="Configured scanner backend")
    malware_scanner_ready: bool = Field(False, description="Whether scanner is active and verified")
    malware_scanner_engine: str = Field(..., description="Class name of active scanner")


router = APIRouter(tags=["Health & Status"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Check service operational status and security readiness",
    description="Returns service health, version, VPC egress boundary state, and scanner readiness.",
)
async def check_health(
    request: Request,
    settings: ApiSettings = Depends(get_settings),
) -> Dict[str, Any]:
    """Inspects engine health status, reusing existing get_module_health."""
    scanner = getattr(request.app.state, "malware_scanner", None)
    health_data = get_module_health(settings, scanner=scanner)
    health_data["api_version"] = settings.api_version
    # Database component: reported only when MongoDB is configured, so the default response is unchanged. An unreachable
    # database degrades the status but keeps HTTP 200 (this endpoint is also the liveness probe: a database outage must
    # not restart every replica). The component holds a fixed status and reason only. The probe is CACHED for a few seconds
    # with one probe in flight: this route is unauthenticated, so its request rate must not become database load.
    container = getattr(request.app.state, "container", None)
    mongo = getattr(container, "mongo", None)
    if mongo is not None:
        database = await mongo.cached_health()
        health_data["database"] = database.to_public()
        if not database.ok:
            health_data["status"] = "degraded"

    # Queue component (reported only when queue is configured and started)
    queue = getattr(container, "_owned_queue", None)
    if queue is not None and hasattr(queue, "health"):
        try:
            q_healthy = await queue.health()
            health_data["queue"] = {"status": "ok" if q_healthy else "degraded", "backend": "rabbitmq"}
            if not q_healthy:
                health_data["status"] = "degraded"
        except Exception:
            health_data["queue"] = {"status": "unhealthy", "backend": "rabbitmq"}
            health_data["status"] = "degraded"

    # Redis component (reported only when Redis URL is explicitly configured)
    redis_url = getattr(getattr(container, "engine_settings", None), "redis_url", None)
    if redis_url:
        try:
            from doc_extractor.distributed_state import connect_redis

            def _ping_redis():
                r = connect_redis(redis_url)
                return r.ping()

            r_ok = await asyncio.to_thread(_ping_redis)
            health_data["redis"] = {"status": "ok" if r_ok else "degraded"}
            if not r_ok:
                health_data["status"] = "degraded"
        except Exception:
            health_data["redis"] = {"status": "unhealthy"}
            health_data["status"] = "degraded"

    # S3 Object Storage & KMS component (reported only when storage_backend is s3)
    if getattr(settings, "storage_backend", "local") == "s3":
        try:
            from doc_extractor.infrastructure.storage.adapter import S3Storage
            from doc_extractor.infrastructure.storage.settings import S3StorageSettings

            s3_settings = S3StorageSettings.from_api_settings(settings)
            if s3_settings is not None:
                s3 = S3Storage(settings=s3_settings, tenant_id="_health")
                try:
                    s3_h = await asyncio.to_thread(s3.check_health)
                    health_data["storage"] = {"status": s3_h.get("status", "ok"), "backend": "s3"}
                    if s3_h.get("status") != "ok":
                        health_data["status"] = "degraded"
                    if hasattr(s3, "kms_provider") and hasattr(s3.kms_provider, "check_health"):
                        try:
                            kms_h = await asyncio.to_thread(s3.kms_provider.check_health)
                            health_data["kms"] = {"status": kms_h.get("status", "ok"), "provider": kms_h.get("provider", "aws_kms")}
                            if kms_h.get("status") != "ok":
                                health_data["status"] = "degraded"
                        except Exception:
                            health_data["kms"] = {"status": "unhealthy"}
                            health_data["status"] = "degraded"
                finally:
                    s3.close()
        except Exception:
            health_data["storage"] = {"status": "unhealthy", "backend": "s3"}
            health_data["status"] = "degraded"

    return health_data
