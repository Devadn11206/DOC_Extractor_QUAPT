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


def test_model_error_sends_document_to_review_with_no_values():
    res = read_insurance_form(_img(), BrokenReader())
    assert res["status"] == "needs_review"
    assert any(f["code"] == "MODEL_ERROR" for f in res["flags"])
    assert res["fields"]["proposer_name"]["value"] is None


def test_looping_output_is_trimmed():
    looped = GOOD + "\n" + "[X] Yes\n" * 10
    res = read_insurance_form(_img(), FakeReader(looped))
    assert any(f["code"] == "MODEL_OUTPUT_LOOPED" for f in res["flags"])
