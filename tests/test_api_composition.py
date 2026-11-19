"""
Unit and integration tests for FastAPI Composition Root and Application Lifecycle (Milestone 2B).

Covers:
1. Application startup and shutdown (deterministic lifespan execution and container shutdown).
2. Exactly one shared extractor per application across multiple tenant requests.
3. Correct shared Module C lifecycle (one instance across tenants).
4. Tenant A and Tenant B receive strictly isolated tenant-bound components.
5. Repeated requests for one tenant reuse cached components.
6. Cache bounds and eviction cleanup (LRU eviction when cache exceeds max_size, calling cleanup()).
7. Scanner identity consistency between health endpoint and processing configuration.
8. API-to-engine settings mapping and invalid configuration (zero-egress, debug-in-production).
9. Concurrent model initialization does not create duplicate instances (thread-safe routing).
10. No tenant state leaks across requests (isolated directories, cross-tenant rejection).
11. FastAPI dependency injection providers (container, tenant components, document service).
"""

from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import tempfile
import threading
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("fastapi", reason="optional API extra not installed (pip install -e '.[dev,api]')")

from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from doc_extractor.adapters import OCRTextAdapter
from doc_extractor.api import (
    ApplicationContainer,
    DocumentService,
    TenantComponentCache,
    TenantComponents,
    ThreadSafeRoutedExtractor,
    bridge_to_api_settings,
    bridge_to_engine_settings,
    create_app,
)
from doc_extractor.api import dependencies as deps
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.exceptions import ContainerUnavailableError, PortNotConfiguredError
from doc_extractor.api.ports.authentication import ApiPrincipal, PrincipalType
from doc_extractor.config import Settings
from doc_extractor.exceptions import (
    ConfigurationError,
    InvalidTenantIdentifierError,
    TenantMismatchError,
)
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.processing_service import (
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentProcessingService,
)
from doc_extractor.routing import RoutedExtractor
from doc_extractor import vlm_runtime


class MockAuthProvider:
    """Mock authentication provider deriving tenant context from authorization token."""

    async def authenticate(self, credentials: str, request: Optional[Request] = None) -> Optional[ApiPrincipal]:
        if credentials and credentials.startswith("Bearer valid_"):
            tid = credentials.replace("Bearer valid_", "").strip()
            return ApiPrincipal(
                principal_id=f"user_{tid}",
                tenant_id=tid,
                principal_type=PrincipalType.API_KEY,
                roles={"api_user"},
                scopes={"documents:read", "documents:write"},
            )
        return None


# ============================================================================
# 1. Application Container Lifecycle (Startup and Shutdown)
# ============================================================================

def test_container_lifespan_startup_and_shutdown():
    """Verify lifespan context initializes container and cleans up on shutdown."""
    settings = ApiSettings(app_env="development")
    app = create_app(settings)

    assert hasattr(app.state, "container")
    container = app.state.container
    assert isinstance(container, ApplicationContainer)
    assert not container._is_shutdown

    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200

        # Create tenant components inside lifespan
        components = container.get_tenant_components("tenant-lifecycle")
        assert components.tenant_id == "tenant-lifecycle"
        assert container.tenant_cache.contains("tenant-lifecycle")

    # Outside the lifespan context manager, container must be shut down
    assert container._is_shutdown
    assert len(container.tenant_cache) == 0


def test_container_shutdown_closes_cache_and_refuses_operations():
    """Verify ApplicationContainer refuses component retrieval after shutdown with typed 503 error."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    container.get_tenant_components("tenant-1")
    assert len(container.tenant_cache) == 1

    container.shutdown()
    assert container._is_shutdown
    assert len(container.tenant_cache) == 0

    with pytest.raises(ContainerUnavailableError, match="ApplicationContainer has been shut down"):
        container.get_tenant_components("tenant-1")

    with pytest.raises(ContainerUnavailableError, match="ApplicationContainer has been shut down"):
        container.get_document_service("tenant-1")


def test_container_shutdown_is_idempotent():
    """Calling shutdown multiple times must be safe and idempotent."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)
    container.shutdown()
    container.shutdown()
    assert container._is_shutdown


def test_sequential_lifespan_contexts_on_same_app_recreates_container():
    """Verify an app instance can execute sequential lifespan contexts and safely recreate its container."""
    settings = ApiSettings(app_env="development")
    app = create_app(settings)

    # First lifespan run
    with TestClient(app) as client1:
        assert client1.get("/health").status_code == 200
        c1 = app.state.container
        assert not c1._is_shutdown
        comp1 = c1.get_tenant_components("tenant-run-1")
        assert comp1.tenant_id == "tenant-run-1"

    # After exit of first lifespan, c1 is shut down
    assert c1._is_shutdown

    # Second lifespan run on the SAME app instance
    with TestClient(app) as client2:
        assert client2.get("/health").status_code == 200
        c2 = app.state.container
        assert not c2._is_shutdown
        assert c2 is not c1
        comp2 = c2.get_tenant_components("tenant-run-2")
        assert comp2.tenant_id == "tenant-run-2"

    # After exit of second lifespan, c2 is shut down
    assert c2._is_shutdown


