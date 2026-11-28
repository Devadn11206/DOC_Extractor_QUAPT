"""Regressions: PDF page limit with compressed page objects, silently skipped rules, ignored ``on_fail``."""

import io
import zlib

import pytest

from doc_extractor.config import Settings
from doc_extractor.contracts_c import ExtractedField
from doc_extractor.exceptions import UnsupportedFileError
from doc_extractor.rule_evaluator import RULE_NOT_EVALUATED, evaluate_dynamic_rules
from doc_extractor.validation import DocumentValidator
from schema_lifecycle.metamodel import RuleDefinition


def _object_stream_pdf(pages: int) -> bytes:
    """A PDF 1.5 file whose catalog and page objects all sit inside one compressed object stream."""
    kids = " ".join(f"{3 + i} 0 R" for i in range(pages))
    objects = [b"<</Type/Catalog/Pages 2 0 R>>", f"<</Type/Pages/Kids[{kids}]/Count {pages}>>".encode()]
    objects += [b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>"] * pages
    offsets, body = [], b""
    for obj in objects:
        offsets.append(len(body))
        body += obj + b" "
    head = " ".join(f"{i + 1} {off}" for i, off in enumerate(offsets)).encode() + b" "
    data = zlib.compress(head + body)
    stm, xref = len(objects) + 1, len(objects) + 2
    pdf = b"%PDF-1.5\n"
    stm_at = len(pdf)
    pdf += b"%d 0 obj<</Type/ObjStm/N %d/First %d/Filter/FlateDecode/Length %d>>stream\n" % (
        stm, len(objects), len(head), len(data))
    pdf += data + b"\nendstream endobj\n"
    xref_at = len(pdf)
    entries = [(0, 0, 0)] + [(2, stm, i) for i in range(len(objects))] + [(1, stm_at, 0), (1, xref_at, 0)]
    table = b"".join(bytes([a]) + b.to_bytes(4, "big") + bytes([c]) for a, b, c in entries)
    pdf += b"%d 0 obj<</Type/XRef/Size %d/Root 1 0 R/W[1 4 1]/Length %d>>stream\n" % (xref, xref + 1, len(table))
    return pdf + table + b"\nendstream endobj\nstartxref\n%d\n%%%%EOF\n" % xref_at


def test_pages_in_a_compressed_object_stream_are_counted():
    result = DocumentValidator(Settings(max_page_count=3)).validate_file(io.BytesIO(_object_stream_pdf(3)), "a.pdf")
    assert result.page_count == 3


def test_pages_in_a_compressed_object_stream_cannot_bypass_the_limit():
    with pytest.raises(UnsupportedFileError) as exc:
        DocumentValidator(Settings(max_page_count=2)).validate_file(io.BytesIO(_object_stream_pdf(3)), "a.pdf")
    assert exc.value.details["reason"] == "PAGE_LIMIT_EXCEEDED" and exc.value.details["page_count"] == 3


def _evaluate(expression, values, targets, **rule_kwargs):
    fields = {k: ExtractedField.model_construct(value=v, review_required=False, validation=None)
              for k, v in values.items()}
    flags = []
    rule = RuleDefinition(id="R1", expression=expression, target_fields=targets, **rule_kwargs)
    evaluate_dynamic_rules([rule], dict(values), fields, flags)
    return [f.code for f in flags], {k: f.review_required for k, f in fields.items()}


@pytest.mark.parametrize("expression,values,targets", [
    ("dob <= today - 18y", {"dob": "2020-01-01"}, ["dob"]),
    ("deposit >= 1000 and deposit <= 5000", {"deposit": 5}, ["deposit"]),
    ("deposit >= 1000", {"deposit": "Rs. 500"}, ["deposit"]),
    ("deposit + fee > 10", {"deposit": 1, "fee": 2}, ["deposit", "fee"]),
    ("start_date <= end_date", {"start_date": "soon", "end_date": "2026-01-01"}, ["start_date", "end_date"]),
    ("sum(items[].amount) == total", {"items.0.amount": "n/a", "total": 5}, ["items[].amount", "total"]),
    ("regex:[unclosed", {"code": "x"}, ["code"]),
])
def test_a_rule_that_cannot_be_evaluated_routes_to_review(expression, values, targets):
    codes, review = _evaluate(expression, values, targets)
    assert codes == [RULE_NOT_EVALUATED] and all(review.values())


@pytest.mark.parametrize("expression,values", [
    ("dob <= today - 18y", {"dob": None}),
    ("deposit >= 1000", {"deposit": None}),
    ("deposit >= 1000", {"deposit": 1500}),
    ("deposit != null", {"deposit": "anything"}),
])
def test_missing_or_satisfying_values_raise_no_flag(expression, values):
    codes, review = _evaluate(expression, values, list(values))
    assert codes == [] and not any(review.values())


@pytest.mark.parametrize("on_fail,expected", [("review", True), ("reject", True), ("flag", False)])
def test_on_fail_decides_whether_the_field_is_marked_for_review(on_fail, expected):
    codes, review = _evaluate("deposit >= 1000", {"deposit": 5}, ["deposit"], on_fail=on_fail)
    assert codes == ["RULE_VIOLATION"] and review == {"deposit": expected}
