"""
Focused unit and integration tests for DocumentProcessingService.

Covers:
- Successful end-to-end orchestration.
- Module A rejection and processing failures.
- Module B handoff failures.
- Per-page extraction failures and partial coverage resilience.
- Module C canonical envelope assembly.
- Explicit tenant and actor context propagation.
- Progress event reporting via stream and callback.
- Page rotation handling.
- Max pages cap with PAGES_NOT_PROCESSED flag.
- Convenience invocation with kwargs.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import stat
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

from PIL import Image
import pytest

from doc_extractor.config import Settings
from doc_extractor.contracts_b import (
    ClassificationOutput,
    ConfidenceProvenance,
    MVPClassificationDomain,
    MVPDocumentType,
    ModuleAPageRef,
    ModuleBOutputPayload,
    PageLayoutOutput,
)
from doc_extractor.contracts_c import (
    CanonicalExtractionEnvelope,
    DocumentMetadata,
    ExecutionMode,
    ExecutionProvenance,
    ExtractedField,
    FlagItem,
    PageImageRef,
    RunMetadata,
)
from doc_extractor.exceptions import (
    DocumentExtractorError,
    InvalidTenantIdentifierError,
    StorageError,
    TenantMismatchError,
    UnsupportedFileError,
)
from doc_extractor.handoff import ModuleAHandoffPayload, PageHandoffRecord
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.pipeline_b import ModuleBPipeline, ReviewerOverrideRequest
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.processing_service import (
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentProcessingService,
    ProcessingProgressEvent,
    RequestTemporaryWorkspace,
    _rotate_page,
    resolve_engine_choice,
)
from doc_extractor.routed_pages import RoutedPageResult
from doc_extractor.routing import DocumentCategory, RoutedExtractor
from doc_extractor.storage import LocalDiskStorage


def _make_dummy_image(path: Path) -> Path:
    im = Image.new("RGB", (100, 100), color="white")
    im.save(path)
    return path


def _dummy_envelope(doc_id: str = "doc_123", status: str = "needs_review") -> CanonicalExtractionEnvelope:
    return CanonicalExtractionEnvelope(
        document_id=doc_id,
        document_metadata=DocumentMetadata(
            document_type="generic",
            schema_id="generic",
            schema_version="1.0.0",
            classification_confidence=float(0),
            status=status,
            page_count=1,
            document_category="structured",
        ),
        fields={
            "full_name": ExtractedField(
                value="John Doe",
                confidence=None,
                evidence=None,
            )
        },
        flags=[],
        run=RunMetadata(
            run_id="run_test",
            model_versions={"normalizer": "deterministic-v1"},
            provenance=ExecutionProvenance(
                mode=ExecutionMode.HEURISTIC,
                model_name="test_model",
            ),
        ),
    )


def _setup_mock_service(tmp_path: Path, tenant_id: str = "tenant_test", actor_id: str = "actor_test"):
    img1 = _make_dummy_image(tmp_path / "page_1.png")
    img2 = _make_dummy_image(tmp_path / "page_2.png")

    mock_storage = MagicMock()
    mock_storage.tenant_id = tenant_id
    mock_storage.resolve_uri_to_path.side_effect = lambda uri: (
        img1 if "_1" in uri else img2
    )

    # Module A mock
    mock_module_a = MagicMock(spec=ModuleAPipeline)
    mock_module_a.tenant_id = tenant_id
    mock_module_a.storage = mock_storage
    mock_module_a.renderer = MagicMock()
    mock_module_a.renderer.storage = mock_storage
    mock_module_a.preprocessor = MagicMock()
    mock_module_a.preprocessor.storage = mock_storage
    mock_module_a.ingestion_service = MagicMock()
    mock_module_a.ingestion_service.tenant_id = tenant_id
    mock_module_a.ingestion_service.storage = mock_storage
    mock_module_a.process_document.return_value = ModuleAHandoffPayload(
        contract_version="1.0.0",
        run_id="run_a_123",
        document_id="doc_123",
        tenant_id=tenant_id,
        status="processing",
        internal_state="rendered",
        claimed_filename="test.pdf",
        file_hash_sha256="a" * 64,
        file_size_bytes=1024,
        page_count=2,
        mime_type="application/pdf",
        pages=[
            PageHandoffRecord(
                page_number=1,
                original_artifact_id="art_orig_1",
                preprocessed_artifact_id="art_prep_1",
                original_storage_uri="storage://tenant_test/docs/doc_123/orig_1.png",
                preprocessed_storage_uri="storage://tenant_test/docs/doc_123/prep_1.png",
                original_width_px=100,
                original_height_px=100,
                preprocessed_width_px=100,
                preprocessed_height_px=100,
                dpi=200,
                applied_rotation_deg=0,
                applied_deskew_deg=0.0,
                residual_skew_deg=0.0,
                contrast_method="none",
                metadata_storage_uri="storage://tenant_test/docs/doc_123/meta_1.json",
            ),
            PageHandoffRecord(
                page_number=2,
                original_artifact_id="art_orig_2",
                preprocessed_artifact_id="art_prep_2",
                original_storage_uri="storage://tenant_test/docs/doc_123/orig_2.png",
                preprocessed_storage_uri="storage://tenant_test/docs/doc_123/prep_2.png",
                original_width_px=100,
                original_height_px=100,
                preprocessed_width_px=100,
                preprocessed_height_px=100,
                dpi=200,
                applied_rotation_deg=0,
                applied_deskew_deg=0.0,
                residual_skew_deg=0.0,
                contrast_method="none",
                metadata_storage_uri="storage://tenant_test/docs/doc_123/meta_2.json",
            ),
        ],
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    handoff_dict_template = mock_module_a.process_document.return_value.to_dict()
    mock_storage.get_handoff_payload.side_effect = lambda doc_id: (
        deepcopy(handoff_dict_template) if doc_id == "doc_123" else None
    )

    # Module B mock
    mock_module_b = MagicMock(spec=ModuleBPipeline)
    mock_module_b.tenant_id = tenant_id
    mock_module_b.storage = mock_storage
    mock_module_b.loader = MagicMock()
    mock_module_b.loader.storage = mock_storage
    p1_ref = ModuleAPageRef(
        page_number=1,
        original_artifact_id="art_orig_1",
        preprocessed_artifact_id="art_prep_1",
        original_storage_uri="storage://tenant_test/docs/doc_123/orig_1.png",
        preprocessed_storage_uri="storage://tenant_test/docs/doc_123/prep_1.png",
        original_width_px=100,
        original_height_px=100,
        preprocessed_width_px=100,
        preprocessed_height_px=100,
        metadata_storage_uri="storage://tenant_test/docs/doc_123/meta_1.json",
    )
    p2_ref = ModuleAPageRef(
        page_number=2,
        original_artifact_id="art_orig_2",
        preprocessed_artifact_id="art_prep_2",
        original_storage_uri="storage://tenant_test/docs/doc_123/orig_2.png",
        preprocessed_storage_uri="storage://tenant_test/docs/doc_123/prep_2.png",
        original_width_px=100,
        original_height_px=100,
        preprocessed_width_px=100,
        preprocessed_height_px=100,
        metadata_storage_uri="storage://tenant_test/docs/doc_123/meta_2.json",
    )
    mock_module_b.load_page_images.return_value = [
        PageImageRef(page_number=1, image_path=img1, page_ref=p1_ref),
        PageImageRef(page_number=2, image_path=img2, page_ref=p2_ref),
    ]
    mock_module_b.process_handoff.return_value = ModuleBOutputPayload(
        contract_version="1.0.0",
        run_id="run_b_123",
        document_id="doc_123",
        tenant_id=tenant_id,
        status="needs_review",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.5,
            confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
            review_required=True,
        ),
        pages=[
            PageLayoutOutput(page_number=1, markdown_content="# Page 1"),
            PageLayoutOutput(page_number=2, markdown_content="# Page 2"),
        ],
        combined_markdown="# Page 1\n\n# Page 2",
        parser_model_name="test_parser",
        classifier_model_name="test_classifier",
        processing_time_ms=50.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    # RoutedExtractor mock
    mock_routed = MagicMock(spec=RoutedExtractor)
    mock_routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 0,
        "router_model": "test_router",
        "filled_by": None,
    }
    mock_routed.extract.return_value = (
        {"full_name": {"value": "John Doe", "bbox": [10, 10, 50, 20]}},
        ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
        {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
    )

    # Module C mock
    mock_module_c = MagicMock(spec=ModuleCPipeline)
    mock_module_c.process_routed_pages.return_value = _dummy_envelope("doc_123")

    service = DocumentProcessingService(
        module_a=mock_module_a,
        module_b=mock_module_b,
        module_c=mock_module_c,
        routed_extractor=mock_routed,
        storage=mock_storage,
        tenant_id=tenant_id,
        actor_id=actor_id,
    )

    return service, mock_module_a, mock_module_b, mock_routed, mock_module_c, img1


def test_successful_end_to_end_orchestration(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="document.pdf",
        tenant_id="tenant_test",
        actor_id="actor_test",
    )

    result = service.process_document(req)

    assert result.is_success is True
    assert result.document_id == "doc_123"
    assert result.tenant_id == "tenant_test"
    assert result.status == "needs_review"
    assert result.envelope is not None
    assert len(result.page_results) == 2
    assert len(result.pages_info) == 2
    assert mod_a.process_document.called
    assert mod_b.process_handoff.called
    assert routed.extract.call_count == 2
    assert mod_c.process_routed_pages.called


def test_module_a_rejection_returns_rejected_result(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    mod_a.process_document.return_value = ModuleAHandoffPayload(
        contract_version="1.0.0",
        run_id="run_a_rej",
        document_id="doc_rej",
        tenant_id="tenant_test",
        status="rejected",
        internal_state="rejected_malware",
        claimed_filename="malware.exe",
        file_hash_sha256="b" * 64,
        file_size_bytes=500,
        page_count=0,
        mime_type="application/octet-stream",
        pages=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "MALWARE_DETECTED", "message": "EICAR test signature found."},
    )

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="malware.exe",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)

    assert result.is_rejected is True
    assert result.status == "rejected"
    assert result.envelope is None
    assert result.error == {"code": "MALWARE_DETECTED", "message": "EICAR test signature found."}
    assert not mod_b.process_handoff.called
    assert not routed.extract.called
    assert not mod_c.process_routed_pages.called


def test_module_a_processing_exception_returns_failed_result(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    mod_a.process_document.side_effect = IOError("Corrupt file input stream")

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="corrupt.pdf",
        tenant_id="tenant_test",
    )

    diagnostics = {}
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))

    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error["code"] == "INGESTION_ERROR"
    assert result.error["message"] == "Document ingestion and preprocessing failed."
    assert "Corrupt file input stream" in diagnostics.get("split", "")


def test_module_b_handoff_failure_returns_failed_result(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    mod_b.process_handoff.return_value = ModuleBOutputPayload(
        contract_version="1.0.0",
        run_id="run_b_err",
        document_id="doc_123",
        tenant_id="tenant_test",
        status="failed",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.0,
            confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
            review_required=True,
        ),
        pages=[],
        combined_markdown="",
        parser_model_name="test_parser",
        classifier_model_name="test_classifier",
        processing_time_ms=10.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "PARSER_FAILURE", "message": "Page layout engine crashed."},
    )

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)

    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error == {
        "code": "MODULE_B_ERROR",
        "message": "Document page classification and layout analysis failed.",
    }
    assert result.payload_b.error == {
        "code": "MODULE_B_ERROR",
        "message": "Document page classification and layout analysis failed.",
    }
    assert not routed.extract.called
    assert not mod_c.process_routed_pages.called


def test_per_page_extraction_failure_survives_with_partial_results(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    extract_calls = []

    def extract_side_effect(img_path, category, language, schema_cls, subtype, engine):
        extract_calls.append(img_path)
        if len(extract_calls) == 2:
            raise RuntimeError("VLM worker out of memory")
        return (
            {"full_name": {"value": "John Doe", "bbox": [10, 10, 50, 20]}},
            ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
            {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
        )

    routed.extract.side_effect = extract_side_effect

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="multipage.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)

    assert result.is_success is True
    assert len(result.page_results) == 2
    assert result.page_results[0].error is None
    assert result.page_results[1].error == "RuntimeError: Extraction failed for page 2."
    # Module C receives both pages so it can record PARTIAL_PAGE_COVERAGE
    assert mod_c.process_routed_pages.called


def test_explicit_tenant_and_actor_context_propagation(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(
        tmp_path, tenant_id="tenant_alpha", actor_id="analyst_1"
    )

    # When doc_type is explicitly selected, apply_reviewer_override should record actor and tenant
    mod_b.apply_reviewer_override.return_value = mod_b.process_handoff.return_value

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc.pdf",
        tenant_id="tenant_alpha",
        actor_id="analyst_1",
        category_choice="Structured",
        doc_type_choice="DOC-01",
    )

    result = service.process_document(req)
    assert result.is_success is True
    assert mod_b.apply_reviewer_override.called

    override_req: ReviewerOverrideRequest = mod_b.apply_reviewer_override.call_args[0][1]
    assert override_req.tenant_id == "tenant_alpha"
    assert override_req.reviewer_id == "analyst_1"
    assert override_req.new_document_type == MVPDocumentType.DOC_01_CASA


def test_tenant_mismatch_raises_validation_error(tmp_path):
    service, _, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_isolated")

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc.pdf",
        tenant_id="tenant_intruder",
    )

    with pytest.raises(ValueError, match="Tenant mismatch"):
        service.process_document(req)


def test_progress_event_reporting_stream_and_callback(tmp_path):
    service, _, _, _, _, img1 = _setup_mock_service(tmp_path)

    events: List[ProcessingProgressEvent] = []

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="test.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req, progress_callback=events.append)

    assert result.is_success is True
    keys = [e.step_key for e in events]
    assert "upload" in keys
    assert "split" in keys
    assert "cls" in keys
    assert "ext1" in keys
    assert "ext2" in keys
    assert "merge" in keys

    # Verify final event carries the result
    final_event = events[-1]
    assert final_event.step_key == "merge"
    assert final_event.state == "done"
    assert final_event.result is not None
    assert final_event.result.document_id == "doc_123"


def test_page_rotation_handling(tmp_path):
    service, _, _, routed, _, img1 = _setup_mock_service(tmp_path)

    # Router detects 90-degree clockwise rotation
    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="sideways.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)
    assert result.is_success is True
    # Verify rotation was passed to page results for Module C reverse mapping
    assert all(r.rotation == 90 for r in result.page_results)


def test_max_pages_limit_applies_flag(tmp_path):
    service, _, _, _, mod_c, img1 = _setup_mock_service(tmp_path)

    # Document has 2 pages in mock, but max_pages is 1
    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="long.pdf",
        tenant_id="tenant_test",
        max_pages=1,
    )

    result = service.process_document(req)
    assert result.is_success is True
    assert len(result.page_results) == 1
    assert any(f.code == "PAGES_NOT_PROCESSED" for f in result.envelope.flags)


def test_convenience_invocation_with_direct_arguments(tmp_path):
    service, _, _, _, _, img1 = _setup_mock_service(tmp_path)

    result = service.process_document(
        img1,
        claimed_filename="direct.png",
        tenant_id="tenant_test",
        actor_id="direct_actor",
    )

    assert result.is_success is True
    assert result.document_id == "doc_123"
    assert result.tenant_id == "tenant_test"


def test_valid_explicit_tenant(tmp_path):
    img = _make_dummy_image(tmp_path / "valid.png")
    req = DocumentProcessingRequest(
        file_path_or_stream=img,
        tenant_id="tenant_valid",
    )
    assert req.tenant_id == "tenant_valid"


def test_missing_tenant_constructor_argument_raises_type_error(tmp_path):
    img = _make_dummy_image(tmp_path / "missing.png")
    with pytest.raises(TypeError):
        DocumentProcessingRequest(file_path_or_stream=img)  # type: ignore[call-arg]


def test_explicit_none_tenant_raises_validation_error(tmp_path):
    img = _make_dummy_image(tmp_path / "none.png")
    with pytest.raises((ValueError, InvalidTenantIdentifierError)):
        DocumentProcessingRequest(file_path_or_stream=img, tenant_id=None)  # type: ignore[arg-type]


def test_empty_tenant_raises_validation_error(tmp_path):
    img = _make_dummy_image(tmp_path / "empty.png")
    with pytest.raises((ValueError, InvalidTenantIdentifierError)):
        DocumentProcessingRequest(file_path_or_stream=img, tenant_id="")


def test_whitespace_only_tenant_raises_validation_error(tmp_path):
    img = _make_dummy_image(tmp_path / "ws.png")
    with pytest.raises((ValueError, InvalidTenantIdentifierError)):
        DocumentProcessingRequest(file_path_or_stream=img, tenant_id="   ")


def test_request_tenant_conflicting_with_tenant_bound_service(tmp_path):
    service, _, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_bound")
    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        tenant_id="tenant_other",
    )
    with pytest.raises(ValueError, match="Tenant mismatch"):
        service.process_document(req)


def test_successful_explicit_tenant_propagation_into_module_a_and_storage(tmp_path):
    service, mod_a, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_propagation")
    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="propagation.pdf",
        tenant_id="tenant_propagation",
    )
    result = service.process_document(req)
    assert result.is_success is True
    assert result.tenant_id == "tenant_propagation"
    assert mod_a.tenant_id == "tenant_propagation"
    assert result.payload_a.tenant_id == "tenant_propagation"


def test_gradio_adapter_explicitly_supplies_development_tenant():
    from doc_extractor.app import TENANT_ID
    assert TENANT_ID == "test_tenant"

    import inspect
    import doc_extractor.app as app_mod
    src = inspect.getsource(app_mod._process_core)
    assert "tenant_id=TENANT_ID" in src
    assert "effective_tenant" not in src
    assert "default_tenant" not in src


def test_shared_service_without_preset_tenant_accepts_per_request_tenant(tmp_path):
    service, mod_a, mod_b, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_one")
    service.tenant_id = None

    req1 = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_one")
    res1 = service.process_document(req1)
    assert res1.is_success is True

    # Rebind mock components for tenant_two
    service.storage.tenant_id = "tenant_two"
    mod_a.tenant_id = "tenant_two"
    mod_a.ingestion_service.tenant_id = "tenant_two"
    mod_b.tenant_id = "tenant_two"

    req2 = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_two")
    res2 = service.process_document(req2)
    assert res2.is_success is True

    assert service.tenant_id is None


def test_convenience_invocation_without_tenant_on_unbound_service_fails(tmp_path):
    service, _, _, _, _, img1 = _setup_mock_service(tmp_path)
    service.tenant_id = None

    with pytest.raises(ValueError, match="Tenant ID is required"):
        service.process_document(img1)


# =========================================================================
# F1 Regression Tests: Validate Tenant Ownership of Injected Components
# =========================================================================


def test_tenant_a_pipeline_with_tenant_b_request_rejected_with_no_writes(tmp_path):
    storage_root = tmp_path / "storage"
    storage_a = LocalDiskStorage(storage_root, tenant_id="tenant_a")
    mock_module_a = MagicMock(spec=ModuleAPipeline)
    mock_module_a.tenant_id = "tenant_a"
    mock_module_a.storage = storage_a

    service = DocumentProcessingService(module_a=mock_module_a, storage=storage_a)
    img = _make_dummy_image(tmp_path / "doc_b.png")

    req_b = DocumentProcessingRequest(file_path_or_stream=img, tenant_id="tenant_b")

    with pytest.raises(ValueError, match="Tenant mismatch: injected"):
        service.process_document(req_b)

    # Verify no processing or writes occurred
    assert not mock_module_a.process_document.called
    tenant_a_dir = storage_root / "tenant_a"
    tenant_b_dir = storage_root / "tenant_b"
    assert not tenant_b_dir.exists()
    # No documents written to tenant_a
    if tenant_a_dir.exists():
        assert list(tenant_a_dir.glob("docs/*")) == []


def test_tenant_a_storage_with_tenant_b_request_rejected_with_no_writes(tmp_path):
    storage_root = tmp_path / "storage"
    storage_a = LocalDiskStorage(storage_root, tenant_id="tenant_a")

    service = DocumentProcessingService(storage=storage_a)
    img = _make_dummy_image(tmp_path / "doc_b.png")

    req_b = DocumentProcessingRequest(file_path_or_stream=img, tenant_id="tenant_b")

    with pytest.raises(ValueError, match="Tenant mismatch: injected"):
        service.process_document(req_b)

    tenant_b_dir = storage_root / "tenant_b"
    assert not tenant_b_dir.exists()


def test_tenant_bound_service_with_mismatched_pipeline_fails_at_init():
    mock_module_a = MagicMock(spec=ModuleAPipeline)
    mock_module_a.tenant_id = "tenant_a"

    with pytest.raises(ValueError, match="Tenant mismatch: injected"):
        DocumentProcessingService(module_a=mock_module_a, tenant_id="tenant_b")


def test_tenant_bound_service_with_mismatched_storage_fails_at_init(tmp_path):
    storage_a = LocalDiskStorage(tmp_path, tenant_id="tenant_a")

    with pytest.raises(ValueError, match="Tenant mismatch: injected"):
        DocumentProcessingService(storage=storage_a, tenant_id="tenant_b")


def test_correct_tenant_owned_dependencies_succeed(tmp_path):
    service, _, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_owned")
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_owned")

    result = service.process_document(req)
    assert result.is_success is True
    assert result.tenant_id == "tenant_owned"


def test_shared_service_processing_different_tenants_creates_correct_dependencies(tmp_path):
    # Unbound shared service dynamically builds per-request components
    service = DocumentProcessingService(settings=Settings(storage_base_dir=tmp_path / "storage", app_env="development"))
    assert service.tenant_id is None
    assert service.storage is None
    assert service.module_a is None

    # Verify per-request resolution does not retain state
    img = _make_dummy_image(tmp_path / "input.png")
    req_1 = DocumentProcessingRequest(file_path_or_stream=img, tenant_id="tenant_dynamic_1")
    req_2 = DocumentProcessingRequest(file_path_or_stream=img, tenant_id="tenant_dynamic_2")

    tenant_1 = service._resolve_tenant(req_1)
    assert tenant_1 == "tenant_dynamic_1"
    tenant_2 = service._resolve_tenant(req_2)
    assert tenant_2 == "tenant_dynamic_2"

    assert service.tenant_id is None
    assert service.storage is None
    assert service.module_a is None


# =========================================================================
# F2 Regression Tests: Isolate Rotated-Page Temporary Files
# =========================================================================


def test_two_documents_with_identical_filenames_and_rotations_do_not_collide(tmp_path):
    img = _make_dummy_image(tmp_path / "shared_page.png")

    ws1 = RequestTemporaryWorkspace(prefix="test_ws1_")
    ws2 = RequestTemporaryWorkspace(prefix="test_ws2_")

    with ws1, ws2:
        dir1 = ws1.path
        dir2 = ws2.path
        assert dir1 != dir2
        rot1 = ws1.rotate_page(img, 90, page_no=1)
        rot2 = ws2.rotate_page(img, 90, page_no=1)

        # Different private paths, neither overwrites the other
        assert rot1 != rot2
        assert rot1.parent == dir1
        assert rot2.parent == dir2
        assert rot1.exists()
        assert rot2.exists()

    # Both workspaces cleaned up completely
    assert not dir1.exists()
    assert not dir2.exists()
    assert not ws1.has_path
    assert not ws2.has_path


def test_interleaved_temporary_workspaces_remain_isolated(tmp_path):
    img_a = _make_dummy_image(tmp_path / "page.png")

    ws_a = RequestTemporaryWorkspace()
    ws_b = RequestTemporaryWorkspace()

    ws_a.__enter__()
    try:
        p_a = ws_a.rotate_page(img_a, 180, page_no=1)
        assert p_a.exists()

        ws_b.__enter__()
        try:
            p_b = ws_b.rotate_page(img_a, 180, page_no=1)
            assert p_b.exists()
            assert p_a != p_b
        finally:
            ws_b.cleanup()

        # ws_a's artifact remains intact while ws_b was cleaned up
        assert p_a.exists()
        assert not ws_b.has_path
    finally:
        ws_a.cleanup()

    assert not ws_a.has_path


def test_successful_processing_cleans_up_temporary_workspace(tmp_path):
    service, _, _, routed, _, img1 = _setup_mock_service(tmp_path)

    # Rotation 90 to trigger temporary rotated file creation
    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    discovered_workspaces: List[Path] = []

    def mock_extract(img_path, category, language, schema_cls, subtype, engine):
        p = Path(img_path)
        discovered_workspaces.append(p.parent)
        assert p.parent.exists()
        return (
            {"full_name": {"value": "John Doe", "bbox": [10, 10, 50, 20]}},
            ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
            {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
        )

    routed.extract.side_effect = mock_extract

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc_rotated.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)
    assert result.is_success is True
    assert len(discovered_workspaces) > 0

    # Verify that the temporary workspace directory was deleted upon process completion
    for ws_dir in discovered_workspaces:
        assert not ws_dir.exists()


def test_extraction_failure_cleans_up_temporary_workspace(tmp_path):
    service, _, _, routed, _, img1 = _setup_mock_service(tmp_path)

    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    discovered_workspaces: List[Path] = []

    def failing_extract(img_path, category, language, schema_cls, subtype, engine):
        p = Path(img_path)
        discovered_workspaces.append(p.parent)
        raise RuntimeError("Worker process crashed")

    routed.extract.side_effect = failing_extract

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc_failed.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)
    assert len(discovered_workspaces) > 0

    # Workspace directory must be deleted even after extraction failure
    for ws_dir in discovered_workspaces:
        assert not ws_dir.exists()


def test_temporary_workspace_and_file_permissions(tmp_path):
    img = _make_dummy_image(tmp_path / "page.png")

    workspace = RequestTemporaryWorkspace()
    with workspace:
        ws_dir = workspace.path
        # Directory mode 0o700: only owner can read, write, traverse
        dir_mode = stat.S_IMODE(os.stat(ws_dir).st_mode)
        assert dir_mode == 0o700, f"Expected 0o700, got {oct(dir_mode)}"

        rot_file = workspace.rotate_page(img, 90, page_no=1)
        # File mode 0o600: only owner can read, write; not world-readable
        file_mode = stat.S_IMODE(os.stat(rot_file).st_mode)
        assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"

    assert not ws_dir.exists()


def test_original_page_image_preservation_and_geometry(tmp_path):
    service, _, _, routed, _, img1 = _setup_mock_service(tmp_path)

    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    original_bytes = img1.read_bytes()
    orig_hash = hashlib.sha256(original_bytes).hexdigest()

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc_unmodified.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)
    assert result.is_success is True

    # Original file bytes on disk are completely preserved
    assert img1.read_bytes() == original_bytes
    assert hashlib.sha256(img1.read_bytes()).hexdigest() == orig_hash

    # result.page_items holds the unrotated original/preprocessed page paths
    for page_no, page_path, orig_path in result.page_items:
        assert "upright" not in page_path.name
        if orig_path:
            assert "upright" not in orig_path.name


def test_extraction_error_does_not_expose_temporary_workspace_path(tmp_path):
    service, _, _, routed, _, img1 = _setup_mock_service(tmp_path)

    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    def extract_with_path(img_path, category, language, schema_cls, subtype, engine):
        raise RuntimeError(f"Error parsing image at {img_path}")

    routed.extract.side_effect = extract_with_path

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc_masked.pdf",
        tenant_id="tenant_test",
    )

    result = service.process_document(req)
    assert len(result.page_results) > 0
    err_msg = str(result.page_results[0].error)

    # Ensure internal workspace path and raw exception text are completely sanitized
    assert err_msg == "RuntimeError: Extraction failed for page 1."
    assert "doc_workspace_" not in err_msg
    assert "Error parsing image" not in err_msg


# =========================================================================
# Additional F1 / F2 Security & Robustness Regression Tests
# =========================================================================


def test_reject_non_string_missing_or_whitespace_tenant_id_on_components(tmp_path):
    invalid_tenants = [None, 123, b"tenant_raw", "", "   ", "\t\n"]

    for inv in invalid_tenants:
        # 1. Storage
        mock_storage = MagicMock()
        mock_storage.tenant_id = inv
        with pytest.raises(ValueError, match="Tenant mismatch:") as exc_info:
            DocumentProcessingService(storage=mock_storage, tenant_id="tenant_target")
        assert "tenant_target" not in str(exc_info.value)

        # 2. Module A
        mock_mod_a = MagicMock(spec=ModuleAPipeline)
        mock_mod_a.tenant_id = inv
        with pytest.raises(ValueError, match="Tenant mismatch:") as exc_info:
            DocumentProcessingService(module_a=mock_mod_a, tenant_id="tenant_target")
        assert "tenant_target" not in str(exc_info.value)

        # 3. Module B
        mock_mod_b = MagicMock(spec=ModuleBPipeline)
        mock_mod_b.tenant_id = inv
        with pytest.raises(ValueError, match="Tenant mismatch:") as exc_info:
            DocumentProcessingService(module_b=mock_mod_b, tenant_id="tenant_target")
        assert "tenant_target" not in str(exc_info.value)


def test_real_module_a_components_ownership_and_identity_validation(tmp_path):
    storage_root = tmp_path / "storage_root"
    storage_alpha = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")
    pipe_a = ModuleAPipeline(storage=storage_alpha, tenant_id="tenant_alpha")

    # Correct real pipeline passes validation
    service = DocumentProcessingService(module_a=pipe_a, storage=storage_alpha, tenant_id="tenant_alpha")
    assert service.tenant_id == "tenant_alpha"

    img = _make_dummy_image(tmp_path / "doc_alpha.png")

    # 1. Pipeline's canonical storage has mismatched tenant
    pipe_bad_storage = ModuleAPipeline(storage=LocalDiskStorage(storage_root, tenant_id="tenant_beta"), tenant_id="tenant_alpha")
    with pytest.raises(ValueError, match="Tenant mismatch:"):
        DocumentProcessingService(module_a=pipe_bad_storage, tenant_id="tenant_alpha")

    # 2. Renderer storage identity mismatch
    pipe_a_rend = ModuleAPipeline(storage=storage_alpha, tenant_id="tenant_alpha")
    pipe_a_rend.renderer.storage = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")  # same tenant, different instance
    with pytest.raises(ValueError, match="module_a renderer storage does not match module_a storage"):
        DocumentProcessingService(module_a=pipe_a_rend, tenant_id="tenant_alpha")

    # 3. Preprocessor storage identity mismatch
    pipe_a_prep = ModuleAPipeline(storage=storage_alpha, tenant_id="tenant_alpha")
    pipe_a_prep.preprocessor.storage = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")
    with pytest.raises(ValueError, match="module_a preprocessor storage does not match module_a storage"):
        DocumentProcessingService(module_a=pipe_a_prep, tenant_id="tenant_alpha")

    # 4. Ingestion service storage identity mismatch
    pipe_a_ing_s = ModuleAPipeline(storage=storage_alpha, tenant_id="tenant_alpha")
    pipe_a_ing_s.ingestion_service.storage = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")
    with pytest.raises(ValueError, match="module_a ingestion storage does not match module_a storage"):
        DocumentProcessingService(module_a=pipe_a_ing_s, tenant_id="tenant_alpha")

    # 5. Ingestion service tenant ID mismatch
    pipe_a_ing_t = ModuleAPipeline(storage=storage_alpha, tenant_id="tenant_alpha")
    pipe_a_ing_t.ingestion_service.tenant_id = "tenant_beta"
    with pytest.raises(ValueError, match="module_a ingestion service belongs to another tenant"):
        DocumentProcessingService(module_a=pipe_a_ing_t, tenant_id="tenant_alpha")

    # Verify no files or directories written to storage_root during any rejection
    assert not (storage_root / "tenant_alpha" / "docs").exists()
    assert not (storage_root / "tenant_beta").exists()


def test_real_module_b_loader_storage_identity_validation(tmp_path):
    storage_root = tmp_path / "storage_root"
    storage_alpha = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")
    pipe_b = ModuleBPipeline(storage=storage_alpha, tenant_id="tenant_alpha")

    # Correct pipeline passes
    service = DocumentProcessingService(module_b=pipe_b, storage=storage_alpha, tenant_id="tenant_alpha")
    assert service.tenant_id == "tenant_alpha"

    # Loader storage identity mismatch
    pipe_b_loader = ModuleBPipeline(storage=storage_alpha, tenant_id="tenant_alpha")
    pipe_b_loader.loader.storage = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")  # same tenant, different instance
    with pytest.raises(ValueError, match="module_b loader storage does not match module_b storage"):
        DocumentProcessingService(module_b=pipe_b_loader, tenant_id="tenant_alpha")


def test_rotate_page_requires_explicit_active_workspace(tmp_path):
    img = _make_dummy_image(tmp_path / "page.png")

    # 1. Missing or invalid workspace argument raises TypeError
    with pytest.raises(TypeError, match="A valid RequestTemporaryWorkspace instance is required"):
        _rotate_page(img, 90, workspace=None)

    with pytest.raises(TypeError, match="A valid RequestTemporaryWorkspace instance is required"):
        _rotate_page(img, 90, workspace="not_a_workspace")

    # 2. Inactive workspace raises RuntimeError
    ws = RequestTemporaryWorkspace()
    with pytest.raises(RuntimeError, match="not active"):
        _rotate_page(img, 90, workspace=ws)

    # 3. Cleaned-up workspace raises RuntimeError
    with ws:
        pass
    assert not ws.has_path
    with pytest.raises(RuntimeError, match="not active"):
        _rotate_page(img, 90, workspace=ws)


def test_workspace_cleanup_catches_and_logs_error_without_raising(tmp_path, monkeypatch):
    import shutil
    img = _make_dummy_image(tmp_path / "page.png")

    ws = RequestTemporaryWorkspace(prefix="test_cleanup_fail_")
    ws.__enter__()
    ws.rotate_page(img, 90)

    def failing_rmtree(path, **kwargs):
        raise OSError("Simulated disk error during cleanup")

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)

    # Calling cleanup should safely catch and log without raising an unhandled exception
    ws.cleanup()
    assert not ws.has_path


# =========================================================================
# Task 2 & Task 3: Additional F1 Storage Ownership & F2 Workspace Lifecycle Tests
# =========================================================================


def test_real_module_b_storage_tenant_mismatch_rejected_before_writes(tmp_path):
    storage_root = tmp_path / "storage_root"
    storage_alpha = LocalDiskStorage(storage_root, tenant_id="tenant_alpha")
    storage_beta = LocalDiskStorage(storage_root, tenant_id="tenant_beta")

    # Real ModuleBPipeline with tenant_alpha, but storage backend set to storage_beta
    pipe_b_mismatched = ModuleBPipeline(
        storage=storage_beta,
        tenant_id="tenant_alpha",
        settings=Settings(storage_base_dir=storage_root),
    )

    # 1. Tenant-bound service initialization rejects mismatched module_b.storage
    with pytest.raises(ValueError, match=r"Tenant mismatch: module_b storage backend belongs to another tenant\.") as exc_info:
        DocumentProcessingService(module_b=pipe_b_mismatched, storage=storage_alpha, tenant_id="tenant_alpha")
    assert "tenant_alpha" not in str(exc_info.value)
    assert "tenant_beta" not in str(exc_info.value)

    # 2. Unbound service processing a tenant_alpha request rejects mismatched module_b.storage
    unbound_service = DocumentProcessingService(module_b=pipe_b_mismatched, storage=storage_alpha)
    img = _make_dummy_image(tmp_path / "doc_alpha.png")
    req = DocumentProcessingRequest(file_path_or_stream=img, tenant_id="tenant_alpha")

    with pytest.raises(ValueError, match=r"Tenant mismatch: module_b storage backend belongs to another tenant\.") as exc_info:
        unbound_service.process_document(req)
    assert "tenant_alpha" not in str(exc_info.value)
    assert "tenant_beta" not in str(exc_info.value)

    # 3. Assert zero document writes occurred in storage root
    assert not (storage_root / "tenant_alpha" / "docs").exists()
    assert not (storage_root / "tenant_beta" / "docs").exists()


def test_workspace_cleanup_logs_cleanup_failure(tmp_path, caplog):
    import shutil
    import logging

    img = _make_dummy_image(tmp_path / "page.png")
    ws = RequestTemporaryWorkspace(prefix="test_log_cleanup_")
    ws.__enter__()
    ws_dir = ws.path
    ws.rotate_page(img, 90)

    def failing_rmtree(path, **kwargs):
        raise OSError("Permission denied: simulated cleanup fault")

    with caplog.at_level(logging.ERROR, logger="doc_extractor.processing_service"):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(shutil, "rmtree", failing_rmtree)
            ws.cleanup()

    assert not ws.has_path
    assert any(
        "Failed to clean up temporary workspace" in record.message and "Permission denied" in record.message
        for record in caplog.records
    )


def test_workspace_cleanup_failure_does_not_mask_original_processing_exception(tmp_path, monkeypatch):
    import shutil
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Force Module A to raise an explicit processing error
    mod_a.process_document.side_effect = RuntimeError("Module A ingestion catastrophic failure")

    # Force shutil.rmtree to raise an OSError during workspace cleanup in finally:
    def failing_rmtree(path, **kwargs):
        raise OSError("Simulated filesystem lock during cleanup")

    monkeypatch.setattr(shutil, "rmtree", failing_rmtree)

    diagnostics = {}
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))
    assert result.is_failed is True
    assert "Module A ingestion catastrophic failure" in diagnostics.get("split", "")


def test_cleanup_occurs_after_module_a_rejection(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Configure Module A rejection (e.g. malware or unverified bytes)
    mod_a.process_document.return_value = ModuleAHandoffPayload(
        contract_version="1.0.0",
        run_id="run_rej",
        document_id="doc_rej",
        tenant_id="tenant_test",
        status="rejected",
        internal_state="rejected_validation",
        claimed_filename="malicious.pdf",
        file_hash_sha256="0" * 64,
        file_size_bytes=100,
        page_count=0,
        mime_type="application/pdf",
        pages=[],
        error={"code": "VALIDATION_FAILED", "message": "Magic bytes check failed"},
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    created_workspaces: List[Path] = []
    orig_init = RequestTemporaryWorkspace.__init__

    def spy_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        orig_enter = self.__enter__
        def spy_enter():
            p = orig_enter()
            created_workspaces.append(self.path)
            return p
        self.__enter__ = spy_enter

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RequestTemporaryWorkspace, "__init__", spy_init)
        req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
        result = service.process_document(req)

    assert result.is_rejected is True
    assert len(created_workspaces) == 1
    assert not created_workspaces[0].exists()


def test_cleanup_occurs_after_module_b_failure(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Module B returns status="failed"
    mod_b.process_handoff.return_value = ModuleBOutputPayload(
        contract_version="1.0.0",
        run_id="run_b_fail",
        document_id="doc_123",
        tenant_id="tenant_test",
        status="failed",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.0,
            confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
            review_required=True,
        ),
        pages=[],
        combined_markdown="",
        parser_model_name="test_parser",
        classifier_model_name="test_classifier",
        error={"code": "PARSER_FAILURE", "message": "Corrupted visual layout"},
        processing_time_ms=10.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )

    created_workspaces: List[Path] = []
    orig_init = RequestTemporaryWorkspace.__init__

    def spy_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        orig_enter = self.__enter__
        def spy_enter():
            p = orig_enter()
            created_workspaces.append(self.path)
            return p
        self.__enter__ = spy_enter

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RequestTemporaryWorkspace, "__init__", spy_init)
        req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
        result = service.process_document(req)

    assert result.is_failed is True
    assert len(created_workspaces) == 1
    assert not created_workspaces[0].exists()


def test_cleanup_occurs_after_exception_following_image_rotation(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Rotation 90 degrees triggers creation of rotated image in temporary workspace
    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    # Exception in Step 5 (Module C envelope assembly)
    mod_c.process_routed_pages.side_effect = RuntimeError("Module C canonical assembly crash")

    created_workspaces: List[Path] = []
    orig_init = RequestTemporaryWorkspace.__init__

    def spy_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        orig_enter = self.__enter__
        def spy_enter():
            p = orig_enter()
            created_workspaces.append(self.path)
            return p
        self.__enter__ = spy_enter

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RequestTemporaryWorkspace, "__init__", spy_init)
        diagnostics = {}
        req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
        result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))
        assert result.is_failed is True
        assert result.error["code"] == "CANONICAL_ASSEMBLY_FAILED"
        assert "Module C canonical assembly crash" in diagnostics.get("merge", "")

    assert len(created_workspaces) == 1
    assert not created_workspaces[0].exists()


def test_explicit_generator_closure_triggers_cleanup(tmp_path):
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    created_workspaces: List[Path] = []
    orig_init = RequestTemporaryWorkspace.__init__

    def spy_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        orig_enter = self.__enter__
        def spy_enter():
            p = orig_enter()
            created_workspaces.append(self.path)
            return p
        self.__enter__ = spy_enter

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RequestTemporaryWorkspace, "__init__", spy_init)
        req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
        stream = service.process_document_stream(req)

        # Consume the first event (upload received), which entered the workspace
        event1 = next(stream)
        assert event1.step_key == "upload"
        assert len(created_workspaces) == 1
        ws_path = created_workspaces[0]
        assert ws_path.exists()

        # Explicitly close the generator before completion
        stream.close()

        # Assert workspace cleanup occurred immediately on generator closure
        assert not ws_path.exists()


def test_two_simultaneous_requests_retain_independent_workspaces(tmp_path):
    import threading
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # 90-degree rotation so files are written to workspace
    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    # Deterministic synchronization: Barrier of 2
    barrier = threading.Barrier(2)
    workspaces_in_flight: Dict[str, Path] = {}
    lock = threading.Lock()

    def sync_extract(img_path, category, language, schema_cls, subtype, engine):
        p = Path(img_path)
        with lock:
            workspaces_in_flight[threading.current_thread().name] = p.parent
        # Wait until both threads are inside extraction with active rotated files
        barrier.wait(timeout=5.0)
        return (
            {"full_name": {"value": "John Doe", "bbox": [10, 10, 50, 20]}},
            ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
            {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
        )

    routed.extract.side_effect = sync_extract

    req1 = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test", claimed_filename="req1.pdf")
    req2 = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test", claimed_filename="req2.pdf")

    results = {}
    errors = []

    def worker(req, name):
        try:
            threading.current_thread().name = name
            results[name] = service.process_document(req)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(req1, "thread_1"))
    t2 = threading.Thread(target=worker, args=(req2, "thread_2"))

    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert not errors
    assert len(workspaces_in_flight) == 2
    ws1 = workspaces_in_flight["thread_1"]
    ws2 = workspaces_in_flight["thread_2"]

    # Workspaces were distinct and non-interfering
    assert ws1 != ws2
    assert "thread_1" in results and results["thread_1"].is_success is True
    assert "thread_2" in results and results["thread_2"].is_success is True

    # After completion, both workspaces are cleaned up
    assert not ws1.exists()
    assert not ws2.exists()


# ---------------------------------------------------------------------------
# F3 to F7 Behaviour and Error-Contract Remediation Tests
# ---------------------------------------------------------------------------


def test_unexpected_router_classification_failure_produces_structured_result(tmp_path):
    """F3: Classification/routing failure produces structured failed result and event, not raw exception."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    routed.classify.side_effect = RuntimeError("VLM classifier model timed out")

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    diagnostics = {}
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))

    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error["code"] == "ROUTING_FAILED"
    assert result.error["message"] == "Document classification and routing failed."
    assert "VLM classifier model timed out" in diagnostics.get("cls", "")

    # Verify stream yields failed event
    events = list(service.process_document_stream(req))
    last_event = events[-1]
    assert last_event.step_key == "cls"
    assert last_event.state == "failed"
    assert last_event.data["error"] == "Document classification and routing failed."
    assert last_event.result is not None
    assert last_event.result.error["code"] == "ROUTING_FAILED"