def test_caller_owned_container_not_shut_down_by_app():
    """Verify a caller-supplied container is not shut down by the FastAPI application."""
    settings = ApiSettings(app_env="development")
    caller_container = ApplicationContainer(api_settings=settings)
    app = create_app(container=caller_container)

    assert app.state.owns_container is False
    assert app.state.container is caller_container

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        comp = caller_container.get_tenant_components("tenant-caller")
        assert comp.tenant_id == "tenant-caller"

    # After lifespan exit, the caller-owned container MUST NOT be shut down
    assert caller_container._is_shutdown is False
    assert caller_container.tenant_cache.contains("tenant-caller")

    # Caller explicitly cleans it up
    caller_container.shutdown()
    assert caller_container._is_shutdown is True


def test_startup_failure_cleans_up_container(monkeypatch):
    """Verify startup failures inside lifespan clean up newly allocated container resources."""
    settings = ApiSettings(app_env="development")
    app = create_app(settings)
    container = app.state.container
    assert not container._is_shutdown

    def raise_during_startup(level, service_name, environment, clear_existing):
        raise RuntimeError("Simulated startup failure inside lifespan")

    monkeypatch.setattr("doc_extractor.api.app.setup_logging", raise_during_startup)

    with pytest.raises(RuntimeError, match="Simulated startup failure inside lifespan"):
        with TestClient(app):
            pass

    # The container created during app creation must be safely shut down by finally block
    assert container._is_shutdown is True


# ============================================================================
# 2. Exactly One Shared Extractor per Application Across Multiple Tenants
# ============================================================================

def test_exactly_one_routed_extractor_shared_across_tenants():
    """Verify multiple tenants share the exact same RoutedExtractor instance."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    comp_t1 = container.get_tenant_components("tenant-alpha")
    comp_t2 = container.get_tenant_components("tenant-beta")
    comp_t3 = container.get_tenant_components("tenant-gamma")

    # All tenant processing services reference the exact same container routed extractor
    assert comp_t1.processing_service.routed_extractor is container.routed_extractor
    assert comp_t2.processing_service.routed_extractor is container.routed_extractor
    assert comp_t3.processing_service.routed_extractor is container.routed_extractor
    assert comp_t1.processing_service.routed_extractor is comp_t2.processing_service.routed_extractor


# ============================================================================
# 3. Correct Shared Module C Lifecycle (One Instance Across Tenants)
# ============================================================================

def test_shared_module_c_lifecycle_across_tenants():
    """Verify Module C pipeline is application-scoped and reused across tenants."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    comp_t1 = container.get_tenant_components("tenant-alpha")
    comp_t2 = container.get_tenant_components("tenant-beta")

    assert comp_t1.processing_service.module_c is container.module_c
    assert comp_t2.processing_service.module_c is container.module_c
    assert comp_t1.processing_service.module_c is comp_t2.processing_service.module_c
    assert isinstance(container.module_c, ModuleCPipeline)


# ============================================================================
# 4. Strict Tenant Component Isolation
# ============================================================================

def test_tenant_components_strict_isolation():
    """Verify Tenant A and Tenant B receive strictly isolated components."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    comp_a = container.get_tenant_components("tenant-a")
    comp_b = container.get_tenant_components("tenant-b")

    # Tenant identities match
    assert comp_a.tenant_id == "tenant-a"
    assert comp_b.tenant_id == "tenant-b"

    # Distinct storage instances bound to separate tenants
    assert comp_a.storage is not comp_b.storage
    assert comp_a.storage.tenant_id == "tenant-a"
    assert comp_b.storage.tenant_id == "tenant-b"

    # Distinct Module A instances
    assert comp_a.module_a is not comp_b.module_a
    assert comp_a.module_a.tenant_id == "tenant-a"
    assert comp_b.module_a.tenant_id == "tenant-b"

    # Distinct Module B instances
    assert comp_a.module_b is not comp_b.module_b
    assert comp_a.module_b.tenant_id == "tenant-a"
    assert comp_b.module_b.tenant_id == "tenant-b"

    # Distinct DocumentProcessingService instances
    assert comp_a.processing_service is not comp_b.processing_service
    assert comp_a.processing_service.tenant_id == "tenant-a"
    assert comp_b.processing_service.tenant_id == "tenant-b"


# ============================================================================
# 5. Repeated Requests Reuse Cached Components
# ============================================================================

def test_repeated_requests_reuse_cached_components():
    """Verify repeated retrieval for the same tenant returns identical instances."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    comp_first = container.get_tenant_components("tenant-cached")
    comp_second = container.get_tenant_components("tenant-cached")

    assert comp_first is comp_second
    assert len(container.tenant_cache) == 1


