"""
FastAPI Composition Root and Application Lifecycle (Module M5 / Phase 2 Milestone 2B).

Centralizes:
- Application-scoped resource lifecycle (RoutedExtractor, ModuleCPipeline, MalwareScanner).
- Thread-safe model initialization concurrency (NFR-006).
- Tenant-isolated component construction and bounded LRU caching.
- Zero-egress boundary enforcement and deterministic shutdown.
"""

from collections import OrderedDict
from dataclasses import dataclass
import logging
import threading
from typing import Any, Dict, Optional

from doc_extractor.api.config import ApiSettings, bridge_to_engine_settings, load_api_settings
from doc_extractor.api.contracts.tenant_bound import require_path_safe_tenant_id
from doc_extractor.api.exceptions import ContainerUnavailableError, PortNotConfiguredError
from doc_extractor.api.ports.audit import AuditLogger
from doc_extractor.api.ports.authentication import AuthenticationProvider
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
from doc_extractor.api.services.document_service import DocumentService
from doc_extractor.api.services.review_task_service import ReviewTaskService
from doc_extractor.config import Settings
from doc_extractor.exceptions import InvalidTenantIdentifierError
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.api.audit_chain import DurableAuditLogger, HashChainedAuditRepository
from doc_extractor.distributed_state import RedisIdempotencyStore, RedisLockProvider, connect_redis, tenant_lock_factory
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.malware import MalwareScanner, build_malware_scanner
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.processing_service import DocumentProcessingService
from doc_extractor.routing import RoutedExtractor
from doc_extractor.storage import LocalDiskStorage

logger = logging.getLogger("doc_extractor.api.composition")


class ThreadSafeRoutedExtractor(RoutedExtractor):
    """
    Thread-safe subclass of RoutedExtractor ensuring concurrent first access
    cannot initialize the same specialist, adapter, or router model multiple times (NFR-006).
    Uses narrowly-scoped synchronization around model instantiation and adapter caching.
    """

    def __init__(self, table: Optional[Dict[str, Any]] = None, **extractor_kwargs: Any) -> None:
        super().__init__(table=table, **extractor_kwargs)
        self._model_lock = threading.Lock()

    def get_extractor(self, model_name: str, device: Optional[str] = None):
        """
        Thread-safe extractor retrieval with double-checked locking.
        Prevents duplicate model compilation and GPU/CPU memory thrashing.
        """
        if model_name in self._cache:
            return self._cache[model_name]
        with self._model_lock:
            if model_name not in self._cache:
                return super().get_extractor(model_name, device=device)
            return self._cache[model_name]

    def get_adapter(self, route: Any):
        """
        Thread-safe adapter retrieval and cache creation.
        Prevents duplicate concurrent OCR adapter construction and caching.
        """
        if isinstance(route, dict) and route.get("adapter") == "ocr_text":
            key = f"ocr_text::{route.get('ocr_model')}::{route.get('text_model')}"
            if key in self._cache:
                return self._cache[key]
            with self._model_lock:
                if key not in self._cache:
                    return super().get_adapter(route)
                return self._cache[key]
        return super().get_adapter(route)


@dataclass
class TenantComponents:
    """
    Bundle of tenant-bound processing components.
    Guarantees strict tenant isolation across storage, pipelines, and services.

    Lifecycle & Cleanup Contract:
    - Cache eviction currently releases memory/object references only; .cleanup() is a non-destructive no-op.
    - Cleanup must NOT destructively delete or unmount underlying storage directories or resources
      that may currently be accessed by in-flight active requests.
    - Introducing destructive resource disposal (e.g., closing file descriptors or connection pools)
      in the future strictly requires a lease/reference-counting mechanism or active-request drain protection.
    """
    tenant_id: str
    storage: LocalDiskStorage
    idempotency_store: LocalIdempotencyStore
    module_a: ModuleAPipeline
    module_b: ModuleBPipeline
    processing_service: DocumentProcessingService

    def cleanup(self) -> None:
        """
        Safe cleanup hook when evicted from cache or during application shutdown.
        Currently a non-destructive hook ensuring active request safety.
        """
        logger.debug("Cleaning up tenant components for tenant: %s", self.tenant_id)


