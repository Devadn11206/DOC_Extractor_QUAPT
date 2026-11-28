"""
Phase 1 / Task 6: multipage multimodal extraction and evidence aggregation in ModuleCPipeline.

Real Module A artifacts (a 3-page PDF, a 5-page TIFF) flow through the real extractor, grounding,
aggregation, CoordinateTransform, field assembly and envelope code. Only the lowest-level model call
(SchemaGuidedExtractor._generate_from_image) is replaced by a page-aware fake, so no GPU or model
weights are needed.
"""

import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from PIL import Image

from doc_extractor.contracts_b import ModuleAPageRef, MVPDocumentType
from doc_extractor.contracts_c import ExecutionMode, ExecutionProvenance, PageImageRef
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.dynamic_adapter import DynamicExtractionAdapter
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import RawOCRToken
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.schemas import InsuranceApplicationSchema
from doc_extractor.vlm_grounding import (
    PageLeaf,
    aggregate_leaves,
    conflict_flags,
    grounding_to_evidence_hints,
)
from schema_lifecycle.enums import SchemaStatus

from tests.test_canonical_module_c_orchestration import (  # noqa: F401  (fixtures + helpers)
    TENANT,
    CASA_VLM_OUTPUT,
    _casa_ocr_tokens,
    _expected_original_bbox,
    _isolate_environment,
    _module_a,
    _tiff_bytes,
    _vlm_pipeline,
    casa_run,
    temp_storage,
)
from tests.test_confidence_integrity_and_review_gating import (  # noqa: F401
    _envelope,
    _good_field,
    _text_extractor,
)
from tests.test_dynamic_module_c_adapter import (  # noqa: F401
    build_test_schema_def,
    lifecycle_manager,
    sample_payload,
)

VLM_MODEL = "Qwen/Qwen3-VL-8B-Instruct"


# ----------------------------------------------------------------------------
# Fixtures / helpers
# ----------------------------------------------------------------------------

def _pdf_bytes(pages: int) -> bytes:
    images = [Image.new("RGB", (1000, 1400), color=(255, 255, 255)) for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, format="PDF", save_all=True, append_images=images[1:], resolution=200.0)
    return buf.getvalue()


def _doc02_tokens(img) -> List[RawOCRToken]:
    return [
        RawOCRToken(text="LIFE INSURANCE PROPOSAL FORM FOR INDIVIDUALS", bbox_preprocessed=(50, 50, 500, 100), confidence=0.98),
        RawOCRToken(text="Sum Insured INR 5000000", bbox_preprocessed=(50, 110, 350, 150), confidence=0.97),
        RawOCRToken(text="Policy Term 20 Years", bbox_preprocessed=(50, 160, 300, 200), confidence=0.96),
        RawOCRToken(text="Medical History Questionnaire", bbox_preprocessed=(50, 210, 400, 250), confidence=0.95),
        RawOCRToken(text="Nominee Name and Share", bbox_preprocessed=(50, 260, 350, 300), confidence=0.94),
    ]


class Run:
    def __init__(self, handoff, payload, refs, pipe_b):
        self.handoff, self.payload, self.refs, self.pipe_b = handoff, payload, refs, pipe_b


def _run(storage, pages: int, fmt: str = "tiff", tokens=_casa_ocr_tokens, expect=MVPDocumentType.DOC_01_CASA) -> Run:
    data = _pdf_bytes(pages) if fmt == "pdf" else _tiff_bytes(pages)
    handoff = _module_a(storage).process_document(io.BytesIO(data), f"doc.{fmt}")
    pipe_b = ModuleBPipeline(
        storage=storage, parser=LocalOCRParserAdapter(raw_extractor=tokens), tenant_id=TENANT
    )
    payload, refs = pipe_b.process_handoff(handoff), pipe_b.load_page_images(handoff)
    assert payload.classification.document_type == expect
    assert len(payload.pages) == pages and len(refs) == pages
    return Run(handoff, payload, refs, pipe_b)