# ============================================================================
# 6. Cache Bounds and Eviction Cleanup
# ============================================================================

def test_tenant_component_cache_bounded_lru_and_cleanup():
    """Verify TenantComponentCache enforces bounded size and calls cleanup upon eviction."""
    cache = TenantComponentCache(max_size=2)
    cleaned_up = []

    def make_components(tid: str) -> TenantComponents:
        c = MagicMock(spec=TenantComponents)
        c.tenant_id = tid
        c.cleanup = MagicMock(side_effect=lambda: cleaned_up.append(tid))
        return c

    c1 = make_components("t1")
    c2 = make_components("t2")
    c3 = make_components("t3")

    cache.put("t1", c1)
    cache.put("t2", c2)
    assert len(cache) == 2

    # Access t1 to mark it as most recently used; t2 becomes LRU
    assert cache.get("t1") is c1

    # Put t3: causes LRU eviction of t2
    evicted = cache.put("t3", c3)
    assert evicted is c2
    assert len(cache) == 2
    assert not cache.contains("t2")
    assert cache.contains("t1")
    assert cache.contains("t3")
    assert "t2" in cleaned_up

    # Explicit evict
    evicted_t1 = cache.evict("t1")
    assert evicted_t1 is c1
    assert "t1" in cleaned_up
    assert len(cache) == 1

    # Clear invokes cleanup on all remaining
    cache.clear()
    assert "t3" in cleaned_up
    assert len(cache) == 0


def test_tenant_component_cache_invalid_max_size():
    """Verify invalid cache size is rejected."""
    with pytest.raises(ValueError, match="max_size must be greater than zero"):
        TenantComponentCache(max_size=0)
    with pytest.raises(ValueError, match="max_size must be greater than zero"):
        TenantComponentCache(max_size=-5)


def test_tenant_components_cleanup_contract_is_non_destructive(tmp_path: Path):
    """
    Verify cleanup() contract (MEDIUM-2):
    Eviction cleanup releases references only and is non-destructive to active storage
    or pipeline resources being handled by in-flight requests.
    """
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings)

    components = container.get_tenant_components("tenant-cleanup-test")
    tenant_dir = components.storage._tenant_dir(create=True)
    assert tenant_dir.exists()

    # Create a test file in storage
    test_file = tenant_dir / "sample.txt"
    test_file.write_text("active request data")

    # Call cleanup()
    components.cleanup()

    # File and directory must remain intact (non-destructive)
    assert test_file.exists()
    assert test_file.read_text() == "active request data"


# ============================================================================
# 7. Scanner Identity Consistency Between Health and Processing
# ============================================================================

def test_scanner_identity_consistency_between_health_and_processing():
    """Verify health endpoint and Module A processing share the exact same scanner instance."""
    settings = ApiSettings(app_env="development")
    custom_scanner = MockMalwareScanner(is_ready=True)
    container = ApplicationContainer(api_settings=settings, malware_scanner=custom_scanner)

    app = create_app(settings=settings, container=container)

    # 1. Scanner on app.state must be the container's scanner
    assert app.state.malware_scanner is custom_scanner
    assert app.state.container.malware_scanner is custom_scanner

    # 2. Scanner passed to Module A must be the exact same object reference
    components = container.get_tenant_components("tenant-scanner-check")
    assert components.module_a.scanner is custom_scanner
    assert components.module_a.scanner is app.state.malware_scanner

    # 3. Health endpoint accurately queries this scanner
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["malware_scanner_engine"] == "MockMalwareScanner"
    assert data["malware_scanner_ready"] is True


# ============================================================================
# 8. API-to-Engine Settings Mapping and Security Boundaries
# ============================================================================

def test_settings_bridge_valid_mapping():
    """Verify faithful mapping between ApiSettings and engine Settings."""
    api_settings = ApiSettings(
        app_env="staging",
        log_level="WARNING",
        storage_base_dir="/var/data/staging",
        malware_scan_enabled=True,
        malware_scanner_type="mock",
    )

    engine_settings = bridge_to_engine_settings(api_settings)
    assert engine_settings.app_env == "staging"
    assert engine_settings.log_level == "WARNING"
    assert engine_settings.storage_base_dir == Path("/var/data/staging")
    assert engine_settings.malware_scan_enabled is True
    assert engine_settings.malware_scanner_type == "mock"
    assert engine_settings.allow_external_egress is False

    # Reverse bridge
    roundtrip = bridge_to_api_settings(engine_settings)
    assert roundtrip.app_env == "staging"
    assert roundtrip.log_level == "WARNING"
    assert roundtrip.storage_base_dir == Path("/var/data/staging")
    assert roundtrip.allow_external_egress is False