class TenantComponentCache:
    """
    Thread-safe, bounded LRU cache for tenant-bound components.
    Prevents tenant state leaking across requests while bounding memory footprint.
    """

    def __init__(self, max_size: int = 100) -> None:
        if max_size <= 0:
            raise ValueError("max_size must be greater than zero.")
        self.max_size = max_size
        self._cache: OrderedDict[str, TenantComponents] = OrderedDict()
        self._lock = threading.RLock()

    def get(self, tenant_id: str) -> Optional[TenantComponents]:
        """Retrieve cached components, promoting the entry to most recently used."""
        with self._lock:
            if tenant_id in self._cache:
                self._cache.move_to_end(tenant_id)
                return self._cache[tenant_id]
            return None

    def put(self, tenant_id: str, components: TenantComponents) -> Optional[TenantComponents]:
        """
        Store components for tenant_id.
        If cache exceeds max_size, evicts the least recently used entry and invokes its cleanup.
        Returns the evicted TenantComponents instance if an eviction occurred, else None.
        """
        with self._lock:
            if tenant_id in self._cache:
                self._cache.move_to_end(tenant_id)
                self._cache[tenant_id] = components
                return None

            evicted: Optional[TenantComponents] = None
            if len(self._cache) >= self.max_size:
                _, evicted = self._cache.popitem(last=False)
                try:
                    evicted.cleanup()
                except Exception as exc:
                    logger.error("Error during evicted tenant components cleanup (%s): %s", evicted.tenant_id, exc)

            self._cache[tenant_id] = components
            return evicted

    def evict(self, tenant_id: str) -> Optional[TenantComponents]:
        """Explicitly evict components for a single tenant, invoking cleanup."""
        with self._lock:
            components = self._cache.pop(tenant_id, None)
            if components is not None:
                try:
                    components.cleanup()
                except Exception as exc:
                    logger.error("Error cleaning up tenant components (%s): %s", tenant_id, exc)
            return components

    def clear(self) -> None:
        """Clear all cached tenant components, invoking cleanup on each."""
        with self._lock:
            while self._cache:
                _, components = self._cache.popitem()
                try:
                    components.cleanup()
                except Exception as exc:
                    logger.error("Error during cache clear for tenant %s: %s", components.tenant_id, exc)

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def contains(self, tenant_id: str) -> bool:
        with self._lock:
            return tenant_id in self._cache