def test_unexpected_rotation_failure_produces_structured_result(tmp_path, monkeypatch):
    """F3: Unexpected rotation failure produces structured failed result and event."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    def failing_rotate(self, path, clockwise_deg, page_no=1):
        raise OSError("Corrupt JPEG header during rotation")

    monkeypatch.setattr(RequestTemporaryWorkspace, "rotate_page", failing_rotate)

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    diagnostics = {}
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))

    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error["code"] == "ROTATION_FAILED"
    assert result.error["message"] == "Document page rotation failed."
    assert "Corrupt JPEG header during rotation" in diagnostics.get("cls", "")


def test_canonical_module_c_assembly_failure_produces_structured_result(tmp_path):
    """F3: Module C canonical envelope assembly failure produces structured failed result."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    mod_c.process_routed_pages.side_effect = ValueError("Schema validation mismatch in canonical builder")

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    diagnostics = {}
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))

    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error["code"] == "CANONICAL_ASSEMBLY_FAILED"
    assert result.error["message"] == "Canonical output assembly failed."
    assert "Schema validation mismatch in canonical builder" in diagnostics.get("merge", "")


def test_partial_page_extraction_resilience_preserves_successful_pages(tmp_path):
    """F3: Per-page extraction failure on page 1 does not abort; page 2 is extracted and merged."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    call_count = 0

    def extract_side_effect(img_path, category, language, schema_cls, subtype, engine=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("OOM during specialist model execution on page 1")
        return (
            {"tax_id": {"value": "99-9999999", "bbox": [20, 20, 60, 30]}},
            ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
            {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
        )

    routed.extract.side_effect = extract_side_effect

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    result = service.process_document(req)

    assert result.is_success is True
    assert len(result.page_results) == 2
    assert result.page_results[0].error == "RuntimeError: Extraction failed for page 1."
    assert "OOM during specialist model execution on page 1" not in result.page_results[0].error
    assert result.page_results[0].raw is None
    assert result.page_results[1].error is None
    assert "tax_id" in result.page_results[1].raw

    # Verify Module C received both results
    args, kwargs = mod_c.process_routed_pages.call_args
    passed_page_results = args[1]
    assert len(passed_page_results) == 2
    assert passed_page_results[0].page_number == 1
    assert passed_page_results[1].page_number == 2


def test_gradio_error_handling_preserves_original_behavior(tmp_path, monkeypatch):
    """F4: Gradio _process_core preserves 'Error: {e}' format for Module A/B exceptions and 'Module B failed:' for failures."""
    from doc_extractor.app import _process_core

    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path, tenant_id="test_tenant")

    # 1. Module A exception
    mod_a.process_document.side_effect = RuntimeError("Catastrophic ingestion crash")
    monkeypatch.setattr("doc_extractor.app.get_pipelines", lambda settings: (mod_a, mod_b))
    monkeypatch.setattr("doc_extractor.app.get_module_c", lambda: mod_c)
    monkeypatch.setattr("doc_extractor.app.get_routed_extractor", lambda: routed)

    steps = []
    outputs = list(_process_core(str(img1), "Auto-Detect", "Auto-Detect", "Auto-Detect", "Auto-Detect", "Follow routing table", steps))
    last_output = outputs[-1]
    gallery, status, json_out, md, json_path = last_output
    assert status.startswith("Error: ")
    assert "Catastrophic ingestion crash" in status
    assert "Catastrophic ingestion crash" in json_out
    assert any(s["key"] == "split" and s["state"] == "failed" for s in steps)

    # 2. Module B failure (status="failed")
    mod_a.process_document.side_effect = None
    mod_b.process_handoff.return_value = ModuleBOutputPayload(
        contract_version="1.0.0",
        run_id="run_b_err",
        document_id="doc_123",
        tenant_id="test_tenant",
        status="failed",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.0,
            confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
            review_required=True,
        ),
        pages=[],
        combined_markdown="",
        parser_model_name="test_parser",
        classifier_model_name="test_classifier",
        processing_time_ms=1.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "PAGE_CORRUPT", "message": "Authoritative page reference unreadable"},
    )
    steps_b = []
    outputs_b = list(_process_core(str(img1), "Auto-Detect", "Auto-Detect", "Auto-Detect", "Auto-Detect", "Follow routing table", steps_b))
    last_b = outputs_b[-1]
    gallery_b, status_b, json_out_b, _, _ = last_b
    assert status_b == "Module B failed: PAGE_CORRUPT: Authoritative page reference unreadable"
    assert "MODULE_B_ERROR" in json_out_b
    assert any(s["key"] == "cls" and s["state"] == "failed" for s in steps_b)


def test_gradio_rejected_file_preserves_rejection_shape(tmp_path, monkeypatch):
    """F4: Gradio _process_core preserves 'Rejected: {payload_a.error}' and payload JSON."""
    from doc_extractor.app import _process_core

    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path, tenant_id="test_tenant")
    rejection_payload = ModuleAHandoffPayload(
        contract_version="1.0.0",
        run_id="run_rej",
        document_id="doc_rej",
        tenant_id="test_tenant",
        status="rejected",
        internal_state="rejected",
        claimed_filename="bad.exe",
        file_hash_sha256="b" * 64,
        file_size_bytes=100,
        page_count=0,
        mime_type="application/x-dosexec",
        pages=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "UNSUPPORTED_FILE", "message": "Executable files rejected by security policy."},
    )
    mod_a.process_document.return_value = rejection_payload
    monkeypatch.setattr("doc_extractor.app.get_pipelines", lambda settings: (mod_a, mod_b))
    monkeypatch.setattr("doc_extractor.app.get_module_c", lambda: mod_c)
    monkeypatch.setattr("doc_extractor.app.get_routed_extractor", lambda: routed)

    steps = []
    outputs = list(_process_core(str(img1), "Auto-Detect", "Auto-Detect", "Auto-Detect", "Auto-Detect", "Follow routing table", steps))
    last = outputs[-1]
    gallery, status, json_out, _, _ = last
    assert status == f"Rejected: {rejection_payload.error}"
    assert "UNSUPPORTED_FILE" in json_out
    assert any(s["key"] == "split" and s["state"] == "failed" for s in steps)


def test_unknown_engine_fallback_to_routing_table(tmp_path, caplog):
    """F5: Supplying an unknown engine choice safely falls back to None (routing table) without raising ValueError."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Unit check for resolve_engine_choice
    assert resolve_engine_choice(None) is None
    assert resolve_engine_choice("") is None
    assert resolve_engine_choice("Follow routing table") is None
    assert resolve_engine_choice("vlm") is None
    assert resolve_engine_choice("unknown_experimental_engine_999") is None

    # End-to-end processing with unknown engine choice
    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        tenant_id="tenant_test",
        engine_choice="unknown_experimental_engine_999",
    )
    with caplog.at_level(logging.WARNING):
        result = service.process_document(req)

    assert result.is_success is True
    # Ensure routed.extract was invoked with engine=None
    extract_call_args = routed.extract.call_args_list[0]
    assert extract_call_args.kwargs.get("engine") is None