def test_settings_bridge_rejects_zero_egress_violation():
    """Verify bridge fails closed if allow_external_egress is True (ARC-001/SEC-011)."""
    api_settings = ApiSettings(app_env="development")
    api_settings.allow_external_egress = True

    with pytest.raises(ConfigurationError, match="external egress is enabled"):
        bridge_to_engine_settings(api_settings)


def test_settings_bridge_rejects_debug_in_production():
    """Verify bridge fails closed if debug=True in production environment."""
    api_settings = ApiSettings(app_env="production")
    api_settings.debug = True

    with pytest.raises(ConfigurationError, match="debug mode disallowed in production"):
        bridge_to_engine_settings(api_settings)


def test_api_settings_convenience_methods():
    """Verify ApiSettings.to_engine_settings and from_engine_settings."""
    api_settings = ApiSettings(app_env="development")
    engine_settings = api_settings.to_engine_settings()
    assert engine_settings.app_env == "development"

    rebuilt = ApiSettings.from_engine_settings(engine_settings)
    assert rebuilt.app_env == "development"


# ============================================================================
# 9. Concurrent Model Initialization Does Not Create Duplicates
# ============================================================================

def test_thread_safe_routed_extractor_concurrency():
    """Verify concurrent calls to get_extractor instantiate the underlying model exactly once."""
    extractor = ThreadSafeRoutedExtractor(allow_fallback=False)
    instantiation_counter = 0
    lock = threading.Lock()

    real_get_extractor = RoutedExtractor.get_extractor

    def slow_instantiate(self, model_name: str, device: Optional[str] = None):
        nonlocal instantiation_counter
        with lock:
            instantiation_counter += 1
        # Return a sentinel mock object
        mock = MagicMock()
        mock.model_name = model_name
        self._cache[model_name] = mock
        return mock

    with patch.object(RoutedExtractor, "get_extractor", slow_instantiate):
        def worker():
            return extractor.get_extractor("test-concurrency-model")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: worker(), range(16)))

        # All threads must receive the exact same instance
        first = results[0]
        assert all(res is first for res in results)
        assert instantiation_counter == 1


def test_thread_safe_lazy_model_initialization_counts_actual_load_qwen_vl():
    """
    HIGH-1: Verify that concurrent first access to SchemaGuidedExtractor._load_model()
    invokes vlm_runtime.load_qwen_vl exactly once across concurrent threads.
    """
    extractor = SchemaGuidedExtractor(
        model_name="Qwen/Qwen3-VL-4B-Instruct",
        use_llm=True,
    )
    assert extractor._model is None
    assert extractor._processor is None

    mock_model = MagicMock()
    mock_processor = MagicMock()
    loaded_result = vlm_runtime.LoadedVLM(
        model=mock_model,
        processor=mock_processor,
        family=vlm_runtime.FAMILY_QWEN3_VL,
        image_config=vlm_runtime.ImageConfig(image_patch_size=16, merge_size=2, min_pixels=100, max_pixels=1000),
        runtime_info={"model_id": "Qwen/Qwen3-VL-4B-Instruct"},
    )

    load_call_count = 0
    load_lock = threading.Lock()

    def mock_load(config):
        nonlocal load_call_count
        with load_lock:
            load_call_count += 1
        time.sleep(0.01)  # Simulate expensive weight initialization / compilation
        return loaded_result

    with patch.object(vlm_runtime, "load_qwen_vl", side_effect=mock_load):
        def worker():
            return extractor._load_model()

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: worker(), range(16)))

    # Exact call count must be 1
    assert load_call_count == 1
    # All threads received the exact same loaded model and processor
    for m, p in results:
        assert m is mock_model
        assert p is mock_processor
    assert extractor._model is mock_model
    assert extractor._processor is mock_processor