class ApplicationContainer:
    """
    Centralized FastAPI Composition Root.
    Manages application-scoped singletons, lazy tenant-isolated pipelines, and port wiring.
    """

    def __init__(
        self,
        api_settings: Optional[ApiSettings] = None,
        engine_settings: Optional[Settings] = None,
        malware_scanner: Optional[MalwareScanner] = None,
        routed_extractor: Optional[RoutedExtractor] = None,
        module_c: Optional[ModuleCPipeline] = None,
        idempotency_store: Optional[LocalIdempotencyStore] = None,
        tenant_cache_size: int = 100,
        auth_provider: Optional[AuthenticationProvider] = None,
        document_metadata_repo: Optional[DocumentMetadataRepository] = None,
        review_task_repo: Optional[ReviewTaskRepository] = None,
        api_key_repo: Optional[ApiKeyRepository] = None,
        webhook_subscription_repo: Optional[WebhookSubscriptionRepository] = None,
        webhook_delivery_repo: Optional[WebhookDeliveryRepository] = None,
        audit_repo: Optional[AuditRepository] = None,
        queue_producer: Optional[QueueProducer] = None,
        queue_consumer: Optional[QueueConsumer] = None,
        audit_logger: Optional[AuditLogger] = None,
        schema_manager: Optional[Any] = None,
        schema_approval_service: Optional[Any] = None,
        rate_limiter: Optional[Any] = None,
    ) -> None:
        self.api_settings = api_settings or load_api_settings()
        self.engine_settings = engine_settings or bridge_to_engine_settings(self.api_settings)

        # 1. Application-scoped malware scanner (shared between health and processing)
        if malware_scanner is not None:
            self.malware_scanner = malware_scanner
        else:
            self.malware_scanner = build_malware_scanner(
                scanner_type=self.engine_settings.malware_scanner_type,
                enabled=self.engine_settings.malware_scan_enabled,
                app_env=self.engine_settings.app_env,
                clamav_address=self.engine_settings.malware_clamav_address,
                clamav_timeout_seconds=self.engine_settings.malware_scan_timeout_seconds,
            )

        # 2. Application-scoped thread-safe RoutedExtractor (loaded once per process)
        if routed_extractor is not None:
            self.routed_extractor = routed_extractor
        else:
            self.routed_extractor = ThreadSafeRoutedExtractor(allow_fallback=False)

        # 3. Application-scoped canonical ModuleCPipeline
        if module_c is not None:
            self.module_c = module_c
        else:
            self.module_c = ModuleCPipeline(extractor=SchemaGuidedExtractor(use_llm=False))

        # 4. Application-scoped idempotency store. With redis_url set it is shared by every replica (ARC-003, NFR-006);
        #    otherwise it is the single-process LocalIdempotencyStore (shared across tenants, per-(tenant, key) isolation).
        if idempotency_store is not None:
            self.idempotency_store = idempotency_store
        elif self.engine_settings.redis_url:
            self.idempotency_store = RedisIdempotencyStore(connect_redis(self.engine_settings.redis_url))
        else:
            self.idempotency_store = LocalIdempotencyStore(self.engine_settings.storage_base_dir)

        # 5. Bounded tenant component cache
        self.tenant_cache = TenantComponentCache(max_size=tenant_cache_size)
        self._tenant_creation_lock = threading.Lock()

        # 6. Hexagonal ports and repositories
        self.auth_provider = auth_provider
        self.document_metadata_repo = document_metadata_repo
        self.review_task_repo = review_task_repo
        self.api_key_repo = api_key_repo
        self.webhook_subscription_repo = webhook_subscription_repo
        self.webhook_delivery_repo = webhook_delivery_repo
        # Durable, hash-chained audit trail (SEC-008). Configured by audit_store_dir; a write failure fails the operation.
        if audit_repo is None and audit_logger is None and self.api_settings.audit_store_dir:
            lock_factory = None
            if self.engine_settings.redis_url:
                lock_factory = tenant_lock_factory(RedisLockProvider(connect_redis(self.engine_settings.redis_url)))
            audit_repo = HashChainedAuditRepository(self.api_settings.audit_store_dir, lock_factory=lock_factory)
        if audit_logger is None and audit_repo is not None and isinstance(audit_repo, HashChainedAuditRepository):
            audit_logger = DurableAuditLogger(audit_repo)
        self.audit_repo = audit_repo
        self.queue_producer = queue_producer
        self.queue_consumer = queue_consumer
        self.audit_logger = audit_logger
        # Schema lifecycle (SCH-001..006). Deployment supplies a manager over a durable SchemaRepository. Nothing is
        # defaulted here: the in-memory repository must never back production schemas.
        self.schema_manager = schema_manager
        self.schema_approval_service = schema_approval_service
        # API-012: a shared token bucket across replicas. Built only when a capacity and a Redis URL are both configured.
        self.rate_limiter = rate_limiter
        if self.rate_limiter is None and self.api_settings.rate_limit_capacity and self.engine_settings.redis_url:
            from doc_extractor.rate_limit import RedisTokenBucket

            self.rate_limiter = RedisTokenBucket(
                connect_redis(self.engine_settings.redis_url),
                capacity=self.api_settings.rate_limit_capacity,
                refill_per_second=self.api_settings.rate_limit_refill_per_second,
            )

        self._is_shutdown = False

    def get_tenant_components(self, tenant_id: str) -> TenantComponents:
        """
        Lazily creates or retrieves tenant-bound components for tenant_id.
        Fails closed on missing or invalid tenant ID.
        """
        if self._is_shutdown:
            raise ContainerUnavailableError("ApplicationContainer has been shut down.")

        if not isinstance(tenant_id, str):
            raise InvalidTenantIdentifierError("Tenant ID must be a string.")
        if not tenant_id:
            raise InvalidTenantIdentifierError("Tenant ID cannot be empty.")
        if not tenant_id.strip():
            raise InvalidTenantIdentifierError("Tenant ID cannot be whitespace.")
        require_path_safe_tenant_id(tenant_id)

        # Fast path: check cache
        cached = self.tenant_cache.get(tenant_id)
        if cached is not None:
            return cached

        with self._tenant_creation_lock:
            # Recheck under lock
            cached = self.tenant_cache.get(tenant_id)
            if cached is not None:
                return cached

            storage = LocalDiskStorage(self.engine_settings.storage_base_dir, tenant_id=tenant_id)
            module_a = ModuleAPipeline(
                settings=self.engine_settings,
                scanner=self.malware_scanner,
                storage=storage,
                idempotency_store=self.idempotency_store,
                tenant_id=tenant_id,
                logger=logging.getLogger("doc_extractor"),
            )
            module_b = ModuleBPipeline(
                storage=storage,
                tenant_id=tenant_id,
                settings=self.engine_settings,
            )
            processing_service = DocumentProcessingService(
                module_a=module_a,
                module_b=module_b,
                module_c=self.module_c,
                routed_extractor=self.routed_extractor,
                storage=storage,
                idempotency_store=self.idempotency_store,
                settings=self.engine_settings,
                tenant_id=tenant_id,
            )

            components = TenantComponents(
                tenant_id=tenant_id,
                storage=storage,
                idempotency_store=self.idempotency_store,
                module_a=module_a,
                module_b=module_b,
                processing_service=processing_service,
            )
            self.tenant_cache.put(tenant_id, components)
            return components

    def get_document_service(
        self,
        tenant_id: str,
        document_metadata_repo: Optional[DocumentMetadataRepository] = None,
        queue_producer: Optional[QueueProducer] = None,
        audit_logger: Optional[AuditLogger] = None,
    ) -> DocumentService:
        """Retrieve a tenant-bound DocumentService application boundary."""
        if self._is_shutdown:
            raise ContainerUnavailableError("ApplicationContainer has been shut down.")
        components = self.get_tenant_components(tenant_id)
        return DocumentService(
            processing_service=components.processing_service,
            tenant_id=tenant_id,
            document_metadata_repo=document_metadata_repo or self.document_metadata_repo,
            queue_producer=queue_producer or self.queue_producer,
            audit_logger=audit_logger or self.audit_logger,
        )

    def get_document_read_service(self, tenant_id: str) -> DocumentReadService:
        """
        Lazily creates a lightweight tenant-bound DocumentReadService.
        Does NOT instantiate Module A, Module B, or specialist extraction pipelines.
        """
        if self._is_shutdown:
            raise ContainerUnavailableError("ApplicationContainer has been shut down.")

        if not isinstance(tenant_id, str):
            raise InvalidTenantIdentifierError("Tenant ID must be a string.")
        if not tenant_id:
            raise InvalidTenantIdentifierError("Tenant ID cannot be empty.")
        if not tenant_id.strip():
            raise InvalidTenantIdentifierError("Tenant ID cannot be whitespace.")
        require_path_safe_tenant_id(tenant_id)

        storage = LocalDiskStorage(self.engine_settings.storage_base_dir, tenant_id=tenant_id)
        return DocumentReadService(storage=storage, tenant_id=tenant_id)

    def get_review_task_service(
        self,
        tenant_id: str,
        principal_id: str,
        review_task_repo: Optional[ReviewTaskRepository] = None,
    ) -> ReviewTaskService:
        """
        Retrieve a tenant- and principal-bound ReviewTaskService (API-005, API-016). Lightweight: no extraction pipeline is
        created. There is no production adapter for the repository port yet, so an unconfigured port fails closed with the
        typed 503 (``PortNotConfiguredError``): nothing is substituted.
        """
        if self._is_shutdown:
            raise ContainerUnavailableError("ApplicationContainer has been shut down.")
        if not isinstance(tenant_id, str):
            raise InvalidTenantIdentifierError("Tenant ID must be a string.")
        if not tenant_id.strip():
            raise InvalidTenantIdentifierError("Tenant ID cannot be empty or whitespace.")
        require_path_safe_tenant_id(tenant_id)
        repository = review_task_repo or self.review_task_repo
        if repository is None:
            raise PortNotConfiguredError("ReviewTaskRepository")
        return ReviewTaskService(repository=repository, tenant_id=tenant_id, principal_id=principal_id)

    def shutdown(self) -> None:
        """
        Deterministic application shutdown.
        Cleans up and clears cached tenant components and releases resources.
        """
        if self._is_shutdown:
            return
        logger.info("Shutting down ApplicationContainer. Evicting tenant components.")
        self._is_shutdown = True
        self.tenant_cache.clear()
