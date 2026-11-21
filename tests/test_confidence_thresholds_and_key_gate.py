"""FR-CNF-002 thresholds, FR-CNF-004 key-field gate, and model-probability plumbing (FR-CNF-001)."""
from doc_extractor.contracts_b import ConfidenceProvenance
from doc_extractor.contracts_c import ExtractedField
from doc_extractor.field_assembly import (
    DEFAULT_FIELD_CONFIDENCE_THRESHOLD,
    HANDWRITTEN_FIELD_CONFIDENCE_THRESHOLD,
    KEY_FIELD_CONFIDENCE_THRESHOLD,
    ReviewReason,
    assess_review_requirement,
    resolve_field_threshold,
)
from doc_extractor.vlm_grounding import _grounding_from, _valid_probability, collect_generic_leaves


def _field(conf, prov=ConfidenceProvenance.MODEL, value="X"):
    return ExtractedField(value=value, confidence=conf, confidence_provenance=prov, null_reason=None,
                          review_required=False, validation="passed", evidence=None)


def test_threshold_classes():
    assert resolve_field_threshold(is_key_field=True) == KEY_FIELD_CONFIDENCE_THRESHOLD == 0.95
    assert resolve_field_threshold(is_handwritten=True) == HANDWRITTEN_FIELD_CONFIDENCE_THRESHOLD == 0.75
    assert resolve_field_threshold() == DEFAULT_FIELD_CONFIDENCE_THRESHOLD       # printed default is env-configurable
    assert resolve_field_threshold(is_key_field=True, explicit=0.9) == 0.9       # per-field schema value wins


def test_key_field_below_bar_forces_review_even_when_the_field_itself_passed():
    fields = {"pan": _field(0.93), "city": _field(0.99)}
    a = assess_review_requirement("DOC-01", fields, [], key_fields={"pan"})
    assert a.requires_review
    assert ("pan", (ReviewReason.KEY_FIELD_CONFIDENCE_LOW,)) in a.field_reasons


def test_key_field_at_or_above_bar_and_non_key_low_do_not_trigger_the_key_rule():
    ok = assess_review_requirement("DOC-01", {"pan": _field(0.95), "city": _field(0.80)}, [], key_fields={"pan"})
    assert not ok.requires_review
    none_conf = assess_review_requirement(
        "DOC-01", {"pan": _field(None, ConfidenceProvenance.UNAVAILABLE)}, [], key_fields={"pan"})
    assert ReviewReason.KEY_FIELD_CONFIDENCE_LOW in dict(none_conf.field_reasons)["pan"]


def test_no_key_fields_declared_leaves_the_gate_unchanged():
    assert not assess_review_requirement("DOC-01", {"a": _field(0.80)}, []).requires_review


def test_model_probability_flows_from_raw_json_to_grounding():
    leaves = collect_generic_leaves({"name": {"value": "RUPESH", "bbox": [1, 2, 3, 4], "confidence": 0.8}})
    assert leaves["name"].confidence == 0.8
    assert _grounding_from([(1, leaves["name"])]).model_confidence == 0.8
    assert _valid_probability(1.7) is None and _valid_probability(True) is None and _valid_probability("x") is None
