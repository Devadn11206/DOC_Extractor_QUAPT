"""
Document classification and routing (PRD FR-CLS-001 to 004, GR-004).

1. Reads the printed text of the page (OCR, no model).
2. Scores each document type by how many of its characteristic words appear.
3. Routes to the matching extractor:
     CASA_ACCOUNT_OPENING (DOC-01, BFSI)   -> read_casa_form
     INSURANCE_APPLICATION (DOC-02, BFSI)  -> read_insurance_form
     DISCHARGE_SUMMARY (DOC-03, Healthcare) -> not built yet, routed to review
     UNKNOWN                                -> review, no extraction (FR-CLS-002)
A reviewer can override the type (FR-CLS-004).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from PIL import Image

MIN_CONFIDENCE = 0.5  # FR-CLS-002: below this the type is UNKNOWN

# Characteristic words per type (letters and digits only, uppercase).
TYPE_KEYWORDS: Dict[str, List[str]] = {
    "CASA_ACCOUNT_OPENING": [
        "ACCOUNTOPENING", "CASA", "CUSTID", "INITIALDEPOSIT", "SAVINGS", "CURRENT",
        "ACCOUNTDETAILS", "OPTIN", "KYC", "FORMFORINDIVIDUALS", "SMALLACCOUNT",
    ],
    "INSURANCE_APPLICATION": [
        "INSURANCE", "PROPOSAL", "POLICY", "SUMINSURED", "NOMINEE", "POLICYTERM",
        "PROPOSER", "PREMIUM", "COVER",
    ],
    "DISCHARGE_SUMMARY": [
        "DISCHARGE", "ADMISSION", "DIAGNOSIS", "HOSPITAL", "MEDICATION", "IPNUMBER",
        "PHYSICIAN", "DISCHARGESUMMARY", "TREATMENT",
    ],
}

DOMAIN = {
    "CASA_ACCOUNT_OPENING": ("DOC-01", "BFSI"),
    "INSURANCE_APPLICATION": ("DOC-02", "BFSI"),
    "DISCHARGE_SUMMARY": ("DOC-03", "Healthcare"),
    "UNKNOWN": (None, None),
}


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def classify_text(page_text: str) -> Dict[str, Any]:
    """Classifies from printed text. Returns type, domain, confidence and the matched words."""
    haystack = _norm(page_text)
    scores = {}
    matches = {}
    for doc_type, keywords in TYPE_KEYWORDS.items():
        hit = [k for k in keywords if k in haystack]
        matches[doc_type] = hit
        # Confidence: share of the first four characteristic words found, capped at 1.
        scores[doc_type] = min(1.0, len(hit) / 4.0)

    best = max(scores, key=scores.get)
    ranked = sorted(scores.values(), reverse=True)
    tie = len(ranked) > 1 and ranked[0] == ranked[1]
    if scores[best] < MIN_CONFIDENCE or tie:
        return {"doc_type": "UNKNOWN", "domain": None, "document_id": None,
                "confidence": round(scores[best], 2), "matched": matches[best]}
    doc_id, domain = DOMAIN[best]
    return {"doc_type": best, "domain": domain, "document_id": doc_id,
            "confidence": round(scores[best], 2), "matched": matches[best]}


def page_text(image: Image.Image) -> str:
    """Printed text of the page (RapidOCR), joined with spaces."""
    from doc_extractor.casa_reader import ocr_tokens

    return " ".join(t.get("text", "") for t in ocr_tokens(image))


def classify_document(image: Image.Image) -> Dict[str, Any]:
    return classify_text(page_text(image))


def route_document(image: Image.Image, reader: Any, override: Optional[str] = None) -> Dict[str, Any]:
    """Classifies the page, then runs the matching extractor. override (FR-CLS-004) replaces the detected type."""
    from doc_extractor.casa_reader import read_casa_form
    from doc_extractor.insurance_reader import read_insurance_form

    classification = classify_document(image)
    if override:
        if override not in TYPE_KEYWORDS:
            raise ValueError(f"unknown document type: {override}")
        classification = {**classification, "doc_type": override, "domain": DOMAIN[override][1],
                          "document_id": DOMAIN[override][0], "overridden": True}

    doc_type = classification["doc_type"]
    if doc_type == "UNKNOWN":
        # FR-CLS-002: no extraction runs; the document goes to review.
        return {"classification": classification, "status": "needs_review", "extraction": None,
                "flags": [{"code": "UNKNOWN_DOCUMENT_TYPE", "fields": []}]}

    if doc_type == "CASA_ACCOUNT_OPENING":
        extraction = read_casa_form(image, reader)
    elif doc_type == "INSURANCE_APPLICATION":
        extraction = read_insurance_form(image, reader)
    else:  # DISCHARGE_SUMMARY: schema exists, extractor not built yet
        return {"classification": classification, "status": "needs_review", "extraction": None,
                "flags": [{"code": "EXTRACTOR_NOT_BUILT", "message": "discharge summary extraction is not built yet"}]}

    return {"classification": classification, "status": extraction["status"], "extraction": extraction,
            "flags": extraction.get("flags", [])}
