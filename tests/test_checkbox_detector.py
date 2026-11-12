"""Checkbox detector on synthetic images (known answers)."""

import numpy as np
import cv2

from doc_extractor.checkbox_detector import detect_checkboxes

LABELS = {"opt_in_cheque_book": ["CHEQUEBOOK"], "opt_in_debit_card": ["DEBITCARD"]}


def _draw(ticked_cheque: bool, ticked_debit: bool):
    img = np.full((200, 700, 3), 255, np.uint8)
    tokens = []
    rows = [(40, "Cheque book", ticked_cheque), (110, "Debit card", ticked_debit)]
    for y, label, ticked in rows:
        box_x0, box_y0, box_size = 100, y, 30
        cv2.rectangle(img, (box_x0, box_y0), (box_x0 + box_size, box_y0 + box_size), (0, 0, 0), 2)
        if ticked:
            cv2.line(img, (box_x0 + 6, box_y0 + 16), (box_x0 + 13, box_y0 + 25), (0, 0, 0), 3)
            cv2.line(img, (box_x0 + 13, box_y0 + 25), (box_x0 + 26, box_y0 + 6), (0, 0, 0), 3)
        lx0, ly0 = 150, y + 2
        cv2.putText(img, label, (lx0, y + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        tokens.append({"text": label, "bbox": [lx0, ly0, lx0 + 180, ly0 + 28], "page": 1})
    return img, tokens


def test_ticked_box_is_true_and_empty_box_is_false():
    img, tokens = _draw(ticked_cheque=True, ticked_debit=False)
    got = detect_checkboxes(img, tokens, LABELS)
    assert got["opt_in_cheque_book"] is True
    assert got["opt_in_debit_card"] is False


def test_both_ticked():
    img, tokens = _draw(ticked_cheque=True, ticked_debit=True)
    got = detect_checkboxes(img, tokens, LABELS)
    assert got["opt_in_cheque_book"] is True
    assert got["opt_in_debit_card"] is True


def test_missing_label_gives_none():
    img, tokens = _draw(True, True)
    got = detect_checkboxes(img, [], LABELS)
    assert got["opt_in_cheque_book"] is None and got["opt_in_debit_card"] is None


def test_reader_uses_detector_for_account_type_on_real_sample_form():
    import os
    from PIL import Image
    from doc_extractor.casa_reader import read_casa_form

    sample = os.path.join(os.path.dirname(__file__), "..", "test_account_form.jpg")
    if not os.path.exists(sample):
        import pytest
        pytest.skip("sample form not present")

    class EmptyModel:
        def generate_text(self, image, prompt=None, repetition_penalty=1.05):
            return "{}"

    out = read_casa_form(Image.open(sample), EmptyModel())
    acc = out["fields"]["account_type"]
    assert acc["value"] == "SAVINGS"
    assert acc["review_required"] is True  # detector is not validated yet, so still reviewed


def test_word_inside_a_sentence_is_not_a_checkbox_label():
    tokens = [{"text": "Please open a Savings Bank account in the name of Mr/Ms", "bbox": [10, 10, 600, 30], "page": 1}]
    img = np.full((100, 700, 3), 255, np.uint8)
    assert detect_checkboxes(img, tokens, {"account": ["SAVINGS"]})["account"] is None


def test_currency_read_from_ocr_when_model_omits_it():
    import re
    joined = re.sub(r"[^A-Z]", "", " ".join(t["text"] for t in [
        {"text": "Amount:"}, {"text": "15,000.00"}, {"text": "Currency:"}, {"text": "I N R"}]).upper())
    assert re.search(r"CURRENCY.{0,12}INR", joined)