def test_supported_alternative_engine_routing(tmp_path):
    """F5: Supported alternative engine keys ('ocr_text') and UI labels are preserved."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Verify UI label mappings
    assert resolve_engine_choice("GLM-OCR + Qwen3-4B-Instruct text (experimental)") == "ocr_text"

    # Verify raw keys
    assert resolve_engine_choice("ocr_text") == "ocr_text"
    assert resolve_engine_choice("ocr_text_7b") is None          # removed engine: routing table default

    # Process with alternative engine choice
    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        tenant_id="tenant_test",
        engine_choice="GLM-OCR + Qwen3-4B-Instruct text (experimental)",
    )
    result = service.process_document(req)
    assert result.is_success is True

    extract_call_args = routed.extract.call_args_list[0]
    assert extract_call_args.kwargs.get("engine") == "ocr_text"


def test_typed_tenant_mismatch_error_concealed_as_404(tmp_path):
    """F6: TenantMismatchError is typed, inherits from ValueError, and maps to 404 concealed as DOCUMENT_NOT_FOUND."""
    from doc_extractor.api.exceptions import concealed_not_found, _STATUS_CODE_MAP

    assert issubclass(TenantMismatchError, ValueError)
    assert issubclass(TenantMismatchError, DocumentExtractorError)

    service, _, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_bound")
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_other")

    with pytest.raises(TenantMismatchError) as exc_info:
        service.process_document(req)

    exc = exc_info.value
    # No tenant ID disclosed
    assert "tenant_other" not in str(exc)
    assert "tenant_bound" not in str(exc)
    assert "Tenant mismatch" in str(exc)

    # API mapping
    assert _STATUS_CODE_MAP[TenantMismatchError] == 404
    concealed = concealed_not_found(exc)
    assert concealed == ("DOCUMENT_NOT_FOUND", "Document not found.")

    # Contrast with InvalidTenantIdentifierError
    inv_err = InvalidTenantIdentifierError("whitespace_only")
    assert _STATUS_CODE_MAP[InvalidTenantIdentifierError] == 422
    assert concealed_not_found(inv_err) is None


def test_api_facing_errors_do_not_expose_filesystem_paths_or_raw_exceptions(tmp_path):
    """F7: result.error contains sanitized messages without raw paths; diagnostic_sink retains debug info."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    sensitive_path = "/var/secrets/confidential/tenant_data/file.pdf"
    mod_a.process_document.side_effect = FileNotFoundError(f"Missing internal resource: {sensitive_path}")

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    diagnostics = {}
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))

    assert result.is_failed is True
    # result.error is sanitized
    assert sensitive_path not in json.dumps(result.error)
    assert result.error == {
        "code": "INGESTION_ERROR",
        "message": "Document ingestion and preprocessing failed.",
    }

    # Public result serialization (asdict, repr) contains zero filesystem paths or raw exceptions
    import dataclasses
    assert sensitive_path not in repr(result)
    assert sensitive_path not in str(dataclasses.asdict(result))

    # diagnostic_sink retains the raw exception for trusted logging
    assert sensitive_path in diagnostics.get("split", "")


