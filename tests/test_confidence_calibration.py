"""FR-CNF-001 / M6: token-probability field confidence and 10-bin calibration gap (PRD 13.3)."""
from doc_extractor.extractor import MAX_IMAGE_PIXELS, attach_field_confidence
from doc_extractor.validation_rules import (
    calibrate_probability,
    expected_calibration_error,
    fit_calibration_alpha,
)


def test_field_confidence_is_geometric_mean_of_value_tokens():
    text = '{"a.b": {"value": "RUPESH", "bbox": [1, 2, 3, 4]}, "c": {"value": null, "bbox": null}}'
    raw = {"a.b": {"value": "RUPESH"}, "c": {"value": None}}
    attach_field_confidence(raw, text, [(i, i + 1, 0.9) for i in range(len(text))])
    assert raw["a.b"]["confidence"] == 0.9


def test_unlocatable_field_gets_no_invented_confidence():
    raw = {"zzz": {"value": "x"}}
    attach_field_confidence(raw, '{"a": {"value": "x"}}', [(0, 20, 0.5)])
    assert "confidence" not in raw["zzz"]


def test_ece_perfectly_calibrated_and_overconfident():
    assert expected_calibration_error([0.9] * 10, [True] * 9 + [False]) < 1e-9
    assert expected_calibration_error([1.0] * 4, [True, False, True, False]) == 0.5


def test_fit_alpha_reduces_gap():
    confs, ok = [0.95] * 10, [True] * 7 + [False] * 3
    a = fit_calibration_alpha(confs, ok)
    assert expected_calibration_error([calibrate_probability(c, a) for c in confs], ok) < expected_calibration_error(confs, ok)


def test_pixel_budget_default():
    assert MAX_IMAGE_PIXELS == 1_300_000