def test_ocr_text_adapter_concurrent_initialization():
    """
    HIGH-1: Verify concurrent first access to OCRTextAdapter._load_ocr() and _load_text()
    executes underlying model loads exactly once.
    """
    adapter = OCRTextAdapter(
        ocr_model="zai-org/GLM-OCR",
        text_model="Qwen/Qwen3-4B-Instruct-2507",
    )
    assert adapter._ocr is None
    assert adapter._txt is None

    ocr_call_count = 0
    txt_call_count = 0
    call_lock = threading.Lock()

    mock_ocr_model = MagicMock()
    mock_ocr_proc = MagicMock()
    mock_txt_model = MagicMock()
    mock_txt_tok = MagicMock()

    def mock_load_ocr(*args, **kwargs):
        nonlocal ocr_call_count
        with call_lock:
            ocr_call_count += 1
        time.sleep(0.01)
        return mock_ocr_model

    def mock_load_txt(*args, **kwargs):
        nonlocal txt_call_count
        with call_lock:
            txt_call_count += 1
        time.sleep(0.01)
        return mock_txt_model

    fake_transformers = MagicMock()
    fake_transformers.AutoProcessor.from_pretrained.return_value = mock_ocr_proc
    fake_transformers.AutoModelForImageTextToText.from_pretrained.side_effect = mock_load_ocr
    fake_transformers.AutoTokenizer.from_pretrained.return_value = mock_txt_tok
    fake_transformers.AutoModelForCausalLM.from_pretrained.side_effect = mock_load_txt

    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False

    with patch.dict("sys.modules", {"transformers": fake_transformers, "torch": fake_torch}):
        def worker_ocr():
            return adapter._load_ocr()

        def worker_txt():
            return adapter._load_text()

        with ThreadPoolExecutor(max_workers=8) as pool:
            ocr_results = list(pool.map(lambda _: worker_ocr(), range(16)))
            txt_results = list(pool.map(lambda _: worker_txt(), range(16)))

    assert ocr_call_count == 1
    assert txt_call_count == 1
    for m, p in ocr_results:
        assert m is mock_ocr_model
        assert p is mock_ocr_proc
    for m, t in txt_results:
        assert m is mock_txt_model
        assert t is mock_txt_tok


def test_thread_safe_routed_extractor_adapter_caching_concurrency():
    """
    HIGH-1 / LOW: Verify concurrent calls to get_adapter for an OCR-text route
    create and cache the OCRTextAdapter instance exactly once even when initializations overlap.
    """
    extractor = ThreadSafeRoutedExtractor(allow_fallback=False)
    route = {"adapter": "ocr_text", "ocr_model": "test-ocr-model", "text_model": "test-text-model"}

    orig_init = OCRTextAdapter.__init__
    init_call_count = 0
    init_lock = threading.Lock()

    def slow_init(self, *args, **kwargs):
        nonlocal init_call_count
        with init_lock:
            init_call_count += 1
        time.sleep(0.02)
        orig_init(self, *args, **kwargs)

    with patch.object(OCRTextAdapter, "__init__", slow_init):
        with ThreadPoolExecutor(max_workers=8) as pool:
            adapters = list(pool.map(lambda _: extractor.get_adapter(route), range(16)))

    assert init_call_count == 1
    first = adapters[0]
    assert all(a is first for a in adapters)
    assert len(extractor._cache) == 1


def test_ocr_model_publication_order_and_retry_on_failure():
    """
    MEDIUM: Verify OCRTextAdapter._load_ocr loads processor and model, calls .eval(),
    and publishes the model reference last. Also verify failed .eval() leaves no
    partial state and allows safe retry.
    """
    adapter = OCRTextAdapter(ocr_model="test-ocr", text_model="test-text")
    assert adapter._ocr is None
    assert adapter._ocr_proc is None

    mock_proc = MagicMock()
    mock_model = MagicMock()
    eval_started = threading.Event()
    allow_eval = threading.Event()

    def blocked_eval():
        eval_started.set()
        allow_eval.wait(timeout=2.0)
        return mock_model

    mock_model.eval.side_effect = blocked_eval

    fake_transformers = MagicMock()
    fake_transformers.AutoProcessor.from_pretrained.return_value = mock_proc
    fake_transformers.AutoModelForImageTextToText.from_pretrained.return_value = mock_model
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False

    with patch.dict("sys.modules", {"transformers": fake_transformers, "torch": fake_torch}):
        # 1. Run _load_ocr in a background thread
        res_container = []

        def run_load():
            res_container.append(adapter._load_ocr())

        t = threading.Thread(target=run_load)
        t.start()

        assert eval_started.wait(timeout=2.0)
        # While eval is running, adapter._ocr must still be None (published last)
        assert adapter._ocr is None
        # Allow eval to complete
        allow_eval.set()
        t.join(timeout=2.0)

        assert adapter._ocr is mock_model
        assert adapter._ocr_proc is mock_proc
        assert res_container[0] == (mock_model, mock_proc)

    # 2. Test failure during eval leaves no partial state and allows retry
    adapter_fail = OCRTextAdapter(ocr_model="test-ocr", text_model="test-text")
    failing_model = MagicMock()
    failing_model.eval.side_effect = RuntimeError("eval failed")
    succeeding_model = MagicMock()
    succeeding_model.eval.return_value = succeeding_model

    fake_transformers.AutoModelForImageTextToText.from_pretrained.side_effect = [failing_model, succeeding_model]

    with patch.dict("sys.modules", {"transformers": fake_transformers, "torch": fake_torch}):
        with pytest.raises(RuntimeError, match="eval failed"):
            adapter_fail._load_ocr()

        # Must leave no partial state on failure
        assert adapter_fail._ocr is None
        assert adapter_fail._ocr_proc is None

        # Retry should succeed
        m, p = adapter_fail._load_ocr()
        assert m is succeeding_model
        assert p is mock_proc
        assert adapter_fail._ocr is succeeding_model
        assert adapter_fail._ocr_proc is mock_proc