def test_storage_uri_resolution_failure_produces_structured_result(tmp_path):
    """F3/F7: Storage URI resolution failure produces structured failed result without path leakage."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    internal_path = "/storage/pools/encrypted/metadata.json"
    mod_a.storage.resolve_uri_to_path.side_effect = PermissionError(f"Permission denied accessing {internal_path}")

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    diagnostics = {}
    result = service.process_document(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m))

    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error["code"] == "STORAGE_RESOLUTION_FAILED"
    assert result.error["message"] == "Failed to resolve storage URIs for document pages."
    assert internal_path not in json.dumps(result.error)
    import dataclasses
    assert internal_path not in repr(result)
    assert internal_path not in str(dataclasses.asdict(result))
    assert internal_path in diagnostics.get("split", "")


# =========================================================================
# Final Milestone 1 Remediation Regression Tests (Requirements 1 - 11)
# =========================================================================


def test_req1_component_configuration_faults_map_to_500_not_concealed_404(tmp_path):
    """1. Component configuration faults map to generic 500 INTERNAL_ERROR, never concealed 404."""
    from doc_extractor.api.exceptions import concealed_not_found, _STATUS_CODE_MAP, _SERVER_SIDE_ONLY
    from doc_extractor.exceptions import ComponentConfigurationError, ConfigurationError

    # Verify class hierarchy
    assert issubclass(ComponentConfigurationError, ConfigurationError)
    assert issubclass(ComponentConfigurationError, ValueError)

    # Verify HTTP status code mapping is 500 and included in server-side only
    assert _STATUS_CODE_MAP[ComponentConfigurationError] == 500
    assert ComponentConfigurationError in _SERVER_SIDE_ONLY

    # Verify it is NEVER concealed as 404
    exc = ComponentConfigurationError("Injected component belongs to wrong tenant.")
    assert concealed_not_found(exc) is None

    # Verify constructor raises ComponentConfigurationError when injected component has mismatched tenant
    mock_storage = MagicMock()
    mock_storage.tenant_id = "tenant_injected"
    with pytest.raises(ComponentConfigurationError) as exc_info:
        DocumentProcessingService(storage=mock_storage, tenant_id="tenant_service")
    assert "tenant_service" not in str(exc_info.value)
    assert "tenant_injected" not in str(exc_info.value)
    assert "Tenant mismatch:" in str(exc_info.value)


def test_req2_caller_facing_tenant_mismatch_maps_to_concealed_404(tmp_path):
    """2. Caller-facing tenant mismatches (TenantMismatchError) map to concealed 404 DOCUMENT_NOT_FOUND."""
    from doc_extractor.api.exceptions import concealed_not_found, _STATUS_CODE_MAP
    from doc_extractor.exceptions import TenantMismatchError

    service, _, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_service_bound")
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_caller_claimed")

    with pytest.raises(TenantMismatchError) as exc_info:
        service.process_document(req)

    exc = exc_info.value
    # Preserves SEC-006: never discloses tenant identifiers
    assert "tenant_service_bound" not in str(exc)
    assert "tenant_caller_claimed" not in str(exc)
    assert "Tenant mismatch" in str(exc)

    # Maps to 404 concealed as DOCUMENT_NOT_FOUND
    assert _STATUS_CODE_MAP[TenantMismatchError] == 404
    assert concealed_not_found(exc) == ("DOCUMENT_NOT_FOUND", "Document not found.")


def test_req3_invalid_tenant_identifier_maps_to_422(tmp_path):
    """3. Invalid tenant identifiers (InvalidTenantIdentifierError) map to 422."""
    from doc_extractor.api.exceptions import concealed_not_found, _STATUS_CODE_MAP
    from doc_extractor.exceptions import InvalidTenantIdentifierError

    assert _STATUS_CODE_MAP[InvalidTenantIdentifierError] == 422
    inv_exc = InvalidTenantIdentifierError("tenant is empty or whitespace")
    assert concealed_not_found(inv_exc) is None

    service, _, _, _, _, img1 = _setup_mock_service(tmp_path, tenant_id="tenant_valid")
    # Whitespace-only tenant in request or service entry point
    with pytest.raises(InvalidTenantIdentifierError):
        DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="   ")

    with pytest.raises(InvalidTenantIdentifierError):
        service.process_document(img1, tenant_id="   ")


def test_req4_raw_filesystem_paths_absent_from_all_public_serializations(tmp_path):
    """4. Raw filesystem paths do not appear in any public result or event serialization."""
    import dataclasses
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    secret_path = "/internal/super/secret/path/to/tenant_file.pdf"
    mod_a.process_document.side_effect = RuntimeError(f"Failed opening file: {secret_path}")

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    events = list(service.process_document_stream(req))

    for ev in events:
        # Check event detail and data
        assert secret_path not in (ev.detail or "")
        assert secret_path not in json.dumps(ev.data or {})
        if ev.result is not None:
            # Dataclass serialization
            res_dict = dataclasses.asdict(ev.result)
            assert secret_path not in str(res_dict)
            assert secret_path not in repr(ev.result)
            assert secret_path not in json.dumps(ev.result.error or {})


def test_req5_raw_exception_text_not_present_in_progress_event_details_or_data(tmp_path):
    """5. Raw exception text is not present in progress-event details or data."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    raw_error_text = "DBConnectionPoolTimeoutError: connection timed out after 30000ms"
    mod_a.process_document.side_effect = RuntimeError(raw_error_text)

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    events = list(service.process_document_stream(req))

    failed_event = events[-1]
    assert failed_event.state == "failed"
    assert raw_error_text not in failed_event.detail
    assert raw_error_text not in json.dumps(failed_event.data)
    # Detail contains only sanitized, high-level message
    assert failed_event.detail == "Document ingestion and preprocessing failed."