class PageAwareVLM:
    """
    Fake model call keyed by *page number*: a dict is returned as JSON, a str is returned verbatim
    (e.g. "" for a blank reply), an Exception is raised.
    """

    def __init__(self, responses: Optional[Dict[int, Any]] = None, default: Any = None):
        self.responses: Dict[int, Any] = responses or {}
        self.default = {} if default is None else default
        self.calls: List[Dict[str, Any]] = []
        self._page_of: Dict[str, int] = {}

    def bind(self, refs) -> "PageAwareVLM":
        self._page_of = {str(r.image_path): r.page_number for r in refs}
        return self

    def install(self, monkeypatch) -> "PageAwareVLM":
        fake = self

        def generate(self_extractor, image_input, system_prompt, user_text, max_new_tokens):
            page = fake._page_of.get(str(image_input))
            fake.calls.append({"page": page, "image": str(image_input), "user_text": user_text,
                               "system_prompt": system_prompt})
            response = fake.responses.get(page, fake.default)
            if isinstance(response, Exception):
                raise response
            return response if isinstance(response, str) else json.dumps(response)

        monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", generate)
        return self


def _leaf(value, bbox=None) -> PageLeaf:
    return PageLeaf(value=value, bbox_norm=bbox, raw_value=value, uncertain=False)


def _process(run: Run, vlm: PageAwareVLM, monkeypatch, refs=None):
    vlm.bind(run.refs).install(monkeypatch)
    return _vlm_pipeline().process(run.payload, page_images=run.refs if refs is None else refs)


def _name(page_value: str, bbox=(120, 100, 420, 150)) -> Dict[str, Any]:
    return {"applicant.full_name": {"value": page_value, "bbox": list(bbox)}}


PAGE_FIELDS = {
    1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [120, 100, 420, 150]},
        "applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [120, 200, 320, 250]}},
    2: {"applicant.date_of_birth": {"value": "15/08/1990", "bbox": [120, 300, 320, 350]},
        "applicant.phone_number": {"value": "9876543210", "bbox": [120, 400, 320, 450]}},
    3: {"account_details.account_type": {"value": "Savings", "bbox": [120, 500, 320, 550]},
        "account_details.initial_deposit_amount": {"value": "Rs. 5,000.00", "bbox": [120, 600, 320, 650]}},
}


# ============================================================================
# 1-3. Every page is read, in order, with the right page numbers
# ============================================================================

def test_three_page_pdf_extracts_values_from_every_page(temp_storage, monkeypatch):
    run = _run(temp_storage, 3, fmt="pdf")
    vlm = PageAwareVLM(PAGE_FIELDS)

    env = _process(run, vlm, monkeypatch)

    expected_pages = {
        "applicant.full_name": 1, "applicant.pan_card_number": 1,
        "applicant.date_of_birth": 2, "applicant.phone_number": 2,
        "account_details.account_type": 3, "account_details.initial_deposit_amount": 3,
    }
    for path, page in expected_pages.items():
        field = env.fields[path]
        assert field.value is not None, path
        assert field.evidence is not None and field.evidence.page == page, path
    assert env.fields["applicant.date_of_birth"].value == "1990-08-15"        # normalized per page
    assert env.fields["account_details.initial_deposit_amount"].value == 5000.0
    details = env.run.provenance.details
    assert details["pages_attempted"] == [1, 2, 3] and details["pages_processed"] == [1, 2, 3]
    assert details["pages_failed"] == [] and details["pages_empty"] == []
    assert not any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)