def test_text_model_publication_order_and_retry_on_failure():
    """
    MEDIUM: Verify OCRTextAdapter._load_text loads tokenizer and model, calls .eval(),
    and publishes the model reference last. Also verify failed .eval() leaves no
    partial state and allows safe retry.
    """
    adapter = OCRTextAdapter(ocr_model="test-ocr", text_model="test-text")
    assert adapter._txt is None
    assert adapter._tok is None

    mock_tok = MagicMock()
    mock_model = MagicMock()
    eval_started = threading.Event()
    allow_eval = threading.Event()

    def blocked_eval():
        eval_started.set()
        allow_eval.wait(timeout=2.0)
        return mock_model

    mock_model.eval.side_effect = blocked_eval

    fake_transformers = MagicMock()
    fake_transformers.AutoTokenizer.from_pretrained.return_value = mock_tok
    fake_transformers.AutoModelForCausalLM.from_pretrained.return_value = mock_model
    fake_torch = MagicMock()
    fake_torch.cuda.is_available.return_value = False

    with patch.dict("sys.modules", {"transformers": fake_transformers, "torch": fake_torch}):
        # 1. Run _load_text in a background thread
        res_container = []

        def run_load():
            res_container.append(adapter._load_text())

        t = threading.Thread(target=run_load)
        t.start()

        assert eval_started.wait(timeout=2.0)
        # While eval is running, adapter._txt must still be None (published last)
        assert adapter._txt is None
        # Allow eval to complete
        allow_eval.set()
        t.join(timeout=2.0)

        assert adapter._txt is mock_model
        assert adapter._tok is mock_tok
        assert res_container[0] == (mock_model, mock_tok)

    # 2. Test failure during eval leaves no partial state and allows retry
    adapter_fail = OCRTextAdapter(ocr_model="test-ocr", text_model="test-text")
    failing_model = MagicMock()
    failing_model.eval.side_effect = RuntimeError("eval failed")
    succeeding_model = MagicMock()
    succeeding_model.eval.return_value = succeeding_model

    fake_transformers.AutoModelForCausalLM.from_pretrained.side_effect = [failing_model, succeeding_model]

    with patch.dict("sys.modules", {"transformers": fake_transformers, "torch": fake_torch}):
        with pytest.raises(RuntimeError, match="eval failed"):
            adapter_fail._load_text()

        # Must leave no partial state on failure
        assert adapter_fail._txt is None
        assert adapter_fail._tok is None

        # Retry should succeed
        m, tok = adapter_fail._load_text()
        assert m is succeeding_model
        assert tok is mock_tok
        assert adapter_fail._txt is succeeding_model
        assert adapter_fail._tok is mock_tok


def test_vlm_model_publication_order():
    """
    LOW: Verify SchemaGuidedExtractor._load_model publishes _processor,
    _image_config, and runtime_info before the _model sentinel reference.
    """
    extractor = SchemaGuidedExtractor(model_name="Qwen/Qwen2.5-VL-7B-Instruct")
    assert extractor._model is None
    assert extractor._processor is None

    mock_model = MagicMock()
    mock_processor = MagicMock()
    mock_img_cfg = vlm_runtime.ImageConfig(
        image_patch_size=16, merge_size=2, min_pixels=100, max_pixels=1000
    )
    mock_runtime_info = {"device": "cpu"}

    loaded_obj = vlm_runtime.LoadedVLM(
        model=mock_model,
        processor=mock_processor,
        family=vlm_runtime.FAMILY_QWEN2_5_VL,
        image_config=mock_img_cfg,
        runtime_info=mock_runtime_info,
    )

    publication_order = []
    orig_setattr = object.__setattr__

    def tracking_setattr(obj, name, value):
        if obj is extractor and name in ("_model", "_processor", "_image_config", "runtime_info"):
            publication_order.append(name)
        orig_setattr(obj, name, value)

    with patch.object(vlm_runtime, "load_qwen_vl", return_value=loaded_obj):
        with patch.object(SchemaGuidedExtractor, "__setattr__", tracking_setattr):
            m, p = extractor._load_model()

    assert m is mock_model
    assert p is mock_processor
    # Verify _model is published after processor, image_config, and runtime_info
    assert publication_order == ["_processor", "_image_config", "runtime_info", "_model"]