def test_req6_per_page_exceptions_are_sanitized_on_routed_page_result(tmp_path):
    """6. Per-page exceptions are sanitized on RoutedPageResult.error."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    def extract_with_sensitive_info(img_path, category, language, schema_cls, subtype, engine=None):
        raise ZeroDivisionError("/storage/data/secret.bin division by zero in specialist weights")

    routed.extract.side_effect = extract_with_sensitive_info
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    result = service.process_document(req)

    assert len(result.page_results) == 2
    for pr in result.page_results:
        assert "/storage/data/secret.bin" not in pr.error
        assert "division by zero" not in pr.error
        assert pr.error.startswith("ZeroDivisionError: Extraction failed for page ")


def test_req7_trusted_gradio_diagnostic_display_preserves_legacy_messages(tmp_path):
    """7. Trusted Gradio diagnostic display preserves required legacy messages (f'Error: {e}' and JSON shape)."""
    from doc_extractor.app import _process_core
    test_img = tmp_path / "test_doc.png"
    _make_dummy_image(test_img)

    # Test failure during split with custom diagnostic
    with pytest.MonkeyPatch.context() as mp:
        mock_storage = MagicMock(spec=["tenant_id", "resolve_uri_to_path"])
        mock_storage.tenant_id = "test_tenant"

        mock_a = MagicMock(spec=["tenant_id", "storage", "process_document"])
        mock_a.tenant_id = "test_tenant"
        mock_a.storage = mock_storage
        raw_exc_msg = "Corrupted zlib stream in decompression table at offset 0x4f"
        mock_a.process_document.side_effect = RuntimeError(raw_exc_msg)

        mock_b = MagicMock(spec=["tenant_id", "process_handoff"])
        mock_b.tenant_id = "test_tenant"

        mp.setattr("doc_extractor.app.get_pipelines", lambda settings: (mock_a, mock_b))

        steps = []
        outputs = list(_process_core(
            test_img,
            category_choice="Auto-detect",
            doc_type_choice="Auto-detect",
            subtype_choice="",
            language_choice="English",
            engine_choice="Follow routing table",
            steps=steps,
        ))

        # Gradio must yield Error: {e} and {"error": str(e)}
        last_out = outputs[-1]
        gallery, status, json_out, classification_md, download_path = last_out
        assert gallery is None
        assert status == f"Error: {raw_exc_msg}"
        parsed_json = json.loads(json_out)
        assert parsed_json == {"error": raw_exc_msg}
        # And the split step detail is updated
        split_step = next(s for s in steps if s["key"] == "split")
        assert split_step["state"] == "failed"
        assert raw_exc_msg[:120] in split_step["detail"]


def test_req8_missing_model_used_metadata_produces_controlled_handling(tmp_path):
    """8. Missing model_used metadata produces controlled handling (no KeyError)."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Route dict returned by extractor completely lacks model_used or has None
    def extract_sparse_route(img_path, category, language, schema_cls, subtype, engine=None):
        return (
            {"invoice_number": {"value": "INV-101", "bbox": [10, 10, 50, 20]}},
            ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
            {"grounded": True},  # No model_used or fallback_occurred keys
        )

    routed.extract.side_effect = extract_sparse_route
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    # Must not raise KeyError
    result = service.process_document(req)
    assert result.is_success is True
    assert result.pages_info[0]["model_used"] == "unknown_model"
    assert result.pages_info[0]["fallback_occurred"] is False


def test_req9_invalid_module_a_and_b_return_values_handled_defensively(tmp_path):
    """9. Invalid Module A and B return values (None or unexpected type) produce controlled handling."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Case A: Module A returns a dict instead of ModuleAHandoffPayload
    mod_a.process_document.return_value = {"not": "a_payload"}
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    result = service.process_document(req)
    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error["code"] == "INGESTION_ERROR"
    assert result.error["message"] == "Document ingestion failed: invalid payload produced."

    # Case B: Module A returns None
    mod_a.process_document.return_value = None
    result = service.process_document(req)
    assert result.is_failed is True
    assert result.error["code"] == "INGESTION_ERROR"
    assert result.error["message"] == "Document ingestion failed: invalid payload produced."

    # Case C: Module B returns None
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    mod_b.process_handoff.return_value = None
    result = service.process_document(req)
    assert result.is_failed is True
    assert result.error["code"] == "MODULE_B_ERROR"
    assert result.error["message"] == "Module B processing failed: invalid payload produced."


def test_req10_typed_storage_and_tenant_exceptions_bubble_up_unswallowed(tmp_path):
    """10. Typed storage and tenant exceptions bubble up unswallowed and retain semantics."""
    from doc_extractor.exceptions import (
        CrossTenantStorageAccessError,
        TenantStorageUnavailableError,
        TenantStorageIsolationError,
    )

    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")

    # Ingestion stage: CrossTenantStorageAccessError bubbles up
    mod_a.process_document.side_effect = CrossTenantStorageAccessError("Cross tenant storage access blocked")
    with pytest.raises(CrossTenantStorageAccessError):
        service.process_document(req)

    # Storage resolution stage: TenantStorageUnavailableError bubbles up
    mod_a.process_document.side_effect = None
    mod_a.storage.resolve_uri_to_path.side_effect = TenantStorageUnavailableError("Storage backend is down")
    with pytest.raises(TenantStorageUnavailableError):
        service.process_document(req)

    # Module B stage: TenantStorageIsolationError bubbles up
    mod_a.storage.resolve_uri_to_path.side_effect = None
    mod_b.process_handoff.side_effect = TenantStorageIsolationError("Tenant isolation breached")
    with pytest.raises(TenantStorageIsolationError):
        service.process_document(req)


def test_req11_existing_partial_page_extraction_operational(tmp_path):
    """11. Existing partial-page extraction remains operational and passes page results to Module C."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    calls = 0
    def extract_with_page1_fail(img_path, category, language, schema_cls, subtype, engine=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Transient worker GPU timeout")
        return (
            {"invoice_total": {"value": 500.0, "bbox": [10, 10, 50, 30]}},
            ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
            {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
        )

    routed.extract.side_effect = extract_with_page1_fail
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")
    result = service.process_document(req)

    assert result.is_success is True
    assert len(result.page_results) == 2
    # Page 1 sanitized error
    assert result.page_results[0].error == "RuntimeError: Extraction failed for page 1."
    assert result.page_results[0].raw is None
    # Page 2 success
    assert result.page_results[1].error is None
    assert "invoice_total" in result.page_results[1].raw
    # Module C was called with both page results
    assert mod_c.process_routed_pages.called


def test_f7_module_b_failure_sanitizes_errors_and_prevents_path_leakage(tmp_path):
    """F7: Module B failed payload containing internal path is sanitized; zero path leakage in public fields."""
    import dataclasses
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    fake_internal_path = "/var/data/tenants/confidential_b/secrets.json"
    raw_error_message = f"Failed opening parser configuration: {fake_internal_path}"

    mod_b.process_handoff.return_value = ModuleBOutputPayload(
        contract_version="1.0.0",
        run_id="run_b_leak",
        document_id="doc_123",
        tenant_id="tenant_test",
        status="failed",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.0,
            confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
            review_required=True,
        ),
        pages=[],
        combined_markdown="",
        parser_model_name="test_parser",
        classifier_model_name="test_classifier",
        processing_time_ms=12.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "IO_ERROR", "message": raw_error_message},
    )

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="doc.pdf",
        tenant_id="tenant_test",
    )

    diagnostics: Dict[str, str] = {}
    events = list(service.process_document_stream(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m)))
    result = events[-1].result
    assert result is not None

    # 1. Result status and sanitized error code
    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error == {
        "code": "MODULE_B_ERROR",
        "message": "Document page classification and layout analysis failed.",
    }

    # 2. Path absent from all public serialization
    assert fake_internal_path not in json.dumps(result.error)
    assert fake_internal_path not in repr(result)
    assert fake_internal_path not in str(dataclasses.asdict(result))

    # 3. Nested payload_b error is sanitized
    assert result.payload_b is not None
    assert fake_internal_path not in json.dumps(result.payload_b.error)
    assert result.payload_b.error == {
        "code": "MODULE_B_ERROR",
        "message": "Document page classification and layout analysis failed.",
    }

    # 4. Progress events data and detail do not contain fake path
    for ev in events:
        assert fake_internal_path not in (ev.detail or "")
        assert fake_internal_path not in json.dumps(ev.data or {}, default=str)

    # 5. Diagnostic sink received raw diagnostic for trusted server logging
    assert fake_internal_path in diagnostics.get("cls", "")


