"""
Focused Unit and Integration Test Suite for Module B Phase 1:
Contracts and Secure Input Loading.
"""

import io
from pathlib import Path
import tempfile
from PIL import Image
import pytest
from pydantic import ValidationError

from doc_extractor.contracts_b import (
    MODULE_B_CONTRACT_VERSION,
    BoundingBox,
    CheckboxMark,
    ClassificationOutput,
    HTMLTable,
    LayoutBlock,
    LayoutElementType,
    MVPClassificationDomain,
    MVPDocumentType,
    ModuleBInputPayload,
    ModuleBOutputPayload,
    PageLayoutOutput,
)
from doc_extractor.exceptions import (
    DocumentProcessingError,
    StorageError,
    UnsupportedFileError,
)
from doc_extractor.handoff import CoordinateTransform, ModuleAHandoffPayload, PageHandoffRecord
from doc_extractor.loader_b import LoadedPageImage, ModuleBLoadResult, SecureModuleBLoader
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.pipeline import ModuleAPipeline
from doc_extractor.storage import LocalDiskStorage


# ============================================================================
# Contract Serialization & Model Validation Tests
# ============================================================================

def test_contract_valid_bounding_box():
    bbox = BoundingBox(x0=10.0, y0=20.0, x1=100.0, y1=200.0)
    assert bbox.to_tuple() == (10.0, 20.0, 100.0, 200.0)

    # Inverted bounds must fail
    with pytest.raises(ValidationError):
        BoundingBox(x0=100.0, y0=20.0, x1=10.0, y1=200.0)

    with pytest.raises(ValidationError):
        BoundingBox(x0=10.0, y0=200.0, x1=100.0, y1=20.0)

    # Negative bounds must fail
    with pytest.raises(ValidationError):
        BoundingBox(x0=-5.0, y0=10.0, x1=50.0, y1=100.0)


def test_contract_unsupported_version():
    with pytest.raises(ValidationError) as exc:
        ModuleBInputPayload(
            contract_version="2.0.0",  # Unsupported major version
            run_id="run_1",
            document_id="doc_1",
            tenant_id="tenant_1",
            status="processing",
            internal_state="ready_for_extraction",
            claimed_filename="test.pdf",
            file_hash_sha256="abc",
            file_size_bytes=100,
            page_count=1,
            mime_type="application/pdf",
            created_at="2026-10-29T12:00:00Z",
            completed_at="2026-10-29T12:00:01Z",
        )
    assert "Unsupported Module A contract version" in str(exc.value)


def test_contract_invalid_processing_status_or_state():
    # If status is not 'processing', reject
    with pytest.raises(ValidationError):
        ModuleBInputPayload(
            contract_version="1.0.0",
            run_id="run_1",
            document_id="doc_1",
            tenant_id="tenant_1",
            status="failed",
            internal_state="ready_for_extraction",
            claimed_filename="test.pdf",
            file_hash_sha256="abc",
            file_size_bytes=100,
            page_count=1,
            mime_type="application/pdf",
            created_at="2026-10-29T12:00:00Z",
            completed_at="2026-10-29T12:00:01Z",
        )

    # If internal_state is not 'ready_for_extraction', reject
    with pytest.raises(ValidationError):
        ModuleBInputPayload(
            contract_version="1.0.0",
            run_id="run_1",
            document_id="doc_1",
            tenant_id="tenant_1",
            status="processing",
            internal_state="failed",
            claimed_filename="test.pdf",
            file_hash_sha256="abc",
            file_size_bytes=100,
            page_count=1,
            mime_type="application/pdf",
            created_at="2026-10-29T12:00:00Z",
            completed_at="2026-10-29T12:00:01Z",
        )


def test_contract_optical_confidence_bounds():
    # OCR optical confidence must be within [0.0, 1.0]
    bbox = BoundingBox(x0=0, y0=0, x1=10, y1=10)
    valid_block = LayoutBlock(
        block_id="b1",
        element_type=LayoutElementType.PARAGRAPH,
        text="Sample text",
        ocr_confidence=0.95,
        bbox_preprocessed=bbox,
        bbox_original=bbox,
        reading_order_index=0,
    )
    assert valid_block.ocr_confidence == 0.95

    with pytest.raises(ValidationError):
        LayoutBlock(
            block_id="b2",
            element_type=LayoutElementType.PARAGRAPH,
            text="Invalid",
            ocr_confidence=1.05,  # Exceeds 1.0
            bbox_preprocessed=bbox,
            bbox_original=bbox,
            reading_order_index=1,
        )

    with pytest.raises(ValidationError):
        LayoutBlock(
            block_id="b3",
            element_type=LayoutElementType.PARAGRAPH,
            text="Invalid",
            ocr_confidence=-0.1,  # Sub-zero
            bbox_preprocessed=bbox,
            bbox_original=bbox,
            reading_order_index=1,
        )


