"""
DOC-02 Insurance Application reader: asks a vision-language model for the DOC-02 fields as JSON,
trims repeated output, then applies the DOC-02 rules (insurance_form.normalise_insurance_fields).

Works with any reader that has generate_text(image, prompt) (QwenVLCasaReader, PaddleVLCasaReader).
Model failures never raise: the document goes to review with no guessed values.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Union
from pathlib import Path

from PIL import Image

from doc_extractor.casa_form import _after, _lines, parse_model_json
from doc_extractor.casa_reader import MODEL_RETRY_ATTEMPTS, _with_retries, trim_repeated_lines
from doc_extractor.insurance_form import normalise_insurance_fields

logger = logging.getLogger(__name__)

INSURANCE_PROMPT = (
    "This is an insurance application form. Return only a JSON object with exactly these keys:\n"
    "proposer_name (text), sum_insured (number, rupees or dollars as printed), "
    "policy_term_years (whole number), "
    "nominees (a list; each item has name, relationship, age (whole number), share_percentage (number)), "
    "has_diabetes (true/false), has_hypertension (true/false), has_tobacco_use (true/false).\n"
    "Use the value written on the form. Use null if a field is blank or unreadable. Do not guess. "
    "A ticked Yes or No box is true for Yes and false for No. Return only the JSON."
)


def insurance_fields_from_text(text: str) -> Dict[str, Any]:
    """Fallback when the model does not return JSON: reads the three scalar fields from the printed page text."""
    lines = _lines(text)
    out: Dict[str, Any] = {"proposer_name": None, "sum_insured": None, "policy_term_years": None,
                           "nominees": [], "has_diabetes": None, "has_hypertension": None, "has_tobacco_use": None}
    name = " ".join(_after(lines, r"FULL\s*NAME", span=1)).strip()
    if name:
        out["proposer_name"] = name
    sum_text = " ".join(_after(lines, r"SUM\s*INSURED", span=2))
    m = re.search(r"\d{1,3}(?:,\d{3})+|\d{4,}", sum_text)
    if m:
        out["sum_insured"] = m.group(0)
    term_text = " ".join(_after(lines, r"POLICY\s*TERM", span=2))
    m = re.search(r"(?<![\d,])\d{1,2}(?![\d,])", term_text)
    if m:
        out["policy_term_years"] = m.group(0)
    return out


def read_insurance_form(
    source: Union[str, Path, Image.Image],
    reader: Any,
) -> Dict[str, Any]:
    image = source if isinstance(source, Image.Image) else Image.open(source)
    started = time.perf_counter()
    raw_text, model_error, attempts = _with_retries(
        lambda: reader.generate_text(image, prompt=INSURANCE_PROMPT)
    )  # GR-018: up to 3 attempts with back-off
    if model_error:
        logger.error("Model failed on insurance form after %s attempts: %s", attempts, model_error)
    model_secs = round(time.perf_counter() - started, 2)

    raw_text, looped = trim_repeated_lines(raw_text)
    parsed = parse_model_json(raw_text)
    if parsed is None and raw_text.strip():
        # The model answered in page text, not JSON: read the scalar fields from the text.
        parsed = insurance_fields_from_text(raw_text)
    result = normalise_insurance_fields(parsed if parsed is not None else raw_text)
    out = result.to_dict()
    out["timing"] = {"model_seconds": model_secs}
    out["raw_model_output"] = raw_text
    if looped:
        out["flags"].append({"code": "MODEL_OUTPUT_LOOPED", "message": "repeated lines removed"})
    if attempts > 1:
        out["flags"].append({"code": "MODEL_ATTEMPTS", "message": f"{attempts} attempts"})
    if model_error:
        out["flags"].append({"code": "MODEL_ERROR", "message": model_error})
        out["status"] = "failed"
    else:
        out["status"] = "needs_review"
    return out