def test_f3_failed_module_a_payload_is_terminal_and_halts_downstream(tmp_path):
    """F3: When Module A returns status='failed', it is treated as terminal and halts downstream execution."""
    import dataclasses
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    failed_payload_a = ModuleAHandoffPayload(
        contract_version="1.0.0",
        run_id="run_a_fail",
        document_id="doc_a_fail",
        tenant_id="tenant_test",
        status="failed",
        internal_state="ingestion_failure",
        claimed_filename="corrupt_input.pdf",
        file_hash_sha256="c" * 64,
        file_size_bytes=2048,
        page_count=0,
        mime_type="application/pdf",
        pages=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "PDF_CORRUPT", "message": "Corrupted XRef table at byte offset 512"},
    )
    mod_a.process_document.return_value = failed_payload_a

    req = DocumentProcessingRequest(
        file_path_or_stream=img1,
        claimed_filename="corrupt_input.pdf",
        tenant_id="tenant_test",
    )

    diagnostics: Dict[str, str] = {}
    events = list(service.process_document_stream(req, diagnostic_sink=lambda s, m: diagnostics.setdefault(s, m)))
    result = events[-1].result
    assert result is not None

    # 1. Result is failed with INGESTION_ERROR
    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error == {
        "code": "INGESTION_ERROR",
        "message": "Document ingestion and preprocessing failed.",
    }

    # 2. Downstream pipeline stages never invoked
    mod_b.process_handoff.assert_not_called()
    routed.classify.assert_not_called()
    routed.extract.assert_not_called()
    mod_c.process_routed_pages.assert_not_called()

    # 3. Sanitized payload_a attached without exposing raw error
    assert result.payload_a is not None
    assert result.payload_a.error == {
        "code": "INGESTION_ERROR",
        "message": "Document ingestion and preprocessing failed.",
    }
    assert "Corrupted XRef table" not in str(dataclasses.asdict(result))

    # 4. Diagnostic sink received the raw message
    assert "Corrupted XRef table" in diagnostics.get("split", "")

    # 5. Distinct from rejected-file behavior
    rejected_payload_a = ModuleAHandoffPayload(
        contract_version="1.0.0",
        run_id="run_a_rej",
        document_id="doc_a_rej",
        tenant_id="tenant_test",
        status="rejected",
        internal_state="rejected",
        claimed_filename="malicious.exe",
        file_hash_sha256="d" * 64,
        file_size_bytes=512,
        page_count=0,
        mime_type="application/x-dosexec",
        pages=[],
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "REJECTED", "message": "File rejected by security scanner."},
    )
    mod_a.process_document.return_value = rejected_payload_a
    res_rej = service.process_document(req)
    assert res_rej.status == "rejected"
    assert res_rej.error["code"] == "REJECTED"


def test_f4_gradio_displays_per_page_diagnostics_while_public_fields_remain_sanitized(tmp_path, monkeypatch):
    """F4: Gradio displays trusted diagnostic for per-page extraction error while public API/event fields remain sanitized."""
    import dataclasses
    from doc_extractor.app import _process_core
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path, tenant_id="test_tenant")

    def failing_extract(img_path, category, language, schema_cls, subtype, engine=None):
        raise RuntimeError("VLM worker CUDA out of memory on page 1")

    routed.extract.side_effect = failing_extract
    monkeypatch.setattr("doc_extractor.app.get_pipelines", lambda settings: (mod_a, mod_b))
    monkeypatch.setattr("doc_extractor.app.get_module_c", lambda: mod_c)
    monkeypatch.setattr("doc_extractor.app.get_routed_extractor", lambda: routed)

    steps = []
    outputs = list(_process_core(
        str(img1),
        category_choice="Auto-Detect",
        doc_type_choice="Auto-Detect",
        subtype_choice="",
        language_choice="English",
        engine_choice="Follow routing table",
        steps=steps,
    ))

    # 1. Gradio steps panel contains the legacy per-page diagnostic
    ext_step = next(s for s in steps if s["key"] == "ext1")
    assert ext_step["state"] == "failed"
    assert "CUDA out of memory" in ext_step["detail"]

    # 2. Public events and result from the service are strictly sanitized
    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="test_tenant")
    events = list(service.process_document_stream(req))
    ext_event = next(e for e in events if e.step_key == "ext1" and e.state == "failed")
    assert "CUDA out of memory" not in ext_event.detail
    assert "CUDA out of memory" not in json.dumps(ext_event.data)
    assert ext_event.detail == "Page 1 extraction failed"
    assert ext_event.data["error"] == "RuntimeError: Extraction failed for page 1."

    # Final result page result is sanitized
    final_res = events[-1].result
    assert final_res is not None
    assert final_res.page_results[0].error == "RuntimeError: Extraction failed for page 1."
    assert "CUDA out of memory" not in repr(final_res)
    assert "CUDA out of memory" not in str(dataclasses.asdict(final_res))


def test_defensive_diagnostic_sink_failure_does_not_break_processing(tmp_path):
    """Defensive: A raising diagnostic_sink callback is logged safely and does not break processing or disclose exceptions."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)
    mod_a.process_document.side_effect = RuntimeError("Original pipeline failure in Module A")

    sink_call_count = 0
    def broken_sink(stage: str, msg: str) -> None:
        nonlocal sink_call_count
        sink_call_count += 1
        raise ConnectionResetError("Host telemetry sink connection reset by peer")

    req = DocumentProcessingRequest(file_path_or_stream=img1, tenant_id="tenant_test")

    # Invocation must not raise ConnectionResetError
    result = service.process_document(req, diagnostic_sink=broken_sink)

    assert sink_call_count >= 1
    assert result.is_failed is True
    assert result.status == "failed"
    assert result.error == {
        "code": "INGESTION_ERROR",
        "message": "Document ingestion and preprocessing failed.",
    }


# ============================================================================
# Milestone 2C: Single-Page Stored Document Processing Service Tests
# ============================================================================

def test_process_stored_page_success(tmp_path):
    """Verify successful single-page processing of a stored document page."""
    service, mod_a, mod_b, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    events: List[ProcessingProgressEvent] = []
    res = service.process_stored_page(
        document_id="doc_123",
        page_number=1,
        progress_callback=events.append,
    )

    assert res.is_success is True
    assert res.status == "needs_review"
    assert res.document_id == "doc_123"
    assert res.tenant_id == "tenant_test"
    assert res.envelope is not None
    assert len(res.page_results) == 1
    assert res.page_results[0].page_number == 1
    assert len(res.pages_info) == 1
    assert res.pages_info[0]["page"] == 1
    assert len(res.page_items) == 1
    assert res.page_items[0][0] == 1
    assert len(res.routes) == 1
    assert res.routes[0]["page"] == 1
    assert res.error is None

    # Step keys in progress events: load -> cls -> ext1 -> merge
    step_keys = [e.step_key for e in events]
    assert "load" in step_keys
    assert "cls" in step_keys
    assert "ext1" in step_keys
    assert "merge" in step_keys

    # Module A ingestion was NOT rerun
    mod_a.process_document.assert_not_called()


def test_process_stored_page_missing_handoff(tmp_path):
    """Verify missing Module A handoff returns failed result with HANDOFF_NOT_FOUND."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path)

    res = service.process_stored_page(document_id="nonexistent_doc", page_number=1)

    assert res.is_failed is True
    assert res.status == "failed"
    assert res.error["code"] == "HANDOFF_NOT_FOUND"
    assert res.error["message"] == "Module A handoff payload not found for document."


def test_process_stored_page_malformed_handoff(tmp_path):
    """Verify malformed Module A handoff returns failed result with MALFORMED_HANDOFF."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path)
    service.storage.get_handoff_payload.side_effect = lambda doc_id: {"corrupt": "data"}

    res = service.process_stored_page(document_id="doc_123", page_number=1)

    assert res.is_failed is True
    assert res.status == "failed"
    assert res.error["code"] == "MALFORMED_HANDOFF"
    assert res.error["message"] == "Module A handoff payload is malformed or invalid."


def test_process_stored_page_invalid_page_number_and_missing_page(tmp_path):
    """Verify invalid page numbers raise ValueError and missing pages return failed result."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path)

    # Invalid page numbers must raise ValueError
    with pytest.raises(ValueError, match="Invalid page number"):
        service.process_stored_page("doc_123", page_number=0)

    with pytest.raises(ValueError, match="Invalid page number"):
        service.process_stored_page("doc_123", page_number=-2)

    with pytest.raises(ValueError, match="Invalid page number"):
        service.process_stored_page("doc_123", page_number="first")

    # Missing page (valid number, but page does not exist in the 2-page document)
    res = service.process_stored_page("doc_123", page_number=99)
    assert res.is_failed is True
    assert res.status == "failed"
    assert res.error["code"] == "PAGE_NOT_FOUND"
    assert "Page 99 not found" in res.error["message"]