def test_contract_unknown_and_low_confidence_classification_routing():
    # 1. UNKNOWN type cannot bind schema
    with pytest.raises(ValidationError) as exc:
        ClassificationOutput(
            domain=MVPClassificationDomain.UNKNOWN,
            document_type=MVPDocumentType.UNKNOWN,
            classification_confidence=0.45,
            schema_id="DOC-01",  # Forbidden on UNKNOWN
            schema_version="1.0.0",
        )
    assert "UNKNOWN document type cannot bind an extraction schema" in str(exc.value)

    # 2. Valid UNKNOWN automatically forces review_required=True
    unrecog = ClassificationOutput(
        domain=MVPClassificationDomain.UNKNOWN,
        document_type=MVPDocumentType.UNKNOWN,
        classification_confidence=0.45,
    )
    assert unrecog.review_required is True
    assert unrecog.schema_id is None

    # 3. Output payload status must be 'needs_review' when review_required is True
    with pytest.raises(ValidationError) as exc:
        ModuleBOutputPayload(
            contract_version=MODULE_B_CONTRACT_VERSION,
            run_id="run_1",
            document_id="doc_1",
            tenant_id="t1",
            status="processing",  # Incompatible with review_required=True
            classification=unrecog,
            combined_markdown="# Sample",
            parser_model_name="mock_parser",
            classifier_model_name="mock_classifier",
            processing_time_ms=120.0,
            created_at="2026-10-29T12:00:00Z",
            completed_at="2026-10-29T12:00:01Z",
        )
    assert "Document requires review; status cannot remain 'processing'" in str(exc.value)

    # Valid routing to 'needs_review'
    valid_review_envelope = ModuleBOutputPayload(
        contract_version=MODULE_B_CONTRACT_VERSION,
        run_id="run_1",
        document_id="doc_1",
        tenant_id="t1",
        status="needs_review",
        classification=unrecog,
        combined_markdown="# Sample",
        parser_model_name="mock_parser",
        classifier_model_name="mock_classifier",
        processing_time_ms=120.0,
        created_at="2026-10-29T12:00:00Z",
        completed_at="2026-10-29T12:00:01Z",
    )
    assert valid_review_envelope.status == "needs_review"


# ============================================================================
# Secure Loader & Integration Tests
# ============================================================================

def test_loader_successful_module_a_integration(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_load")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_load")

    # Generate synthetic 2-page document via Module A
    img1 = Image.new("RGB", (100, 150), color="blue")
    img2 = Image.new("RGB", (200, 250), color="green")
    buf = io.BytesIO()
    img1.save(buf, format="TIFF", save_all=True, append_images=[img2])

    module_a_payload = pipeline.process_document(io.BytesIO(buf.getvalue()), "input.tiff")
    assert module_a_payload.status == "processing"
    assert len(module_a_payload.pages) == 2

    # Load through SecureModuleBLoader
    loader = SecureModuleBLoader(storage=storage)
    load_result = loader.load_handoff(module_a_payload, expected_tenant_id="tenant_load")

    assert isinstance(load_result, ModuleBLoadResult)
    assert load_result.document_id == module_a_payload.document_id
    assert len(load_result.pages) == 2

    # Verify loaded pages
    p1 = load_result.pages[0]
    assert p1.page_number == 1
    assert p1.width_px == 100
    assert p1.height_px == 150
    assert p1.image_bgr.shape == (150, 100, 3)

    p2 = load_result.pages[1]
    assert p2.page_number == 2
    assert p2.width_px == 200
    assert p2.height_px == 250
    assert p2.image_bgr.shape == (250, 200, 3)


def test_loader_rejects_tenant_mismatch(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_alice")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_alice")

    buf = io.BytesIO()
    Image.new("RGB", (50, 50), color="white").save(buf, format="PNG")
    payload = pipeline.process_document(io.BytesIO(buf.getvalue()), "doc.png")

    loader = SecureModuleBLoader(storage=storage)
    with pytest.raises(StorageError) as exc:
        loader.load_handoff(payload, expected_tenant_id="tenant_bob")  # Different tenant
    assert "Tenant ID mismatch" in exc.value.message


