"""
Phase 1 / Task 8A (D7): a human reviewer override is a human decision, not model/rule confidence.

Before: an override (FR-CLS-004) set ``classification_confidence=1.0`` labelled RULE_HEURISTIC, i.e.
it looked like a deterministic-rule score. Now it carries ``ConfidenceProvenance.HUMAN_OVERRIDE``.
Automatic classification keeps RULE_HEURISTIC; no extraction confidence is ever derived from an
override, and the review/override functionality is unchanged.
"""

import pytest

from doc_extractor.contracts_b import (
    ConfidenceProvenance,
    MVPClassificationDomain,
    MVPDocumentType,
)
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.pipeline_b import ModuleBPipeline, ReviewerOverrideRequest

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    _isolate_environment,
    temp_storage as canonical_storage,
)
from tests.test_module_b_stage4c_pipeline import _create_synthetic_handoff_files, temp_storage
from tests.test_multipage_extraction import PageAwareVLM, _run, _vlm_pipeline


def _unknown_payload(storage):
    pipeline = ModuleBPipeline(
        storage=storage, parser=LocalOCRParserAdapter(raw_extractor=lambda img: []), tenant_id="tenant-test"
    )
    payload = pipeline.process_handoff(_create_synthetic_handoff_files(storage, ["Page 1"]))
    assert payload.classification.document_type == MVPDocumentType.UNKNOWN
    return pipeline, payload


def _request(payload, new_type):
    return ReviewerOverrideRequest(
        document_id=payload.document_id,
        tenant_id="tenant-test",
        reviewer_id="reviewer_alice",
        new_document_type=new_type,
        override_reason="Header faint but clearly a CASA form",
    )


def test_human_override_has_its_own_provenance_distinct_from_model_and_rule_scores():
    values = {p.value for p in ConfidenceProvenance}
    assert "human_override" in values
    assert ConfidenceProvenance.HUMAN_OVERRIDE not in (
        ConfidenceProvenance.RULE_HEURISTIC, ConfidenceProvenance.MODEL, ConfidenceProvenance.OCR,
    )


def test_override_to_a_supported_type_is_labelled_human_override(temp_storage):
    pipeline, orig = _unknown_payload(temp_storage)

    out = pipeline.apply_reviewer_override(orig, _request(orig, MVPDocumentType.DOC_01_CASA)).classification

    assert out.confidence_provenance == ConfidenceProvenance.HUMAN_OVERRIDE
    assert out.confidence_provenance != ConfidenceProvenance.RULE_HEURISTIC
    assert out.classifier_identity == "reviewer_override:reviewer_alice"
    assert "reviewer_alice" in out.classification_reasons[0] and "faint" in out.classification_reasons[0]
    # existing override behaviour is preserved
    assert out.document_type == MVPDocumentType.DOC_01_CASA and out.domain == MVPClassificationDomain.BFSI
    assert out.schema_id == "DOC-01" and out.review_required is False


def test_reaffirming_unknown_is_labelled_human_override_and_still_requires_review(temp_storage):
    pipeline, orig = _unknown_payload(temp_storage)

    out = pipeline.apply_reviewer_override(orig, _request(orig, MVPDocumentType.UNKNOWN))

    assert out.classification.confidence_provenance == ConfidenceProvenance.HUMAN_OVERRIDE
    assert out.classification.review_required is True and out.classification.schema_id is None
    assert out.status == "needs_review"


def test_automatic_classification_still_reports_rule_heuristic_provenance(canonical_storage):
    run = _run(canonical_storage, 1)
    cls = run.payload.classification
    assert cls.document_type == MVPDocumentType.DOC_01_CASA
    assert cls.confidence_provenance == ConfidenceProvenance.RULE_HEURISTIC
    assert not cls.classifier_identity or "reviewer_override" not in cls.classifier_identity


def test_override_never_creates_extraction_confidence_or_approval(temp_storage, monkeypatch):
    """A human choosing the document type does not make the VLM-read fields trustworthy."""
    pipeline, orig = _unknown_payload(temp_storage)
    overridden = pipeline.apply_reviewer_override(orig, _request(orig, MVPDocumentType.DOC_01_CASA))
    assert overridden.classification.confidence_provenance == ConfidenceProvenance.HUMAN_OVERRIDE

    # Real page images for this handoff drive the multimodal extractor.
    refs = pipeline.load_page_images(_create_synthetic_handoff_files(temp_storage, ["Page 1"]))
    PageAwareVLM({1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]}}}
                 ).bind(refs).install(monkeypatch)
    env = _vlm_pipeline().process(overridden, page_images=refs)

    field = env.fields["applicant.full_name"]
    assert field.value == "RUPESH KUMAR"
    assert field.confidence is None and field.review_required is True
    assert env.document_metadata.status == "needs_review"