def test_process_stored_page_cross_tenant_access_rejected(tmp_path):
    """Verify cross-tenant document access is rejected fail-closed without disclosing tenant identity."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path, tenant_id="tenant_a")

    # Case 1: Caller supplies conflicting tenant
    with pytest.raises(TenantMismatchError) as exc_info:
        service.process_stored_page("doc_123", page_number=1, tenant_id="tenant_b")
    assert "tenant_a" not in str(exc_info.value)
    assert "tenant_b" not in str(exc_info.value)
    assert "Tenant mismatch" in str(exc_info.value)

    # Case 2: Stored handoff belongs to a different tenant
    corrupt_tenant_handoff = deepcopy(service.storage.get_handoff_payload("doc_123"))
    corrupt_tenant_handoff["tenant_id"] = "tenant_attacker"
    service.storage.get_handoff_payload.side_effect = lambda doc_id: corrupt_tenant_handoff

    with pytest.raises(TenantMismatchError) as exc_info:
        service.process_stored_page("doc_123", page_number=1)
    assert "tenant_attacker" not in str(exc_info.value)
    assert "tenant_a" not in str(exc_info.value)
    assert "Tenant mismatch" in str(exc_info.value)


def test_process_stored_page_preserves_page_geometry_and_evidence(tmp_path):
    """Verify stored page processing preserves original page geometry and evidence references."""
    service, _, _, routed, mod_c, img1 = _setup_mock_service(tmp_path)

    # Simulate 90 degree rotation detection during classification
    routed.classify.return_value = {
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    }

    orig_bytes = img1.read_bytes()
    orig_hash = hashlib.sha256(orig_bytes).hexdigest()

    res = service.process_stored_page("doc_123", page_number=1)
    assert res.is_success is True

    # 1. Original file bytes on disk are completely unmodified
    assert img1.read_bytes() == orig_bytes
    assert hashlib.sha256(img1.read_bytes()).hexdigest() == orig_hash

    # 2. Result page_items preserves unrotated paths
    assert len(res.page_items) == 1
    page_no, page_path, orig_path = res.page_items[0]
    assert page_no == 1
    assert "upright" not in page_path.name
    if orig_path:
        assert "upright" not in orig_path.name

    # 3. Module C was called with page 1 PageImageRef
    assert mod_c.process_routed_pages.called
    call_args = mod_c.process_routed_pages.call_args
    passed_page_images = call_args[0][2]
    assert len(passed_page_images) == 1
    assert passed_page_images[0].page_number == 1
    assert passed_page_images[0].page_ref.original_width_px == 100
    assert passed_page_images[0].page_ref.original_height_px == 100


def test_process_stored_page_module_b_extraction_failure(tmp_path):
    """Verify Module B processing failures return sanitized failed result."""
    # Case A: Exception in Module B
    service, _, mod_b, _, _, _ = _setup_mock_service(tmp_path)
    mod_b.process_handoff.side_effect = RuntimeError("/sensitive/path/ocr_engine_crash.bin")

    res = service.process_stored_page("doc_123", page_number=1)
    assert res.is_failed is True
    assert res.status == "failed"
    assert res.error["code"] == "MODULE_B_ERROR"
    assert res.error["message"] == "Module B processing failed."
    assert "/sensitive/path" not in str(res.error)

    # Case B: Module B returns failed payload
    service2, _, mod_b2, _, _, _ = _setup_mock_service(tmp_path)
    mod_b2.process_handoff.return_value = ModuleBOutputPayload(
        contract_version="1.0.0",
        run_id="run_b_fail",
        document_id="doc_123",
        tenant_id="tenant_test",
        status="failed",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.0,
            confidence_provenance=ConfidenceProvenance.UNCALIBRATED,
            review_required=True,
        ),
        pages=[],
        combined_markdown="",
        parser_model_name="test",
        classifier_model_name="test",
        processing_time_ms=10.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
        error={"code": "INTERNAL", "message": "Raw internal failure /var/log/app.log"},
    )

    res2 = service2.process_stored_page("doc_123", page_number=1)
    assert res2.is_failed is True
    assert res2.status == "failed"
    assert res2.error["code"] == "MODULE_B_ERROR"
    assert res2.error["message"] == "Document page classification and layout analysis failed."
    assert "/var/log" not in str(res2.error)


def test_process_stored_page_module_c_failure(tmp_path):
    """Verify Module C canonical assembly failure returns sanitized failed result."""
    service, _, _, _, mod_c, _ = _setup_mock_service(tmp_path)
    mod_c.process_routed_pages.side_effect = ValueError("Corrupt envelope structure at /tmp/workspace/env.json")

    res = service.process_stored_page("doc_123", page_number=1)
    assert res.is_failed is True
    assert res.status == "failed"
    assert res.error["code"] == "CANONICAL_ASSEMBLY_FAILED"
    assert res.error["message"] == "Canonical output assembly failed."
    assert "/tmp/workspace" not in str(res.error)


def test_process_stored_page_workspace_cleanup_success_and_failure(tmp_path):
    """Verify temporary workspace cleanup is guaranteed after success and failure."""
    from unittest.mock import patch
    from doc_extractor.processing_service import RequestTemporaryWorkspace

    service, _, _, _, mod_c, _ = _setup_mock_service(tmp_path)

    # 1. Successful run cleanup
    created_workspaces: List[Path] = []
    orig_enter = RequestTemporaryWorkspace.__enter__

    def tracking_enter(self):
        res = orig_enter(self)
        created_workspaces.append(self.path)
        return res

    with patch.object(RequestTemporaryWorkspace, "__enter__", tracking_enter):
        res = service.process_stored_page("doc_123", page_number=1)
        assert res.is_success is True

    assert len(created_workspaces) == 1
    assert not created_workspaces[0].exists()

    # 2. Failure run cleanup (Module C failure)
    created_workspaces.clear()
    mod_c.process_routed_pages.side_effect = RuntimeError("Module C fatal error")

    with patch.object(RequestTemporaryWorkspace, "__enter__", tracking_enter):
        res = service.process_stored_page("doc_123", page_number=1)
        assert res.is_failed is True

    assert len(created_workspaces) == 1
    assert not created_workspaces[0].exists()


def test_process_stored_page_stream_early_generator_close_cleans_up(tmp_path):
    """Verify closing the stream generator early triggers finally cleanup."""
    from unittest.mock import patch
    from doc_extractor.processing_service import RequestTemporaryWorkspace

    service, _, _, _, _, _ = _setup_mock_service(tmp_path)

    active_paths: List[Path] = []
    orig_enter = RequestTemporaryWorkspace.__enter__

    def tracking_enter(self):
        res = orig_enter(self)
        active_paths.append(self.path)
        return res

    with patch.object(RequestTemporaryWorkspace, "__enter__", tracking_enter):
        stream = service.process_stored_page_stream("doc_123", page_number=1)
        first_event = next(stream)
        assert first_event.step_key == "load"

        # Workspace directory was created and is active
        assert len(active_paths) == 1
        ws_path = active_paths[0]
        assert ws_path.exists()

        # Closing generator early must execute workspace.cleanup() in finally
        stream.close()
        assert not ws_path.exists()


def test_document_service_process_stored_page_boundary(tmp_path):
    """Verify DocumentService delegates process_stored_page and enforces tenant alignment."""
    from doc_extractor.api.services.document_service import DocumentService

    service, _, _, _, _, _ = _setup_mock_service(tmp_path, tenant_id="tenant_boundary")
    doc_service = DocumentService(processing_service=service, tenant_id="tenant_boundary")

    # 1. Matching tenant succeeds
    res = doc_service.process_stored_page(document_id="doc_123", page_number=1)
    assert res.is_success is True
    assert res.tenant_id == "tenant_boundary"

    # 2. Conflicting tenant parameter fails closed
    with pytest.raises(TenantMismatchError):
        doc_service.process_stored_page(
            document_id="doc_123",
            page_number=1,
            tenant_id="tenant_other",
        )

    # 3. Conflicting tenant in kwargs fails closed
    with pytest.raises(TenantMismatchError):
        doc_service.process_stored_page(
            document_id="doc_123",
            page_number=1,
            kwargs_tenant="tenant_other",  # won't trigger kw_tenant
            tenant_id="tenant_other",
        )
    with pytest.raises(TenantMismatchError):
        doc_service.process_stored_page(
            "doc_123",
            1,
            tenant_id="tenant_other",
        )

    # 4. Non-string document_id rejected
    with pytest.raises(ValueError, match="Document ID is required"):
        doc_service.process_stored_page(document_id=123, page_number=1)

    with pytest.raises(ValueError, match="Document ID is required"):
        doc_service.process_stored_page_stream(document_id=123, page_number=1)

    # 5. Stream conflicting tenant fails closed
    with pytest.raises(TenantMismatchError):
        doc_service.process_stored_page_stream(
            document_id="doc_123",
            page_number=1,
            tenant_id="tenant_other",
        )


def test_process_stored_page_non_string_document_id_rejected(tmp_path):
    """Verify non-string or empty document_id is rejected fail-closed with ValueError."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path)

    for invalid_doc_id in (None, 12345, 0, True, False, "", "   ", DocumentProcessingRequest):
        with pytest.raises(ValueError, match="Document ID is required"):
            service.process_stored_page(document_id=invalid_doc_id, page_number=1)

    req = DocumentProcessingRequest(
        file_path_or_stream=b"dummy",
        claimed_filename="doc.pdf",
        tenant_id="tenant_test",
    )
    with pytest.raises(ValueError, match="Document ID is required"):
        service.process_stored_page(document_id=req, page_number=1)


def test_process_stored_page_stream_synchronous_validation(tmp_path):
    """Verify process_stored_page_stream validates arguments synchronously before generator starts."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path, tenant_id="tenant_test")

    # 1. Invalid document_id fails synchronously without calling next()
    with pytest.raises(ValueError, match="Document ID is required"):
        service.process_stored_page_stream(document_id=123, page_number=1)

    with pytest.raises(ValueError, match="Document ID is required"):
        service.process_stored_page_stream(document_id="", page_number=1)

    # 2. Path traversal fails synchronously without calling next()
    with pytest.raises(StorageError, match="Path traversal attempt"):
        service.process_stored_page_stream(document_id="../etc/passwd", page_number=1)

    # 3. Invalid page_number fails synchronously without calling next()
    with pytest.raises(ValueError, match="Invalid page number"):
        service.process_stored_page_stream(document_id="doc_123", page_number=0)

    with pytest.raises(ValueError, match="Invalid page number"):
        service.process_stored_page_stream(document_id="doc_123", page_number=-1)

    with pytest.raises(ValueError, match="Invalid page number"):
        service.process_stored_page_stream(document_id="doc_123", page_number=True)

    # 4. Tenant mismatch fails synchronously without calling next()
    with pytest.raises(TenantMismatchError):
        service.process_stored_page_stream(
            document_id="doc_123", page_number=1, tenant_id="other_tenant"
        )


def test_process_stored_page_handoff_document_id_mismatch(tmp_path):
    """Verify retrieved handoff with mismatched document_id fails closed with MALFORMED_HANDOFF."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path, tenant_id="tenant_test")

    handoff = deepcopy(service.storage.get_handoff_payload("doc_123"))
    handoff["document_id"] = "foreign_doc_999"
    service.storage.get_handoff_payload.side_effect = lambda doc_id: handoff

    res = service.process_stored_page(document_id="doc_123", page_number=1)

    assert res.is_failed is True
    assert res.status == "failed"
    assert res.error["code"] == "MALFORMED_HANDOFF"
    assert res.error["message"] == "Module A handoff payload is malformed or invalid."
    assert "foreign_doc_999" not in str(res.error)


def test_process_stored_page_request_id_correlation(tmp_path):
    """Verify request_id is propagated to progress event data and error details."""
    service, _, _, _, _, _ = _setup_mock_service(tmp_path, tenant_id="tenant_test")

    req_id = "req_trace_abc123"
    events: List[ProcessingProgressEvent] = []
    res = service.process_stored_page(
        document_id="doc_123",
        page_number=1,
        progress_callback=events.append,
        request_id=req_id,
    )

    assert res.is_success is True
    assert len(events) > 0
    for ev in events:
        if ev.data is not None:
            assert ev.data.get("request_id") == req_id

    # Test error correlation
    error_events: List[ProcessingProgressEvent] = []
    err_res = service.process_stored_page(
        document_id="doc_123",
        page_number=99,  # non-existent page
        progress_callback=error_events.append,
        request_id=req_id,
    )
    assert err_res.is_failed is True
    assert err_res.error.get("request_id") == req_id
    assert any(ev.data and ev.data.get("request_id") == req_id for ev in error_events)