# ============================================================================
# 10. Application-Wide Shared Idempotency Store (HIGH-2)
# ============================================================================

def test_application_wide_shared_idempotency_store_across_eviction_and_tenants(tmp_path: Path):
    """
    HIGH-2: Verify application-scoped LocalIdempotencyStore shares locks across
    cache eviction and recreation of Tenant A while isolating Tenant B.
    """
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings, tenant_cache_size=2)

    # 1. Create Tenant A components
    comp_a1 = container.get_tenant_components("tenant-a")
    assert comp_a1.idempotency_store is container.idempotency_store

    # 2. Acquire a key lock for a specific idempotency key
    lock_a1 = comp_a1.idempotency_store.get_key_lock("tenant-a", "key-shared-123")
    assert hasattr(lock_a1, "acquire") and hasattr(lock_a1, "release")

    # 3. Evict Tenant A through the bounded cache by adding tenant-b and tenant-c
    comp_b = container.get_tenant_components("tenant-b")
    comp_c = container.get_tenant_components("tenant-c")
    assert len(container.tenant_cache) == 2
    assert not container.tenant_cache.contains("tenant-a")

    # 4. Recreate Tenant A
    comp_a2 = container.get_tenant_components("tenant-a")
    assert container.tenant_cache.contains("tenant-a")

    # 5. Confirm that the new components use the same idempotency store and the same key lock
    assert comp_a2.idempotency_store is container.idempotency_store
    assert comp_a2.idempotency_store is comp_a1.idempotency_store
    lock_a2 = comp_a2.idempotency_store.get_key_lock("tenant-a", "key-shared-123")
    assert lock_a2 is lock_a1  # Exact same RLock object identity

    # 6. Confirm that another tenant remains isolated
    assert comp_b.idempotency_store is container.idempotency_store
    lock_b = comp_b.idempotency_store.get_key_lock("tenant-b", "key-shared-123")
    assert lock_b is not lock_a1


# ============================================================================
# 10. No Tenant State Leaks Across Requests
# ============================================================================

def test_tenant_storage_directory_isolation(tmp_path: Path):
    """Verify storage paths are strictly separated by tenant identifier."""
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings)

    comp_a = container.get_tenant_components("tenant-one")
    comp_b = container.get_tenant_components("tenant-two")

    dir_a = comp_a.storage._tenant_dir(create=True)
    dir_b = comp_b.storage._tenant_dir(create=True)

    assert dir_a != dir_b
    assert "tenant-one" in str(dir_a)
    assert "tenant-two" in str(dir_b)


def test_document_service_rejects_cross_tenant_requests():
    """Verify DocumentService rejects processing requests for another tenant."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    doc_service_a = container.get_document_service("tenant-a")

    request_b = DocumentProcessingRequest(
        file_path_or_stream=Path("/tmp/fake.pdf"),
        tenant_id="tenant-b",
    )

    with pytest.raises(TenantMismatchError, match="Tenant mismatch: request tenant does not match service tenant"):
        doc_service_a.process_document(request_b)

    with pytest.raises(TenantMismatchError, match="Tenant mismatch: request tenant does not match service tenant"):
        list(doc_service_a.process_document_stream(request_b))


def test_document_service_rejects_mismatched_processing_service():
    """Verify DocumentService fails closed if injected processing service belongs to another tenant."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    comp_a = container.get_tenant_components("tenant-a")

    with pytest.raises(TenantMismatchError, match="Tenant mismatch: processing service belongs to another tenant"):
        DocumentService(processing_service=comp_a.processing_service, tenant_id="tenant-b")


def test_invalid_tenant_identifiers_rejected():
    """Verify invalid or dangerous tenant IDs fail closed immediately."""
    settings = ApiSettings(app_env="development")
    container = ApplicationContainer(api_settings=settings)

    for invalid in ("", "   ", None, 123, "../traversal", "bad/slash", "tenant\\backslash"):
        with pytest.raises(InvalidTenantIdentifierError):
            container.get_tenant_components(invalid)  # type: ignore

        with pytest.raises(InvalidTenantIdentifierError):
            container.get_document_service(invalid)  # type: ignore


# ============================================================================
# 11. FastAPI Dependency Injection Providers
# ============================================================================