def test_loader_rejects_cross_tenant_and_traversal_uris(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_secure")
    loader = SecureModuleBLoader(storage=storage)

    # 1. Payload referencing a sibling tenant's artifact
    malicious_payload = {
        "contract_version": "1.0.0",
        "run_id": "run_hack",
        "document_id": "doc_hack",
        "tenant_id": "tenant_secure",
        "status": "processing",
        "internal_state": "ready_for_extraction",
        "claimed_filename": "hack.png",
        "file_hash_sha256": "hash",
        "file_size_bytes": 100,
        "page_count": 1,
        "mime_type": "image/png",
        "pages": [
            {
                "page_number": 1,
                "original_artifact_id": "art_1",
                "preprocessed_artifact_id": "prep_1",
                "original_storage_uri": "tenant_secure/doc_hack/pages/page_0001.png",
                "preprocessed_storage_uri": "tenant_victim/doc_hack/preprocessed/page_0001_preprocessed.png",
                "original_width_px": 100,
                "original_height_px": 100,
                "preprocessed_width_px": 100,
                "preprocessed_height_px": 100,
                "dpi": 200,
                "applied_rotation_deg": 0,
                "applied_deskew_deg": 0.0,
                "residual_skew_deg": 0.0,
                "contrast_method": "none",
                "metadata_storage_uri": "tenant_secure/doc_hack/meta.json",
            }
        ],
        "created_at": "2026-10-29T12:00:00Z",
        "completed_at": "2026-10-29T12:00:01Z",
    }

    with pytest.raises(StorageError) as exc:
        loader.load_handoff(malicious_payload, expected_tenant_id="tenant_secure")
    assert "Cross-tenant access or path escape forbidden" in exc.value.message


def test_loader_rejects_missing_image_file(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_missing")
    loader = SecureModuleBLoader(storage=storage)

    payload = {
        "contract_version": "1.0.0",
        "run_id": "run_m",
        "document_id": "doc_m",
        "tenant_id": "tenant_missing",
        "status": "processing",
        "internal_state": "ready_for_extraction",
        "claimed_filename": "doc.png",
        "file_hash_sha256": "hash",
        "file_size_bytes": 100,
        "page_count": 1,
        "mime_type": "image/png",
        "pages": [
            {
                "page_number": 1,
                "original_artifact_id": "art_1",
                "preprocessed_artifact_id": "prep_1",
                "original_storage_uri": "tenant_missing/doc_m/pages/page_0001.png",
                "preprocessed_storage_uri": "tenant_missing/doc_m/preprocessed/non_existent.png",
                "original_width_px": 100,
                "original_height_px": 100,
                "preprocessed_width_px": 100,
                "preprocessed_height_px": 100,
                "dpi": 200,
                "applied_rotation_deg": 0,
                "applied_deskew_deg": 0.0,
                "residual_skew_deg": 0.0,
                "contrast_method": "none",
                "metadata_storage_uri": "tenant_missing/doc_m/meta.json",
            }
        ],
        "created_at": "2026-10-29T12:00:00Z",
        "completed_at": "2026-10-29T12:00:01Z",
    }

    with pytest.raises(UnsupportedFileError) as exc:
        loader.load_handoff(payload, expected_tenant_id="tenant_missing")
    assert "does not exist in storage" in exc.value.message


def test_loader_rejects_dimension_mismatch(tmp_path):
    storage = LocalDiskStorage(tmp_path, tenant_id="tenant_dim")
    pipeline = ModuleAPipeline(scanner=MockMalwareScanner(), storage=storage, tenant_id="tenant_dim")

    buf = io.BytesIO()
    Image.new("RGB", (100, 100), color="yellow").save(buf, format="PNG")
    payload = pipeline.process_document(io.BytesIO(buf.getvalue()), "doc.png")

    # Tamper with claimed metadata dimensions
    data = payload.to_dict()
    data["pages"][0]["preprocessed_width_px"] = 999  # Mismatch!

    loader = SecureModuleBLoader(storage=storage)
    with pytest.raises(DocumentProcessingError) as exc:
        loader.load_handoff(data, expected_tenant_id="tenant_dim")
    assert exc.value.code == "IMAGE_DIMENSION_MISMATCH"


def test_coordinate_transform_integration_with_contracts():
    # Verify that CoordinateTransform integrates seamlessly with BoundingBox models
    orig_w, orig_h = 1000, 2000
    rot = 90
    deskew = 0.0

    # Detected box on 90° clockwise preprocessed image
    bbox_prep = BoundingBox(x0=1799.0, y0=100.0, x1=1899.0, y1=200.0)

    # Invert using Module A CoordinateTransform
    mapped_coords = CoordinateTransform.map_bbox_preprocessed_to_original(
        bbox=bbox_prep.to_tuple(),
        original_width=orig_w,
        original_height=orig_h,
        applied_rotation_deg=rot,
        applied_deskew_deg=deskew,
    )

    bbox_orig = BoundingBox.from_tuple(mapped_coords)
    assert bbox_orig.x0 == 100.0
    assert bbox_orig.y0 == 100.0
    assert bbox_orig.x1 == 200.0
    assert bbox_orig.y1 == 200.0