def test_real_stack_single_page_processing(tmp_path):
    """
    Real-stack integration test using real LocalDiskStorage, real ModuleAPipeline,
    real ModuleBPipeline, real ModuleCPipeline, and a 3-page synthetic PDF fixture.
    Verifies:
      - Page 2 isolation before Module B parsing
      - Byte-for-byte unmodified source PDF in storage
      - Combined markdown contains ONLY page 2
      - Module B payload contains ONLY page 2
      - No false PARTIAL_PAGE_COVERAGE flag
      - Envelope page_count correctly reports total document pages (3)
      - Whole document processing still processes all 3 pages
      - Correct evidence page number (page 2)
      - Exact expected evidence bounding-box coordinates with 90-degree rotation
      - Specialist extractor receives the upright temporary image with transposed dimensions
      - Original and preprocessed stored page-image files and handoff remain byte-for-byte unchanged
      - Hash verification of handoff JSON and all stored page artifacts before and after processing
      - Distinct OCR/parsed content for pages 1, 2, and 3
      - No text from pages 1 or 3 appears in the selected-page result
    """
    import io
    import json
    import hashlib
    from unittest.mock import MagicMock
    from PIL import Image
    from doc_extractor.contracts_c import ExecutionProvenance, ExecutionMode
    from doc_extractor.malware import MockMalwareScanner
    from doc_extractor.pipeline_b import ModuleBPipeline
    from doc_extractor.pipeline_c import ModuleCPipeline
    from doc_extractor.routing import RoutedExtractor

    storage = LocalDiskStorage(base_dir=tmp_path / "real_storage", tenant_id="tenant_real_e2e")
    pipe_a = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_real_e2e")
    pipe_b = ModuleBPipeline(storage=storage, tenant_id="tenant_real_e2e")
    pipe_c = ModuleCPipeline()
    routed = RoutedExtractor(allow_fallback=False)

    # 1. Page 2 requires 90-degree clockwise rotation
    routed.classify = MagicMock(return_value={
        "category": "structured",
        "document_type": None,
        "subtype": None,
        "language": "en",
        "confidence": None,
        "method": "vlm",
        "rotation": 90,
        "router_model": "test_router",
        "filled_by": None,
    })

    extracted_images: List[Tuple[str, Tuple[int, int]]] = []
    captured_upright_pixels: List[Tuple[int, ...]] = []
    def spy_extract(img_path, category, language, schema_cls, subtype, engine=None):
        with Image.open(img_path) as im:
            extracted_images.append((img_path, im.size))
            # Capture center pixel of the image delivered to the specialist extractor
            captured_upright_pixels.append(im.getpixel((im.width // 2, im.height // 2)))
        # Returns normalized bbox [ymin, xmin, ymax, xmax] or [x1, y1, x2, y2] on UPRIGHT image (0-1000)
        return (
            {"applicant_name": {"value": "Alice Wonderland", "bbox": [100.0, 200.0, 300.0, 400.0]}},
            ExecutionProvenance(
                mode=ExecutionMode.GENUINE_LLM,
                model_name="vlm_model",
                provider="local_test",
                details={"pages_processed": [2], "pages_failed": []},
            ),
            {"model_used": "vlm_model", "fallback_occurred": False},
        )
    routed.extract = spy_extract

    service = DocumentProcessingService(
        storage=storage,
        module_a=pipe_a,
        module_b=pipe_b,
        module_c=pipe_c,
        routed_extractor=routed,
        tenant_id="tenant_real_e2e",
    )

    # 2. Create a 3-page synthetic PDF fixture with distinct aspect ratio (200x300)
    imgs = [Image.new("RGB", (200, 300), (i * 60, 100, 100)) for i in range(3)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:])
    original_pdf_bytes = buf.getvalue()
    buf.seek(0)

    # Ingest document into storage through Module A
    handoff = pipe_a.process_document(buf, "invoice_3pages.pdf")
    doc_id = handoff.document_id
    assert handoff.page_count == 3
    assert len(handoff.pages) == 3

    # 3. Hash the stored Module A handoff JSON and all relevant stored page artifacts BEFORE processing
    def compute_storage_hashes() -> Dict[str, str]:
        h: Dict[str, str] = {}
        # Stored handoff JSON
        raw_handoff = storage.get_handoff_payload(doc_id)
        h["handoff_json"] = hashlib.sha256(json.dumps(raw_handoff, sort_keys=True).encode()).hexdigest()
        # Original stored source PDF
        pdf_path = storage.resolve_uri_to_path(f"{storage.tenant_id}/{doc_id}/source/original.pdf")
        h["original_pdf"] = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        # All page image and metadata artifacts
        for p in handoff.pages:
            orig_p = storage.resolve_uri_to_path(p.original_storage_uri)
            prep_p = storage.resolve_uri_to_path(p.preprocessed_storage_uri)
            meta_p = storage.resolve_uri_to_path(p.metadata_storage_uri)
            h[f"page_{p.page_number}_orig"] = hashlib.sha256(orig_p.read_bytes()).hexdigest()
            h[f"page_{p.page_number}_prep"] = hashlib.sha256(prep_p.read_bytes()).hexdigest()
            h[f"page_{p.page_number}_meta"] = hashlib.sha256(meta_p.read_bytes()).hexdigest()
        return h

    hashes_before = compute_storage_hashes()
    assert len(hashes_before) == 11, "Expected 11 distinct stored artifacts to be tracked"

    # 4. Spy on Module B parser.parse_page to provide distinct OCR/parsed content for each page
    original_parse_page = pipe_b.parser.parse_page
    parsed_pages: List[int] = []

    def spy_parse_page(loaded_page):
        p_no = loaded_page.page_number
        parsed_pages.append(p_no)
        layout = original_parse_page(loaded_page)
        # Unique distinct content for each page
        layout.markdown_content = f"--- Page {p_no}: EXCLUSIVE_PAGE_{p_no}_SECRET ---"
        return layout

    pipe_b.parser.parse_page = spy_parse_page

    # 5. Process ONLY page 2
    res = service.process_stored_page(document_id=doc_id, page_number=2)

    # Verify execution outcome
    assert res.is_success is True
    assert res.status in ("auto_approved", "needs_review", "reviewed", "processing")

    # Verify Module B parsed ONLY page 2
    assert parsed_pages == [2], f"Expected parser to be called ONLY for page 2, got: {parsed_pages}"

    # Verify Module B output payload contains ONLY page 2
    assert len(res.payload_b.pages) == 1
    assert res.payload_b.pages[0].page_number == 2

    # Verify distinct OCR/parsed content: only page 2 present, pages 1 and 3 strictly absent
    assert "EXCLUSIVE_PAGE_2_SECRET" in res.payload_b.combined_markdown
    assert "EXCLUSIVE_PAGE_1_SECRET" not in res.payload_b.combined_markdown
    assert "EXCLUSIVE_PAGE_3_SECRET" not in res.payload_b.combined_markdown
    assert "EXCLUSIVE_PAGE_1_SECRET" not in str(res.envelope.model_dump())
    assert "EXCLUSIVE_PAGE_3_SECRET" not in str(res.envelope.model_dump())

    # Verify that specialist extractor received the upright temporary image with transposed dimensions
    assert len(extracted_images) == 1
    upright_img_path, upright_size = extracted_images[0]
    assert "upright_90_" in Path(upright_img_path).name
    prep_p2_path = storage.resolve_uri_to_path(handoff.pages[1].preprocessed_storage_uri)
    with Image.open(prep_p2_path) as prep_im:
        prep_size = prep_im.size
    assert upright_size == (prep_size[1], prep_size[0]), "Specialist model must receive transposed upright image dimensions"

    # Verify that the specialist extractor received the actual page-2 image, not page 1 mislabelled.
    # Note: Each synthetic fixture page has a distinct solid fill colour:
    #   Page 1 R-channel ~10, Page 2 R-channel ~63, Page 3 R-channel ~121  (after CLAHE preprocessing).
    # This pixel-color check verifies page identity (confirming page 2 was routed, not page 1 or 3),
    # while rotation correctness is verified by the transposed dimensions assertion above,
    # the un-rotated evidence bounding box mapping below, and dedicated rotation unit tests.
    assert len(captured_upright_pixels) == 1
    upright_center_r = captured_upright_pixels[0][0]
    with Image.open(prep_p2_path) as p2_im:
        expected_rotated = p2_im.rotate(-90, expand=True)
        expected_center_r = expected_rotated.getpixel((expected_rotated.width // 2, expected_rotated.height // 2))[0]
    assert abs(upright_center_r - expected_center_r) <= 2, (
        f"Specialist extractor received wrong page image: upright R={upright_center_r}, "
        f"expected page-2 rotated R={expected_center_r}"
    )
    # Confirm it is NOT page 1 or page 3 by checking the R-channel distance
    prep_p1_path = storage.resolve_uri_to_path(handoff.pages[0].preprocessed_storage_uri)
    with Image.open(prep_p1_path) as p1_im:
        p1_center_r = p1_im.getpixel((p1_im.width // 2, p1_im.height // 2))[0]
    assert abs(upright_center_r - p1_center_r) > 20, (
        f"Specialist extractor received page-1 image instead of page-2: R={upright_center_r}, page-1 R={p1_center_r}"
    )

    # Verify that the rotated temporary artifact no longer exists after processing
    upright_tmp_file = Path(upright_img_path)
    assert not upright_tmp_file.exists(), (
        f"Rotated temporary artifact should have been cleaned up: {upright_tmp_file}"
    )
    # Also verify the entire temporary workspace directory was removed
    workspace_dir = upright_tmp_file.parent
    assert not workspace_dir.exists(), (
        f"Temporary workspace directory should have been cleaned up: {workspace_dir}"
    )

    # Verify rotation metadata recorded on execution
    assert res.page_results[0].rotation == 90
    assert res.routes[0]["page"] == 2

    # Verify correct evidence page number and exact bounding box coordinates
    assert "applicant_name" in res.envelope.fields
    ev = res.envelope.fields["applicant_name"].evidence
    assert ev is not None
    assert ev.page == 2, f"Expected evidence page to be 2, got {ev.page}"
    assert ev.text == "Alice Wonderland"

    # Exact expected evidence coordinates:
    # Model extracted [100.0, 200.0, 300.0, 400.0] on the 90-degree upright image
    # unrotate_bbox_norm(..., 90) -> (200.0, 700.0, 400.0, 900.0) in preprocessed normalized frame
    w_prep, h_prep = prep_size
    expected_bbox = (
        200.0 * w_prep / 1000.0,
        700.0 * h_prep / 1000.0,
        400.0 * w_prep / 1000.0,
        900.0 * h_prep / 1000.0,
    )
    assert ev.bbox == pytest.approx(expected_bbox), f"Expected evidence bbox {expected_bbox}, got {ev.bbox}"

    # Verify all 11 stored artifacts remain byte-for-byte unchanged after processing
    hashes_after = compute_storage_hashes()
    assert hashes_after == hashes_before, "Stored artifacts or handoff were modified during single-page processing!"

    # Verify canonical extraction envelope:
    # - reports the full original document page count (3)
    # - does NOT produce a false PARTIAL_PAGE_COVERAGE flag
    assert res.envelope.document_metadata.page_count == 3
    coverage_flags = [f for f in res.envelope.flags if f.code == "PARTIAL_PAGE_COVERAGE"]
    assert len(coverage_flags) == 0, f"False PARTIAL_PAGE_COVERAGE flag detected: {coverage_flags}"

    # 6. Verify whole-document processing still extracts all pages (regression check)
    buf2 = io.BytesIO(original_pdf_bytes)
    req_full = DocumentProcessingRequest(
        file_path_or_stream=buf2,
        claimed_filename="invoice_whole.pdf",
        tenant_id="tenant_real_e2e",
    )
    # Configure routed extractor for full document (pages 1, 2, 3)
    routed.extract = MagicMock(return_value=(
        {"applicant_name": {"value": "Alice Wonderland", "bbox": [10, 10, 50, 20]}},
        ExecutionProvenance(
            mode=ExecutionMode.GENUINE_LLM,
            model_name="vlm_model",
            provider="local_test",
            details={"pages_processed": [1, 2, 3], "pages_failed": []},
        ),
        {"model_used": "vlm_model", "fallback_occurred": False},
    ))
    parsed_pages.clear()
    res_full = service.process_document(req_full)
    assert res_full.is_success is True
    assert len(res_full.payload_b.pages) == 3
    assert parsed_pages == [1, 2, 3]


def test_module_b_page_selection_contract(tmp_path):
    """
    Directly test ModuleBPipeline.load_page_images(page_numbers=...) and process_handoff.
    Verifies:
      - page_numbers=None loads all pages
      - [2] selects only page 2
      - Original page numbering remains unchanged (page 2 stays 2)
      - Duplicate page selections are deduplicated ([2, 2] -> [2])
      - Invalid page numbers ([99]) return empty list
      - Corrupt unselected pages still fail full-handoff integrity validation
      - process_handoff() passes only the selected page to its parser
    """
    import io
    from PIL import Image
    from doc_extractor.malware import MockMalwareScanner
    from doc_extractor.pipeline_b import ModuleBPipeline
    from doc_extractor.exceptions import UnsupportedFileError

    storage = LocalDiskStorage(base_dir=tmp_path / "mb_storage", tenant_id="tenant_mb")
    pipe_a = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_mb")
    pipe_b = ModuleBPipeline(storage=storage, tenant_id="tenant_mb")

    # Ingest 3-page document
    imgs = [Image.new("RGB", (200, 200), (i * 50, 100, 100)) for i in range(3)]
    buf = io.BytesIO()
    imgs[0].save(buf, format="PDF", save_all=True, append_images=imgs[1:])
    buf.seek(0)
    handoff = pipe_a.process_document(buf, "mb_test.pdf")

    # 1. page_numbers=None loads all pages
    refs_all = pipe_b.load_page_images(handoff, page_numbers=None)
    assert len(refs_all) == 3
    assert [r.page_number for r in refs_all] == [1, 2, 3]

    # 2. [2] selects only page 2 with original page numbering preserved
    refs_p2 = pipe_b.load_page_images(handoff, page_numbers=[2])
    assert len(refs_p2) == 1
    assert refs_p2[0].page_number == 2
    assert refs_p2[0].page_ref.page_number == 2

    # 3. Duplicate page selection is deduplicated
    refs_dup = pipe_b.load_page_images(handoff, page_numbers=[2, 2])
    assert len(refs_dup) == 1
    assert refs_dup[0].page_number == 2

    # 4. Invalid page selection returns empty list
    refs_invalid = pipe_b.load_page_images(handoff, page_numbers=[99])
    assert refs_invalid == []

    # 5. process_handoff passes only selected page to parser
    orig_parse = pipe_b.parser.parse_page
    parsed_pages: List[int] = []

    def spy_parse(loaded_page):
        parsed_pages.append(loaded_page.page_number)
        return orig_parse(loaded_page)

    pipe_b.parser.parse_page = spy_parse
    payload_b = pipe_b.process_handoff(handoff, page_numbers=[2])

    assert parsed_pages == [2], f"Expected parser to be called ONLY for page 2, got {parsed_pages}"
    assert len(payload_b.pages) == 1
    assert payload_b.pages[0].page_number == 2

    # 6. Corrupt unselected page still fails full-handoff integrity validation
    # Delete page 3 preprocessed image file from storage
    p3_prep_file = storage.resolve_uri_to_path(handoff.pages[2].preprocessed_storage_uri)
    p3_prep_file.unlink()

    # Calling load_page_images for page 1 must fail closed even though page 3 was not requested
    with pytest.raises(UnsupportedFileError):
        pipe_b.load_page_images(handoff, page_numbers=[1])

    # Calling process_handoff for page 1 must also fail closed returning a terminal failed payload
    failed_payload = pipe_b.process_handoff(handoff, page_numbers=[1])
    assert failed_payload.status == "failed"
    assert "page 3" in str(failed_payload.error).lower()


def test_service_passes_scope_and_total_pages_to_module_c(tmp_path):
    """
    Verify DocumentProcessingService passes:
      - page_numbers=[2] to ModuleBPipeline.load_page_images
      - page_numbers=[2] to ModuleBPipeline.process_handoff
      - expected_page_numbers=[2] and total_document_pages=2 to ModuleCPipeline.process_routed_pages
    """
    service, _, mock_module_b, _, mock_module_c, _ = _setup_mock_service(tmp_path)

    # Process page 2 of doc_123 (handoff in _setup_mock_service has 2 pages)
    service.process_stored_page("doc_123", page_number=2)

    # Module B assertions
    assert mock_module_b.load_page_images.called
    assert mock_module_b.load_page_images.call_args.kwargs.get("page_numbers") == [2], (
        f"Expected Module B load_page_images page_numbers=[2], got {mock_module_b.load_page_images.call_args.kwargs.get('page_numbers')}"
    )
    assert mock_module_b.process_handoff.called
    assert mock_module_b.process_handoff.call_args.kwargs.get("page_numbers") == [2], (
        f"Expected Module B process_handoff page_numbers=[2], got {mock_module_b.process_handoff.call_args.kwargs.get('page_numbers')}"
    )

    # Module C assertions
    assert mock_module_c.process_routed_pages.called
    call_kwargs = mock_module_c.process_routed_pages.call_args.kwargs

    assert call_kwargs.get("expected_page_numbers") == [2], (
        f"Expected expected_page_numbers=[2], got {call_kwargs.get('expected_page_numbers')}"
    )
    assert call_kwargs.get("total_document_pages") == 2, (
        f"Expected total_document_pages=2, got {call_kwargs.get('total_document_pages')}"
    )