def test_five_page_document_keeps_correct_page_numbering(temp_storage, monkeypatch):
    run = _run(temp_storage, 5)
    per_page = {
        1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": [100, 100, 400, 150]}},
        2: {"applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": [100, 100, 400, 150]}},
        3: {"applicant.date_of_birth": {"value": "15/08/1990", "bbox": [100, 100, 400, 150]}},
        4: {"applicant.phone_number": {"value": "9876543210", "bbox": [100, 100, 400, 150]}},
        5: {"account_details.account_type": {"value": "Savings", "bbox": [100, 100, 400, 150]}},
    }
    env = _process(run, PageAwareVLM(per_page), monkeypatch)

    pages = {
        "applicant.full_name": 1, "applicant.pan_card_number": 2, "applicant.date_of_birth": 3,
        "applicant.phone_number": 4, "account_details.account_type": 5,
    }
    for path, page in pages.items():
        assert env.fields[path].evidence.page == page, path
    assert env.run.provenance.details["pages_processed"] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("pages", [1, 3, 5])
def test_qwen_is_invoked_once_per_page_image_in_document_order(temp_storage, monkeypatch, pages):
    run = _run(temp_storage, pages)
    vlm = PageAwareVLM()
    _process(run, vlm, monkeypatch)

    assert len(vlm.calls) == pages
    assert [c["page"] for c in vlm.calls] == list(range(1, pages + 1))
    assert [Path(c["image"]) for c in vlm.calls] == [r.image_path for r in run.refs]
    if pages > 1:  # each call is told which page it is reading
        assert all(f"page {c['page']} of a {pages}-page document" in c["user_text"] for c in vlm.calls)
    else:
        assert vlm.calls[0]["user_text"].startswith("Carefully transcribe")  # single-page prompt unchanged