def test_fastapi_dependency_injection_flow():
    """Verify end-to-end FastAPI dependency injection with authenticated tenant context."""
    settings = ApiSettings(app_env="development")
    auth_provider = MockAuthProvider()
    container = ApplicationContainer(api_settings=settings, auth_provider=auth_provider)

    app = create_app(settings=settings, container=container)

    @app.get("/test/probe-container")
    async def probe_container(c: ApplicationContainer = Depends(deps.get_application_container)):
        return {"ok": True, "is_container": isinstance(c, ApplicationContainer)}

    @app.get("/test/probe-tenant")
    async def probe_tenant(
        components: TenantComponents = Depends(deps.get_tenant_components),
        doc_service: DocumentService = Depends(deps.get_document_service),
        proc_service: DocumentProcessingService = Depends(deps.get_document_processing_service),
    ):
        return {
            "tenant_id": components.tenant_id,
            "service_tenant_id": doc_service.tenant_id,
            "proc_tenant_id": proc_service.tenant_id,
        }

    client = TestClient(app)

    # 1. Unauthenticated request to probe-container succeeds because container does not require auth
    resp = client.get("/test/probe-container")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "is_container": True}

    # 2. Unauthenticated request to tenant endpoint must be rejected with 401
    resp_unauth = client.get("/test/probe-tenant")
    assert resp_unauth.status_code == 401
    assert resp_unauth.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"

    # 3. Authenticated request for tenant-omega resolves matching tenant-bound dependencies
    resp_omega = client.get(
        "/test/probe-tenant",
        headers={"Authorization": "Bearer valid_tenant-omega"},
    )
    assert resp_omega.status_code == 200
    data = resp_omega.json()
    assert data["tenant_id"] == "tenant-omega"
    assert data["service_tenant_id"] == "tenant-omega"
    assert data["proc_tenant_id"] == "tenant-omega"

    # 4. Authenticated request for tenant-psi resolves isolated tenant-psi dependencies
    resp_psi = client.get(
        "/test/probe-tenant",
        headers={"Authorization": "Bearer valid_tenant-psi"},
    )
    assert resp_psi.status_code == 200
    assert resp_psi.json()["tenant_id"] == "tenant-psi"


def test_dependency_unconfigured_container_raises_503():
    """Verify get_application_container raises PortNotConfiguredError when container is None."""
    request = MagicMock(spec=Request)
    request.app.state.container = None

    with pytest.raises(PortNotConfiguredError) as exc:
        deps.get_application_container(request)
    assert exc.value.port == "ApplicationContainer"


def test_typed_503_on_shutdown_container():
    """MEDIUM-1: Verify requests to endpoints using an unavailable or shut-down container produce typed 503 responses."""
    settings = ApiSettings(app_env="development")
    auth_provider = MockAuthProvider()
    container = ApplicationContainer(api_settings=settings, auth_provider=auth_provider)
    app = create_app(settings=settings, container=container)

    @app.get("/test/probe-503")
    async def probe(doc_service: DocumentService = Depends(deps.get_document_service)):
        return {"ok": True}

    client = TestClient(app)

    # Shut down the container
    container.shutdown()

    # Request must fail with typed 503 SERVICE_UNAVAILABLE
    response = client.get(
        "/test/probe-503",
        headers={"Authorization": "Bearer valid_tenant-503"},
    )
    assert response.status_code == 503
    data = response.json()
    assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert data["error"]["details"] == {"port": "ApplicationContainer"}


def test_tenant_creation_does_not_reset_global_logging(tmp_path: Path):
    """
    MEDIUM-3: Verify that creating a tenant's Module A pipeline preserves
    externally registered logger handlers on doc_extractor.
    """
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings)

    logger = logging.getLogger("doc_extractor")
    custom_handler = logging.NullHandler()
    logger.addHandler(custom_handler)
    try:
        assert custom_handler in logger.handlers

        # Create tenant components (which constructs ModuleAPipeline)
        comp = container.get_tenant_components("tenant-log-check")
        assert comp.tenant_id == "tenant-log-check"

        # Confirm that custom handler remains registered and was not cleared
        assert custom_handler in logger.handlers
    finally:
        logger.removeHandler(custom_handler)


def test_logging_setup_idempotent_across_lifespans():
    """
    MEDIUM: Verify repeated FastAPI application lifespan startup calls setup_logging
    idempotently without duplicating StreamHandlers or removing externally registered handlers.
    """
    from doc_extractor.logging import SafeJsonFormatter

    logger = logging.getLogger("doc_extractor")
    external_handler = logging.NullHandler()
    logger.addHandler(external_handler)

    try:
        assert external_handler in logger.handlers

        settings = ApiSettings(app_env="development")
        app = create_app(settings)

        # Run through 3 separate lifespan contexts
        for _ in range(3):
            with TestClient(app) as client:
                resp = client.get("/health")
                assert resp.status_code == 200

        # External handler must still be present
        assert external_handler in logger.handlers

        # Number of handlers with SafeJsonFormatter must be exactly 1
        json_handlers = [
            h for h in logger.handlers
            if isinstance(getattr(h, "formatter", None), SafeJsonFormatter)
        ]
        assert len(json_handlers) == 1
    finally:
        logger.removeHandler(external_handler)
