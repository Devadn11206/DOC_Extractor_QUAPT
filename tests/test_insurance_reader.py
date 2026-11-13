"""DOC-02 reader with a fake model (no GPU needed)."""

from PIL import Image

from doc_extractor.insurance_reader import INSURANCE_PROMPT, read_insurance_form

GOOD = ('{"proposer_name":"Arthur Pendelton","sum_insured":500000,"policy_term_years":20,'
        '"nominees":[{"name":"Eleanor Pendelton","relationship":"Spouse","age":42,"share_percentage":70},'
        '{"name":"Thomas Pendelton","relationship":"Son","age":14,"share_percentage":30}],'
        '"has_diabetes":false,"has_hypertension":false,"has_tobacco_use":true}')


class FakeReader:
    def __init__(self, text):
        self.text = text

    def generate_text(self, image, prompt=None):
        return self.text


class BrokenReader:
    def generate_text(self, image, prompt=None):
        raise RuntimeError("gpu unavailable")


def _img():
    return Image.new("RGB", (10, 10), "white")


def test_prompt_lists_every_doc02_field():
    for key in ("proposer_name", "sum_insured", "policy_term_years", "nominees", "has_diabetes",
                "has_hypertension", "has_tobacco_use", "share_percentage", "age"):
        assert key in INSURANCE_PROMPT


def test_good_model_output_is_parsed_and_still_needs_review():
    res = read_insurance_form(_img(), FakeReader(GOOD))
    assert res["fields"]["proposer_name"]["value"] == "ARTHUR PENDELTON"
    assert res["fields"]["sum_insured"]["value"] == 500000.0
    assert res["nominees"][1]["name"] == "THOMAS PENDELTON"
    assert res["status"] == "needs_review"


def test_model_error_after_retries_is_failed_with_no_values():
    res = read_insurance_form(_img(), BrokenReader())
    assert res["status"] == "failed"  # PRD SCH-012 / GR-018
    assert any(f["code"] == "MODEL_ERROR" for f in res["flags"])
    assert res["fields"]["proposer_name"]["value"] is None


def test_looping_output_is_trimmed():
    looped = GOOD + "\n" + "[X] Yes\n" * 10
    res = read_insurance_form(_img(), FakeReader(looped))
    assert any(f["code"] == "MODEL_OUTPUT_LOOPED" for f in res["flags"])


WRIGHT_PAGE_TEXT = """LIFE INSURANCE FORM
Proposer Details
Full Name
Alexander James Wright
Coverage Details
Required Sum Insured (Amount) Policy Term (Years)
$ 500,000 20
Home Address"""


def test_page_text_fallback_reads_name_sum_and_term():
    from doc_extractor.insurance_reader import insurance_fields_from_text
    f = insurance_fields_from_text(WRIGHT_PAGE_TEXT)
    assert f["proposer_name"] == "Alexander James Wright"
    assert f["sum_insured"] == "500,000"
    assert f["policy_term_years"] == "20"


def test_non_json_answer_uses_text_fallback():
    res = read_insurance_form(_img(), FakeReader(WRIGHT_PAGE_TEXT))
    assert res["fields"]["proposer_name"]["value"] == "ALEXANDER JAMES WRIGHT"
    assert res["fields"]["policy_term_years"]["value"] == 20
    assert res["fields"]["sum_insured"]["value"] == 500000.0


def test_printed_value_gets_its_box_and_handwritten_value_gets_label_box():
    from doc_extractor.insurance_reader import ground_insurance_fields
    out = {"fields": {
        "proposer_name": {"value": "ALEXANDER JAMES WRIGHT"},
        "sum_insured": {"value": 500000.0},
    }}
    class Tok(dict): pass
    fake_tokens = [
        {"text": "Full Name", "bbox": [70, 330, 170, 350]},
        {"text": "Alexander James Wright", "bbox": [100, 360, 500, 390]},
        {"text": "Required Sum Insured", "bbox": [70, 430, 300, 450]},
        {"text": "$ 500,000", "bbox": [80, 460, 240, 490]},
    ]
    import doc_extractor.insurance_reader as ir
    original = ir.__dict__.get("ocr_tokens")
    import doc_extractor.casa_reader as cr
    cr_original = cr.ocr_tokens
    cr.ocr_tokens = lambda image: fake_tokens
    try:
        res = ground_insurance_fields(out, _img())
    finally:
        cr.ocr_tokens = cr_original
    assert res["fields"]["proposer_name"]["evidence"]["source"] == "ocr_box"
    assert res["fields"]["sum_insured"]["evidence"]["source"] == "ocr_box"
    # handwritten (not printed): label box
    out2 = {"fields": {"proposer_name": {"value": "SARAH LINDQVIST"}}}
    cr.ocr_tokens = lambda image: [{"text": "Full Name", "bbox": [70, 330, 170, 350]}]
    try:
        res2 = ground_insurance_fields(out2, _img())
    finally:
        cr.ocr_tokens = cr_original
    assert res2["fields"]["proposer_name"]["evidence"]["source"] == "label_box"