def test_shuffled_page_images_are_still_read_in_document_order(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM(PAGE_FIELDS)
    env = _process(run, vlm, monkeypatch, refs=[run.refs[2], run.refs[0], run.refs[1], run.refs[0]])

    assert [c["page"] for c in vlm.calls] == [1, 2, 3]  # ordered and one reference per page
    assert env.fields["applicant.phone_number"].evidence.page == 2


# ============================================================================
# 4. Page-specific coordinate transforms
# ============================================================================

def test_each_page_uses_its_own_geometry_for_coordinate_transform(temp_storage, monkeypatch):
    run = _run(temp_storage, 2)
    skewed_ref = run.refs[1].page_ref.model_copy(
        update={"applied_deskew_deg": 4.0, "preprocessed_width_px": 800, "preprocessed_height_px": 1000}
    )
    refs = [run.refs[0], PageImageRef(page_number=2, image_path=run.refs[1].image_path, page_ref=skewed_ref)]
    same_box = [120, 100, 420, 150]
    vlm = PageAwareVLM({
        1: {"applicant.full_name": {"value": "RUPESH KUMAR", "bbox": same_box}},
        2: {"applicant.pan_card_number": {"value": "ABCDE1234F", "bbox": same_box}},
    })

    seen: List[Dict[str, Any]] = []
    original = CoordinateTransform.map_bbox_preprocessed_to_original

    def spy(**kwargs):
        seen.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(CoordinateTransform, "map_bbox_preprocessed_to_original", staticmethod(spy))
    env = _process(run, vlm, monkeypatch, refs=refs)
    pipeline_calls = list(seen)  # snapshot before the expectations below call the transform too

    name_ev, pan_ev = env.fields["applicant.full_name"].evidence, env.fields["applicant.pan_card_number"].evidence
    assert name_ev.page == 1 and pan_ev.page == 2
    assert name_ev.bbox == pytest.approx(_expected_original_bbox(refs[0], same_box))
    assert pan_ev.bbox == pytest.approx(_expected_original_bbox(refs[1], same_box))
    assert name_ev.bbox != pytest.approx(pan_ev.bbox)  # same model box, different page geometry
    assert sorted(k["applied_deskew_deg"] for k in pipeline_calls) == [0.0, 4.0]


# ============================================================================
# 5-6. Identical scalars are deduplicated; conflicting scalars need review
# ============================================================================

def test_identical_scalar_across_pages_is_deduplicated_with_every_page_evidence(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM({
        1: _name("RUPESH KUMAR", (120, 100, 420, 150)),
        2: _name("RUPESH KUMAR", (200, 300, 500, 350)),
        3: _name("RUPESH KUMAR", (100, 700, 300, 750)),
    })
    env = _process(run, vlm, monkeypatch)

    field = env.fields["applicant.full_name"]
    assert field.value == "RUPESH KUMAR"
    assert field.evidence.page == 1
    assert [e.page for e in field.additional_evidence] == [2, 3]
    refs = {r.page_number: r for r in run.refs}
    assert field.additional_evidence[0].bbox == pytest.approx(_expected_original_bbox(refs[2], (200, 300, 500, 350)))
    assert field.additional_evidence[1].bbox == pytest.approx(_expected_original_bbox(refs[3], (100, 700, 300, 750)))
    assert not any(f.code == "FIELD_VALUE_CONFLICT" for f in env.flags)


def test_conflicting_scalar_is_flagged_and_never_overwrites_the_first_reading(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM({1: _name("RUPESH KUMAR"), 2: {}, 3: _name("RAKESH KUMAR", (200, 300, 500, 350))})
    env = _process(run, vlm, monkeypatch)

    field = env.fields["applicant.full_name"]
    assert field.value == "RUPESH KUMAR"                      # first reading retained, not overwritten
    assert field.evidence.page == 1
    assert [(e.page, e.text) for e in field.additional_evidence] == [(3, "RAKESH KUMAR")]
    assert field.review_required is True
    flag = next(f for f in env.flags if f.code == "FIELD_VALUE_CONFLICT")
    assert flag.fields == ["applicant.full_name"]
    assert "page 1: 'RUPESH KUMAR'" in flag.message and "page 3: 'RAKESH KUMAR'" in flag.message
    assert env.document_metadata.status == "needs_review"


def test_conflict_keeps_whichever_value_came_first_in_page_order(temp_storage, monkeypatch):
    run = _run(temp_storage, 2)
    env = _process(run, PageAwareVLM({1: _name("RAKESH KUMAR"), 2: _name("RUPESH KUMAR")}), monkeypatch)
    assert env.fields["applicant.full_name"].value == "RAKESH KUMAR"
    assert any(f.code == "FIELD_VALUE_CONFLICT" for f in env.flags)


def test_aggregation_unit_rules_dedupe_conflict_and_order():
    pages = [
        (1, {"name": _leaf("A")}),
        (2, {"name": _leaf("A")}),
        (3, {"name": _leaf("B")}),
        (4, {"name": _leaf("B")}),
    ]
    values, grounding = aggregate_leaves(pages)

    assert values == {"name": "A"}
    assert [g.page_number for g in grounding["name"].also_seen] == [2]
    assert [g.page_number for g in grounding["name"].conflicts] == [3, 4]
    assert len(conflict_flags(grounding)) == 1
    hints = grounding_to_evidence_hints(grounding, [])
    assert hints == {}  # no page images -> no boxes; aggregation itself is unaffected


# ============================================================================
# 7. Repeating groups (nominees)
# ============================================================================

def _doc02_pages() -> Dict[int, Dict[str, Any]]:
    def nom(i, name, share, y):
        return {
            f"nominees.{i}.name": {"value": name, "bbox": [100, y, 500, y + 40]},
            f"nominees.{i}.share_percentage": {"value": share, "bbox": [520, y, 600, y + 40]},
        }

    return {
        1: {"proposer_name": {"value": "ANANYA SHARMA", "bbox": [100, 50, 500, 90]},
            **nom(0, "ROHIT SHARMA", "60", 200), **nom(1, "KAVITA SHARMA", "40", 260)},
        2: nom(0, "MEERA SHARMA", "25", 300),
        3: nom(0, "ROHIT SHARMA", "60", 400),  # the first nominee again (e.g. a summary page)
    }


def test_repeating_group_entries_are_preserved_indexed_and_deduplicated(temp_storage, monkeypatch):
    run = _run(temp_storage, 3, tokens=_doc02_tokens, expect=MVPDocumentType.DOC_02_INSURANCE_APP)
    env = _process(run, PageAwareVLM(_doc02_pages()), monkeypatch)

    names = {i: env.fields[f"nominees.{i}.name"] for i in range(3)}
    assert [names[i].value for i in range(3)] == ["ROHIT SHARMA", "KAVITA SHARMA", "MEERA SHARMA"]
    assert "nominees.3.name" not in env.fields
    assert [names[i].evidence.page for i in range(3)] == [1, 1, 2]       # original page per entry
    assert [e.page for e in names[0].additional_evidence] == [3]          # duplicate entry kept as evidence
    assert env.fields["nominees.2.share_percentage"].value == 25.0
    assert env.fields["nominees.0.share_percentage"].value == 60.0
    assert not any(f.code == "FIELD_VALUE_CONFLICT" for f in env.flags)   # distinct entries are not conflicts


def test_nominees_on_separate_pages_are_not_collapsed_into_the_first(temp_storage, monkeypatch):
    run = _run(temp_storage, 2, tokens=_doc02_tokens, expect=MVPDocumentType.DOC_02_INSURANCE_APP)
    # Legacy un-indexed reading ("nominees.name") on each page used to keep only the first entry.
    vlm = PageAwareVLM({
        1: {"nominees.name": {"value": "ROHIT SHARMA", "bbox": [100, 200, 500, 240]}},
        2: {"nominees.name": {"value": "KAVITA SHARMA", "bbox": [100, 200, 500, 240]}},
    })
    env = _process(run, vlm, monkeypatch)
    assert env.fields["nominees.0.name"].value == "ROHIT SHARMA" and env.fields["nominees.0.name"].evidence.page == 1
    assert env.fields["nominees.1.name"].value == "KAVITA SHARMA" and env.fields["nominees.1.name"].evidence.page == 2


def test_aggregation_reindexes_groups_contiguously_in_page_order():
    pages = [
        (1, {"n.0.name": _leaf("A"), "n.1.name": _leaf("B"), "x": _leaf("S")}),
        (2, {"n.0.name": _leaf("C")}),
        (3, {"n.2.name": _leaf("B"), "n.0.name": _leaf("A")}),   # B and A repeat (any model index)
    ]
    values, grounding = aggregate_leaves(pages)

    assert [values[f"n.{i}.name"] for i in range(3)] == ["A", "B", "C"]
    assert [grounding[f"n.{i}.name"].page_number for i in range(3)] == [1, 1, 2]
    assert [g.page_number for g in grounding["n.0.name"].also_seen] == [3]
    assert [g.page_number for g in grounding["n.1.name"].also_seen] == [3]
    assert values["x"] == "S"


def test_group_entries_that_differ_in_any_subfield_are_kept_separate_never_merged():
    pages = [
        (1, {"n.0.name": _leaf("A"), "n.0.share": _leaf(60.0)}),
        (2, {"n.0.name": _leaf("A"), "n.0.share": _leaf(40.0)}),
    ]
    values, _ = aggregate_leaves(pages)
    assert values["n.0.share"] == 60.0 and values["n.1.share"] == 40.0
    assert values["n.0.name"] == values["n.1.name"] == "A"


def test_extraction_prompt_requests_indexed_repeating_groups():
    prompt = SchemaGuidedExtractor()._build_system_prompt(InsuranceApplicationSchema)
    assert '"nominees.0.name"' in prompt and "nominees.1.*" in prompt
    assert "REPEATING GROUPS" in prompt


# ============================================================================
# 8-10. Failure handling and page coverage
# ============================================================================

def test_one_failing_page_keeps_the_other_pages_and_flags_partial_coverage(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM({1: PAGE_FIELDS[1], 2: RuntimeError("CUDA out of memory"), 3: PAGE_FIELDS[3]})
    env = _process(run, vlm, monkeypatch)

    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"
    assert env.fields["account_details.account_type"].evidence.page == 3
    assert env.fields["applicant.date_of_birth"].value is None            # page 2 values are not invented
    details = env.run.provenance.details
    assert details["pages_processed"] == [1, 3]
    assert [f["page"] for f in details["pages_failed"]] == [2]
    assert "CUDA out of memory" in details["pages_failed"][0]["reason"]
    flag = next(f for f in env.flags if f.code == "PARTIAL_PAGE_COVERAGE")
    assert "page 2" in flag.message and "CUDA out of memory" in flag.message and "[1, 3] of 3" in flag.message
    assert env.document_metadata.status == "needs_review"


def test_missing_page_image_reference_is_recorded_and_flagged(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM(PAGE_FIELDS)
    env = _process(run, vlm, monkeypatch, refs=[run.refs[0], run.refs[2]])   # no reference for page 2

    assert [c["page"] for c in vlm.calls] == [1, 3]
    details = env.run.provenance.details
    assert details["pages_failed"] == [{"page": 2, "reason": "no page image reference supplied"}]
    flag = next(f for f in env.flags if f.code == "PARTIAL_PAGE_COVERAGE")
    assert "page 2 (no page image reference supplied)" in flag.message
    assert env.document_metadata.status == "needs_review"


def test_reference_to_a_missing_image_file_fails_only_that_page(temp_storage, monkeypatch, tmp_path):
    run = _run(temp_storage, 3)
    ghost = PageImageRef(page_number=2, image_path=tmp_path / "gone.png", page_ref=run.refs[1].page_ref)
    vlm = PageAwareVLM(PAGE_FIELDS)
    env = _process(run, vlm, monkeypatch, refs=[run.refs[0], ghost, run.refs[2]])

    assert [c["page"] for c in vlm.calls] == [1, 3]
    assert "page image does not exist" in env.run.provenance.details["pages_failed"][0]["reason"]
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)


def test_empty_but_successfully_processed_page_is_not_a_failure(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM({
        1: PAGE_FIELDS[1],
        2: {},                                                    # valid JSON, nothing on this page
        3: {"account_details.account_type": {"value": None, "bbox": None}},  # all-null page
    })
    env = _process(run, vlm, monkeypatch)

    details = env.run.provenance.details
    assert details["pages_processed"] == [1, 2, 3]
    assert details["pages_empty"] == [2, 3] and details["pages_failed"] == []
    assert not any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)


@pytest.mark.parametrize("label,response,reason_fragment", [
    ("blank reply", "", "empty model response"),
    # v1.5 parse_json_tolerant: prose or a bare JSON array contains no JSON object to recover
    ("not JSON", "I cannot read this page", "no JSON object"),
    ("JSON but not an object", [1, 2, 3], "no JSON object"),
    ("values violating the schema", {"account_details.initial_deposit_amount": {"value": "abc", "bbox": None}},
     "does not satisfy schema"),
])
def test_invalid_page_output_fails_that_page_but_preserves_valid_pages(
    temp_storage, monkeypatch, label, response, reason_fragment
):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM({1: PAGE_FIELDS[1], 2: response, 3: PAGE_FIELDS[3]})
    env = _process(run, vlm, monkeypatch)

    failed = env.run.provenance.details["pages_failed"]
    assert [f["page"] for f in failed] == [2], label
    assert reason_fragment in failed[0]["reason"], label
    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"
    assert env.fields["account_details.account_type"].value == "Savings"
    assert any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)


def test_all_pages_failing_raises_and_uses_the_existing_fallback_policy(temp_storage, monkeypatch):
    run = _run(temp_storage, 2)
    vlm = PageAwareVLM(default=RuntimeError("model unavailable"))
    vlm.bind(run.refs).install(monkeypatch)

    from doc_extractor.exceptions import LLMExecutionError

    with pytest.raises(LLMExecutionError, match="No page could be processed"):
        _vlm_pipeline().process(run.payload, page_images=run.refs)

    fallback = SchemaGuidedExtractor(
        model_name="Qwen/Qwen3-4B-Instruct-2507", use_llm=True, allow_fallback=False,
        llm_caller=lambda payload, schema_cls: ({"applicant": {"full_name": "CASA Application"}}, "req-fb"),
    )
    env = ModuleCPipeline(extractor=_vlm_pipeline().extractor, fallback_extractor=fallback).process(
        run.payload, page_images=run.refs
    )
    assert env.run.provenance.fallback_occurred is True


# ============================================================================
# 14. PARTIAL_PAGE_COVERAGE prevents automatic approval
# ============================================================================

def test_partial_page_coverage_prevents_automatic_approval():
    prov = ExecutionProvenance(
        mode=ExecutionMode.GENUINE_LLM, model_name="m",
        details={"pages_processed": [1, 2], "pages_failed": [{"page": 3, "reason": "timeout"}]},
    )
    flag = ModuleCPipeline.partial_coverage_flag(prov, [1, 2, 3], [1, 2, 3])
    assert flag is not None and "page 3 (timeout)" in flag.message

    complete = ExecutionProvenance(
        mode=ExecutionMode.GENUINE_LLM, model_name="m",
        details={"pages_processed": [1, 2, 3], "pages_failed": [], "pages_empty": [2]},
    )
    assert ModuleCPipeline.partial_coverage_flag(complete, [1, 2, 3], [1, 2, 3]) is None

    fields = {"a": _good_field("A"), "b": _good_field("B")}     # fully grounded, measured confidence
    assert _envelope(fields).document_metadata.status == "auto_approved"
    assert _envelope(fields, flags=[flag]).document_metadata.status == "needs_review"


def test_text_extraction_never_reports_partial_coverage(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM().bind(run.refs)

    def forbidden(*args, **kwargs):
        raise AssertionError("text extractor must not invoke the VLM")

    monkeypatch.setattr(SchemaGuidedExtractor, "_generate_from_image", forbidden)
    env = ModuleCPipeline(extractor=_text_extractor({"applicant": {"full_name": "CASA Application"}})).process(
        run.payload, page_images=run.refs
    )
    assert "pages_processed" not in env.run.provenance.details
    assert not any(f.code == "PARTIAL_PAGE_COVERAGE" for f in env.flags)
    assert vlm.calls == []


# ============================================================================
# 11. Single-page compatibility
# ============================================================================

def test_single_page_extraction_behaves_as_before(casa_run, monkeypatch):
    vlm = PageAwareVLM({1: dict(CASA_VLM_OUTPUT)}).bind(casa_run["page_images"]).install(monkeypatch)
    env = _vlm_pipeline().process(casa_run["payload"], page_images=casa_run["page_images"])

    assert len(vlm.calls) == 1 and vlm.calls[0]["user_text"].startswith("Carefully transcribe")
    assert env.fields["applicant.full_name"].value == "RUPESH KUMAR"
    assert env.fields["applicant.full_name"].additional_evidence == []
    assert env.run.provenance.details["pages_processed"] == [1]
    assert env.run.provenance.details["pages_failed"] == []
    assert not any(f.code in ("PARTIAL_PAGE_COVERAGE", "FIELD_VALUE_CONFLICT") for f in env.flags)


def test_single_page_group_mapping_still_reads_unindexed_nominee(temp_storage, monkeypatch):
    run = _run(temp_storage, 1, tokens=_doc02_tokens, expect=MVPDocumentType.DOC_02_INSURANCE_APP)
    vlm = PageAwareVLM({1: {"nominees.name": {"value": "ROHIT SHARMA", "bbox": [100, 200, 500, 240]}}})
    env = _process(run, vlm, monkeypatch)
    assert env.fields["nominees.0.name"].value == "ROHIT SHARMA"


def test_blank_model_reply_is_an_error_but_empty_json_object_is_valid():
    assert SchemaGuidedExtractor._parse_json_output("{}") == {}
    assert SchemaGuidedExtractor._parse_json_output("```json\n{}\n```") == {}
    with pytest.raises(ValueError, match="empty model response"):
        SchemaGuidedExtractor._parse_json_output("   ")


# ============================================================================
# 13. Generic and dynamic-schema paths
# ============================================================================

GENERIC_PAGES = {
    1: {"title": {"value": "Gold Shield Plan", "bbox": [100, 50, 600, 120]},
        "offer": {"value": "10% off", "bbox": [100, 200, 500, 260]}},
    2: {"contact": {"value": "1800-123-456", "bbox": [100, 700, 500, 760]}},
    3: {"title": {"value": "Gold Shield Plan", "bbox": [100, 60, 600, 130]},
        "offer": {"value": "15% off", "bbox": [100, 210, 500, 270]}},
}


def test_generic_path_aggregates_all_pages_with_dedupe_and_conflicts(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM(GENERIC_PAGES).bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process_generic(run.payload, run.refs, "GENERIC PROMPT", document_type="generic")

    assert [c["page"] for c in vlm.calls] == [1, 2, 3]
    assert all(c["system_prompt"] == "GENERIC PROMPT" for c in vlm.calls)
    assert env.fields["title"].evidence.page == 1
    assert [e.page for e in env.fields["title"].additional_evidence] == [3]
    assert env.fields["contact"].evidence.page == 2
    assert env.fields["offer"].value == "10% off"                         # first reading retained
    assert [e.text for e in env.fields["offer"].additional_evidence] == ["15% off"]
    assert [f.fields for f in env.flags if f.code == "FIELD_VALUE_CONFLICT"] == [["offer"]]
    assert env.document_metadata.status == "needs_review"


def test_generic_path_flags_a_failed_page(temp_storage, monkeypatch):
    run = _run(temp_storage, 3)
    vlm = PageAwareVLM({1: GENERIC_PAGES[1], 2: RuntimeError("timeout"), 3: GENERIC_PAGES[3]})
    vlm.bind(run.refs).install(monkeypatch)

    env = _vlm_pipeline().process_generic(run.payload, run.refs, "P", document_type="generic")
    flag = next(f for f in env.flags if f.code == "PARTIAL_PAGE_COVERAGE")
    assert "page 2 (RuntimeError: timeout)" in flag.message
    assert env.fields["title"].value == "Gold Shield Plan"


def test_dynamic_adapter_aggregates_pages_with_page_specific_evidence(
    lifecycle_manager, sample_payload, temp_storage, monkeypatch
):
    lifecycle_manager.create_schema("tenant_alpha", build_test_schema_def(), author="alice")
    lifecycle_manager.transition_status("tenant_alpha", "kyc_dynamic_schema", 1, SchemaStatus.TESTING, actor="alice")
    run = _run(temp_storage, 2)

    vlm = PageAwareVLM({
        1: {"applicant.full_name": {"value": "ANANYA SHARMA", "bbox": [100, 700, 500, 760]},
            "required_missing_field": {"value": "PROVIDED", "bbox": [100, 800, 500, 860]},
            "nominees.name": {"value": "ROHIT SHARMA", "bbox": [100, 880, 500, 930]}},
        2: {"nominees.0.name": {"value": "KAVITA SHARMA", "bbox": [100, 100, 500, 150]}},
    }).bind(run.refs).install(monkeypatch)

    adapter = DynamicExtractionAdapter(
        manager=lifecycle_manager,
        extractor=SchemaGuidedExtractor(model_name=VLM_MODEL, use_llm=True, allow_fallback=False),
    )
    env = adapter.extract_and_assemble("tenant_alpha", "kyc_dynamic_schema", 1, sample_payload, page_images=run.refs)

    assert len(vlm.calls) == 2
    assert env.fields["nominees.0.name"].value == "ROHIT SHARMA" and env.fields["nominees.0.name"].evidence.page == 1
    assert env.fields["nominees.1.name"].value == "KAVITA SHARMA"
    assert env.fields["nominees.1.name"].evidence.page == 2
    assert env.fields["nominees.1.name"].evidence.bbox == pytest.approx(
        _expected_original_bbox(run.refs[1], [100, 100, 500, 150])
    )
    assert env.run.provenance.details["pages_processed"] == [1, 2]
    assert env.document_metadata.schema_version == 1
