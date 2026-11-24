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
from doc_extractor.api.ports.processing import ProcessingLeaseStore
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
from doc_extractor.infrastructure.mongo import DEFAULT_REGISTRY, IndexRegistry, MongoConnection, MongoSettings, ensure_indexes
from doc_extractor.infrastructure.mongo.errors import PersistenceConfigurationError
from doc_extractor.infrastructure.mongo.settings import HARDENED_ENVIRONMENTS
from doc_extractor.infrastructure.queue.errors import BrokerConfigurationError
from doc_extractor.infrastructure.queue.settings import BrokerSettings
from doc_extractor.distributed_state import RedisIdempotencyStore, RedisLockProvider, connect_redis, tenant_lock_factory
from doc_extractor.idempotency import LocalIdempotencyStore
from doc_extractor.malware import MalwareScanner, build_malware_scanner
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.processing_service import DocumentProcessingService
from doc_extractor.routing import RoutedExtractor
from doc_extractor.storage import LocalDiskStorage, StorageBackend

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
    storage: StorageBackend
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
        mongo_connection: Optional[MongoConnection] = None,
        index_registry: Optional[IndexRegistry] = None,
        processing_lease_store: Optional[ProcessingLeaseStore] = None,
        broker_transport: Optional[Any] = None,
        tenant_repo: Optional[Any] = None,
        tenant_service: Optional[Any] = None,
        identity_service: Optional[Any] = None,
        webhook_dispatcher: Optional[Any] = None,
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
        self._idempotency_supplied = idempotency_store is not None
        self._idempotency_replaced_at_startup = False
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
        # Durable queue (Phase 3). The broker is configured only by an explicit URL; nothing falls back to an in-memory queue. The
        # job-ownership store and the broker connection are created by ``startup()`` (they connect inside the event loop).
        self.processing_lease_store = processing_lease_store
        self.broker_settings: Optional[BrokerSettings] = BrokerSettings.from_api_settings(self.api_settings)
        self._broker_transport = broker_transport
        self._owned_queue: Optional[Any] = None
        self.audit_logger = audit_logger
        # Schema lifecycle (SCH-001..006). Deployment supplies a manager over a durable SchemaRepository. Nothing is
        # defaulted here: the in-memory repository must never back production schemas.
        self.schema_manager = schema_manager
        self.schema_approval_service = schema_approval_service
        self.tenant_repo = tenant_repo
        self.tenant_service = tenant_service
        self.identity_service = identity_service
        self.webhook_dispatcher = webhook_dispatcher
        # API-012: a shared token bucket across replicas. Built only when a capacity and a Redis URL are both configured.
        self.rate_limiter = rate_limiter
        if self.rate_limiter is None and self.api_settings.rate_limit_capacity and self.engine_settings.redis_url:
            from doc_extractor.rate_limit import RedisTokenBucket

            self.rate_limiter = RedisTokenBucket(
                connect_redis(self.engine_settings.redis_url),
                capacity=self.api_settings.rate_limit_capacity,
                refill_per_second=self.api_settings.rate_limit_refill_per_second,
            )

        # MongoDB (Team Member 3, Phase 2A). Built from the API settings when a connection string is configured; nothing
        # connects here (construction is synchronous and opens no socket): ``startup()`` connects inside the event loop.
        # There is NO in-memory fallback. A configured database that is unreachable or invalid stops the application at
        # start-up. An unconfigured one leaves the persistence ports unconfigured (503) in DEVELOPMENT only: staging and
        # production refuse to start without one (``_require_durable_database``).
        self.mongo: Optional[MongoConnection] = mongo_connection
        if self.mongo is None:
            mongo_settings = MongoSettings.from_api_settings(self.api_settings)
            if mongo_settings is not None:
                self.mongo = MongoConnection(mongo_settings)
        self.index_registry: IndexRegistry = index_registry or DEFAULT_REGISTRY

        self._is_shutdown = False

    def _require_durable_database(self) -> None:
        """
        The database start-up gate of staging and production (Phase 2A-5). Development keeps MongoDB optional.

        In a hardened environment the application must not run without a verified durable database, however its repositories
        were supplied: an injected repository does NOT waive this, and there is no flag that does. Raises
        ``PersistenceConfigurationError`` with a fixed reason (never a connection string, host, path or credential) when
        - no MongoDB is configured (neither a connection string / file in the settings nor an injected connection), or
        - the connection in use was built for a different, non-hardened environment (so its TLS, authentication and
          replica-set policy was not the one this environment requires).
        An unreachable server, a failed replica-set check or a failed index bootstrap are raised by ``startup()`` itself.
        """
        environment = self.api_settings.app_env
        if environment not in HARDENED_ENVIRONMENTS:
            return
        if self.mongo is None:
            logger.error("Start-up refused: MongoDB is required in %s and is not configured (reason=database_is_required)", environment)
            raise PersistenceConfigurationError("database_is_required_in_staging_and_production", setting="mongo_uri")
        deployment = self.mongo.settings
        if deployment.app_env != environment or not deployment.require_replica_set:
            logger.error("Start-up refused: the MongoDB settings do not match the %s environment (reason=settings_mismatch)", environment)
            raise PersistenceConfigurationError("database_settings_do_not_match_the_environment", setting="mongo_uri")

    async def startup(self) -> None:
        """
        Asynchronous start-up, run once inside the event loop by the application lifespan. Idempotent.
        In staging and production a verified MongoDB is REQUIRED (``_require_durable_database``); in development it is optional.
        Connects MongoDB (when configured), applies the declared indexes (when enabled) and then configures the MongoDB
        ``ReviewTaskRepository``, ``ApiKeyRepository`` and durable audit repository with its logger (each unless one was injected;
        audit also yields to an explicitly configured file store). A failure raises a typed persistence error with a fixed message;
        the application does not start, and nothing falls back to another store.
        """
        self._require_durable_database()
        if self.mongo is None:
            if self.broker_settings is not None or self._broker_transport is not None:
                # The lease store (ownership, attempts, fencing) is the authority on every job and lives in MongoDB.
                raise BrokerConfigurationError("mongodb_is_required_for_processing_state", setting="broker_url")
            return
        await self.mongo.connect()
        if self.mongo.settings.ensure_indexes_on_startup:
            try:
                await ensure_indexes(self.mongo.database, self.index_registry)
            except Exception:
                await self.mongo.aclose()          # leave nothing open when start-up fails
                raise
        # The MongoDB adapters are configured only now: a database that failed its checks above leaves every port
        # unconfigured (503), never half-wired. An adapter injected by the caller always wins. Everything is prepared and
        # checked FIRST and assigned LAST, so a failure part-way leaves nothing configured.
        # Imported here, not at module level: the adapters import the API's ports, and this module is part of the package
        # those imports load (a module-level import would be circular).
        review_task_repo = api_key_repo = audit_repo = audit_logger = None
        idempotency_store = lease_store = queue = None
        started: list = []
        # Imported BEFORE the guarded block below: an import of this distribution must never sit inside a ``try`` that catches ``Exception``
        # (tests/test_api_m4_roles_direct_import.py), even though that block re-raises after closing what it opened.
        from doc_extractor.infrastructure.mongo.api_keys import MongoApiKeyRepository
        from doc_extractor.infrastructure.mongo.audit import MongoAuditRepository
        from doc_extractor.infrastructure.mongo.idempotency import MongoIdempotencyStore
        from doc_extractor.infrastructure.mongo.processing import MongoProcessingLeaseStore
        from doc_extractor.infrastructure.mongo.review_tasks import MongoReviewTaskRepository
        from doc_extractor.infrastructure.queue.rabbitmq import RabbitMQQueue
        from doc_extractor.infrastructure.queue.transport import AioPikaTransport

        try:
            if self.review_task_repo is None:
                review_task_repo = MongoReviewTaskRepository(self.mongo)
            if self.api_key_repo is None:
                api_key_repo = MongoApiKeyRepository(self.mongo)
            # Durable audit (Phase 2B). Only when NOTHING audit-related was supplied: an injected audit repository or logger, or an
            # explicitly configured file audit store (``audit_store_dir``), is never replaced, and no best-effort sink ever stands in.
            # The chain is only safe with its unique (tenant, sequence) index, so that index is verified even when bootstrap is off.
            if self.audit_repo is None and self.audit_logger is None:
                audit_repo = MongoAuditRepository(self.mongo)
                await audit_repo.require_unique_sequence_index()
                audit_logger = DurableAuditLogger(audit_repo)
            # Durable idempotency (Phase 3): the unique record in MongoDB is the source of truth; Redis (when configured) only coordinates
            # the per-key lock. An injected store is never replaced. The unique index is verified even when bootstrap is off.
            if not self._idempotency_supplied:
                locks = RedisLockProvider(connect_redis(self.engine_settings.redis_url)) if self.engine_settings.redis_url else None
                idempotency_store = MongoIdempotencyStore(self.mongo, locks=locks)
                await idempotency_store.start()
                started.append(idempotency_store)
            # Durable queue (Phase 3): only when a broker is configured (or a transport injected) and a queue port is still empty.
            # Injected producers and consumers always win. The lease store is required first; the broker connects and declares its
            # topology here, so an unreachable or refusing broker stops start-up (fail closed, no in-memory fallback).
            wants_queue = self.broker_settings is not None or self._broker_transport is not None
            if wants_queue and (self.queue_producer is None or self.queue_consumer is None):
                lease_store = self.processing_lease_store
                if lease_store is None:
                    lease_store = MongoProcessingLeaseStore(self.mongo)
                    await lease_store.require_unique_job_index()
                transport = self._broker_transport or AioPikaTransport(self.broker_settings)
                queue = RabbitMQQueue(
                    transport, lease_store,
                    max_in_flight=self.broker_settings.max_in_flight if self.broker_settings is not None else 10,
                )
                await queue.start()
                started.append(queue)
        except Exception:
            for adapter in reversed(started):          # leave nothing open and nothing configured when start-up fails
                try:
                    if hasattr(adapter, "stop"):
                        adapter.stop()
                    else:
                        await adapter.close()
                except Exception:                      # noqa: BLE001 - closing is best effort; the original failure is what matters
                    pass
            await self.mongo.aclose()
            raise
        self.review_task_repo = review_task_repo or self.review_task_repo
        self.api_key_repo = api_key_repo or self.api_key_repo
        if audit_repo is not None:
            self.audit_repo, self.audit_logger = audit_repo, audit_logger
        if idempotency_store is not None:
            self.idempotency_store = idempotency_store
            self._idempotency_replaced_at_startup = True
        if lease_store is not None:
            self.processing_lease_store = lease_store
        if queue is not None:
            self._owned_queue = queue
            self.queue_producer = self.queue_producer or queue
            self.queue_consumer = self.queue_consumer or queue
        # Schema lifecycle (Phase 5B): wire durable MongoDB repository and approval service when mongo is configured
        if self.schema_manager is None and self.mongo is not None:
            from doc_extractor.infrastructure.mongo.schemas import MongoSchemaRepository, ensure_schema_indexes
            from schema_lifecycle.manager import SchemaLifecycleManager
            from schema_lifecycle.approval import BenchmarkResultVerifier, InMemoryBenchmarkReportStore, SchemaApprovalService
            from schema_lifecycle.roles import ProductionSchemaAuthorizer

            await ensure_schema_indexes(self.mongo.database)
            schema_repo = MongoSchemaRepository(self.mongo)
            self.schema_manager = SchemaLifecycleManager(repository=schema_repo)
            if self.schema_approval_service is None:
                authorizer = ProductionSchemaAuthorizer()
                report_store = InMemoryBenchmarkReportStore()
                verifier = BenchmarkResultVerifier(report_store=report_store)
                self.schema_approval_service = SchemaApprovalService(
                    manager=self.schema_manager,
                    authorizer=authorizer,
                    verifier=verifier,
                    report_store=report_store,
                )

        # Phase 5C: Tenant provisioning, persistent identity and webhook security
        if self.mongo is not None:
            from doc_extractor.infrastructure.mongo.webhooks import (
                MongoWebhookSubscriptionRepository,
                MongoWebhookDeliveryRepository,
                ensure_webhook_indexes,
            )
            from doc_extractor.infrastructure.mongo.tenants import (
                MongoTenantRepository,
                ensure_tenant_indexes,
            )
            from doc_extractor.infrastructure.mongo.identity import (
                MongoUserRepository,
                MongoRefreshTokenRepository,
                MongoTokenRevocationStore,
                ensure_identity_indexes,
            )
            from doc_extractor.infrastructure.tenancy.service import TenantProvisioningService
            from doc_extractor.infrastructure.webhook.dispatcher import WebhookDispatcher
            from doc_extractor.infrastructure.webhook.security import EphemeralSecretResolver
            from doc_extractor.egress import EgressPolicy

            await ensure_webhook_indexes(self.mongo.database)
            await ensure_tenant_indexes(self.mongo.database)
            await ensure_identity_indexes(self.mongo.database)

            if self.webhook_subscription_repo is None:
                self.webhook_subscription_repo = MongoWebhookSubscriptionRepository(
                    self.mongo,
                    allow_loopback=(self.engine_settings.app_env == "development"),
                )
            if self.webhook_delivery_repo is None:
                self.webhook_delivery_repo = MongoWebhookDeliveryRepository(self.mongo)

            if self.tenant_repo is None:
                self.tenant_repo = MongoTenantRepository(self.mongo)
            if self.tenant_service is None:
                self.tenant_service = TenantProvisioningService(
                    self.tenant_repo,
                    audit_logger=self.audit_logger,
                )

            if self.webhook_dispatcher is None:
                egress = EgressPolicy.from_environment()
                self.webhook_dispatcher = WebhookDispatcher(
                    subscription_repo=self.webhook_subscription_repo,
                    delivery_repo=self.webhook_delivery_repo,
                    secret_resolver=EphemeralSecretResolver(),
                    egress_policy=egress,
                    queue_producer=self.queue_producer,
                    audit_logger=self.audit_logger,
                    allow_loopback=(self.engine_settings.app_env == "development"),
                )

    async def ashutdown(self) -> None:
        """
        Asynchronous shutdown: closes the broker connection this container opened, stops the MongoDB idempotency bridge and closes the
        MongoDB client. Idempotent; the synchronous ``shutdown`` is unchanged. Unsettled deliveries return to the queue.
        """
        queue, self._owned_queue = self._owned_queue, None
        if queue is not None:
            try:
                await queue.close()
            except Exception:                          # noqa: BLE001 - shutdown must reach the database close
                logger.warning("The message broker did not close cleanly")
        stop = getattr(self.idempotency_store, "stop", None)
        if self._idempotency_replaced_at_startup and callable(stop):
            stop()
        if self.mongo is not None:
            await self.mongo.aclose()

    def get_storage(self, tenant_id: str) -> StorageBackend:
        """
        Create or retrieve the tenant-bound storage backend based on configuration.
        Fails closed: if remote S3 storage is configured but invalid, raises typed error
        and NEVER falls back to local disk or memory.
        """
        require_path_safe_tenant_id(tenant_id)
        if getattr(self.api_settings, "storage_backend", "local") == "s3":
            from doc_extractor.infrastructure.storage.adapter import S3Storage
            from doc_extractor.infrastructure.storage.settings import S3StorageSettings

            s3_settings = S3StorageSettings.from_api_settings(self.api_settings)
            if s3_settings is None:
                raise TenantStorageUnavailableError("s3_configuration_missing")
            return S3Storage(settings=s3_settings, tenant_id=tenant_id)
        return LocalDiskStorage(self.engine_settings.storage_base_dir, tenant_id=tenant_id)

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

            storage = self.get_storage(tenant_id)
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

        if getattr(self.api_settings, "storage_backend", "local") == "s3":
            # DocumentReadService performs descriptor-relative directory operations (openat) on LocalDiskStorage.
            # Reading document records and images under S3 object storage depends on Open Decision M5-D18.
            raise TenantStorageUnavailableError("document_read_service_requires_local_disk_storage")

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
