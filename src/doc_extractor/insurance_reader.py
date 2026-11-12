"""
DOC-02 Insurance Application reader: asks a vision-language model for the DOC-02 fields as JSON,
trims repeated output, then applies the DOC-02 rules (insurance_form.normalise_insurance_fields).

Works with any reader that has generate_text(image, prompt) (QwenVLCasaReader, PaddleVLCasaReader).
Model failures never raise: the document goes to review with no guessed values.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Union
from pathlib import Path

from PIL import Image

from doc_extractor.casa_reader import trim_repeated_lines
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


def read_insurance_form(
    source: Union[str, Path, Image.Image],
    reader: Any,
) -> Dict[str, Any]:
    image = source if isinstance(source, Image.Image) else Image.open(source)
    started = time.perf_counter()
    model_error = None
    try:
        raw_text = reader.generate_text(image, prompt=INSURANCE_PROMPT)
    except Exception as exc:  # fail closed
        logger.exception("Model failed on insurance form")
        raw_text = ""
        model_error = str(exc)
    model_secs = round(time.perf_counter() - started, 2)

    raw_text, looped = trim_repeated_lines(raw_text)
    result = normalise_insurance_fields(raw_text)
    out = result.to_dict()
    out["timing"] = {"model_seconds": model_secs}
    out["raw_model_output"] = raw_text
    if looped:
        out["flags"].append({"code": "MODEL_OUTPUT_LOOPED", "message": "repeated lines removed"})
    if model_error:
        out["flags"].append({"code": "MODEL_ERROR", "message": model_error})
    out["status"] = "needs_review"
    return out
