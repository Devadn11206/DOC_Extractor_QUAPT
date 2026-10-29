"""
Extraction Engine and Evidence Linker for Module C.
Strictly adheres to PRD §5.4 (FR-EXT-001, FR-EXT-002, FR-EXT-003, FR-EXT-004, FR-EXT-005, FR-EXT-007)
and PRD §6.3 Output Contract.
"""

from typing import Any, Dict, List, Optional, Tuple, Type
from pydantic import BaseModel
import re

from doc_extractor.contracts_b import ModuleBOutputPayload, PageLayoutOutput, BoundingBox
from doc_extractor.schemas import CASASchema, InsuranceApplicationSchema, DischargeSummarySchema


SCHEMA_MAP: Dict[str, Type[BaseModel]] = {
    "DOC-01": CASASchema,
    "DOC-02": InsuranceApplicationSchema,
    "DOC-03": DischargeSummarySchema,
}


def find_evidence_for_value(
    value: Any,
    pages: List[PageLayoutOutput],
) -> Optional[Dict[str, Any]]:
    """
    FR-EXT-004 & GR-002: Evidence Bounding Boxes.
    Queries LayoutBlocks and checkboxes to find page, bbox, and supporting text snippet.
    """
    if value is None:
        return None

    str_val = str(value).strip().lower()
    if not str_val:
        return None

    for page in pages:
        # 1. Character grids first: PAN, names in printed boxes (highest precision)
        for grid in page.character_grids:
            grid_text = grid.reconstructed_text.strip()
            if str_val in grid_text.lower() or grid_text.lower() in str_val:
                bbox = grid.bbox_original or grid.bbox_preprocessed
                return {
                    "page": page.page_number,
                    "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                    "text": grid_text,
                    "ocr_confidence": grid.mean_ocr_confidence,
                }

        # 2. Layout blocks (paragraphs, key-value pairs, headers)
        for block in page.blocks:
            block_text = block.text.strip()
            if str_val in block_text.lower():
                bbox = block.bbox_original or block.bbox_preprocessed
                return {
                    "page": page.page_number,
                    "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                    "text": block_text[:120],
                    "ocr_confidence": block.ocr_confidence,
                }

        # 3. Checkboxes (booleans only)
        if isinstance(value, bool):
            for cb in page.checkboxes:
                is_checked = (cb.state == "[x]")
                if is_checked == value:
                    bbox = cb.bbox_original or cb.bbox_preprocessed
                    return {
                        "page": page.page_number,
                        "bbox": [bbox.x0, bbox.y0, bbox.x1, bbox.y1],
                        "text": cb.state,
                        "ocr_confidence": cb.ocr_confidence,
                    }

    # GR-002 strict: never fabricate a bounding box. Return None if no grounded evidence found.
    return None


class SchemaGuidedExtractor:
    """
    Module C Schema-Guided Extractor.
    FR-EXT-001, FR-EXT-002: Extracts schema fields using LLM grammar-guided decoding
    (instructor + litellm) when use_llm=True. Falls back to deterministic heuristics
    for offline testing or when no LLM is configured.
    """

    def __init__(self, model_name: str = "Qwen2.5-7B-Instruct", use_llm: bool = False):
        self.model_name = model_name
        self.use_llm = use_llm

    def extract(
        self,
        payload: ModuleBOutputPayload,
    ) -> BaseModel:
        """
        Extracts structured data according to the bound schema.
        When use_llm=True: uses instructor + litellm for grammar-constrained LLM extraction (FR-EXT-002).
        Otherwise: uses deterministic regex heuristics as an offline fast-path / test double.
        """
        doc_type = payload.classification.document_type.value
        schema_cls = SCHEMA_MAP.get(doc_type)

        if not schema_cls:
            raise ValueError(f"No extraction schema registered for document type: {doc_type}")

        if self.use_llm:
            return self._extract_llm(payload, schema_cls)

        return self._extract_heuristic(payload, doc_type, schema_cls)

    def _extract_llm(self, payload: ModuleBOutputPayload, schema_cls: Type[BaseModel]) -> BaseModel:
        """
        FR-EXT-001 & FR-EXT-002: Grammar-constrained LLM extraction via instructor + litellm.
        Temperature=0 ensures deterministic output. Never guesses: null fields returned as null.
        """
        import instructor  # noqa: PLC0415
        import litellm     # noqa: PLC0415
        client = instructor.from_litellm(litellm.completion)
        return client.chat.completions.create(
            model=self.model_name,
            response_model=schema_cls,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a document extraction engine. "
                        "Extract only the fields defined in the schema from the provided document text. "
                        "If a field is absent or not legibly visible, set it to null. "
                        "Never infer, guess, or hallucinate values. "
                        "Return strictly valid JSON matching the schema."
                    ),
                },
                {"role": "user", "content": payload.combined_markdown},
            ],
            temperature=0.0,
        )

    def _extract_heuristic(
        self,
        payload: ModuleBOutputPayload,
        doc_type: str,
        schema_cls: Type[BaseModel],
    ) -> BaseModel:
        """Deterministic regex heuristic fast-path (offline / test double)."""
        md = payload.combined_markdown
        extracted_dict: Dict[str, Any] = {}

        if doc_type == "DOC-01":
            # Extract CASA fields
            applicant: Dict[str, Any] = {}
            account_details: Dict[str, Any] = {}

            # Name match (from grid or line, up to newline)
            name_match = re.search(r"(?:name|applicant name)\s*[:\-]?\s*([A-Z\s]{3,30}?)(?:\n|$)", md, re.IGNORECASE)
            if name_match:
                applicant["full_name"] = name_match.group(1).strip()

            # PAN match
            pan_match = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b", md)
            if pan_match:
                applicant["pan_card_number"] = pan_match.group(1).strip()

            # DOB match
            dob_match = re.search(r"(?:dob|date of birth|birth date)\s*[:\-]?\s*([0-9\/\-\.]+)", md, re.IGNORECASE)
            if dob_match:
                applicant["date_of_birth"] = dob_match.group(1).strip()

            # Phone match
            phone_match = re.search(r"(?:phone|mobile|contact)\s*[:\-]?\s*(\+?[0-9\s\-]{10,14})", md, re.IGNORECASE)
            if phone_match:
                applicant["phone_number"] = phone_match.group(1).strip()

            # Checkbox opt-in services
            opt_in: Dict[str, Any] = {}
            for page in payload.pages:
                for cb in page.checkboxes:
                    # check mark id or association
                    pass
            # From markdown text checkbox checks
            if "[x] cheque book" in md.lower() or "cheque book: [x]" in md.lower():
                opt_in["cheque_book"] = True
            elif "[ ] cheque book" in md.lower() or "cheque book: [ ]" in md.lower():
                opt_in["cheque_book"] = False

            if "[x] debit card" in md.lower() or "debit card: [x]" in md.lower():
                opt_in["debit_card"] = True
            elif "[ ] debit card" in md.lower() or "debit card: [ ]" in md.lower():
                opt_in["debit_card"] = False

            account_details["opt_in_services"] = opt_in

            # Initial deposit
            deposit_match = re.search(r"(?:initial deposit|deposit amount)\s*[:\-]?\s*(?:inr|rs\.?)?\s*([0-9,\.]+)", md, re.IGNORECASE)
            if deposit_match:
                cleaned_num = deposit_match.group(1).replace(",", "").strip()
                try:
                    account_details["initial_deposit_amount"] = float(cleaned_num)
                except ValueError:
                    pass

            extracted_dict["applicant"] = applicant
            extracted_dict["account_details"] = account_details

        elif doc_type == "DOC-02":
            # Extract Insurance fields
            proposer_match = re.search(r"(?:proposer name|insured name|name)\s*[:\-]?\s*([A-Za-z\s]{3,30})", md, re.IGNORECASE)
            if proposer_match:
                extracted_dict["proposer_name"] = proposer_match.group(1).strip()

            sum_insured_match = re.search(r"(?:sum insured|sum assured)\s*[:\-]?\s*(?:inr|rs\.?)?\s*([0-9,\.]+)", md, re.IGNORECASE)
            if sum_insured_match:
                cleaned_num = sum_insured_match.group(1).replace(",", "").strip()
                try:
                    extracted_dict["sum_insured"] = float(cleaned_num)
                except ValueError:
                    pass

            term_match = re.search(r"(?:policy term|term)\s*[:\-]?\s*([0-9]{1,2})\s*(?:years|yrs)?", md, re.IGNORECASE)
            if term_match:
                try:
                    extracted_dict["policy_term_years"] = int(term_match.group(1))
                except ValueError:
                    pass

            # Nominees
            nominees: List[Dict[str, Any]] = []
            nominee_match = re.search(r"nominee\s*[:\-]?\s*([A-Za-z\s]+?)\s*\((\w+)\)", md, re.IGNORECASE)
            if nominee_match:
                nominees.append({
                    "name": nominee_match.group(1).strip(),
                    "relationship": nominee_match.group(2).strip(),
                    "share_percentage": 100.0,
                })
            extracted_dict["nominees"] = nominees

            med_dec: Dict[str, Any] = {}
            if "[x] diabetes" in md.lower():
                med_dec["has_diabetes"] = True
            elif "[ ] diabetes" in md.lower():
                med_dec["has_diabetes"] = False

            if "[x] hypertension" in md.lower():
                med_dec["has_hypertension"] = True
            elif "[ ] hypertension" in md.lower():
                med_dec["has_hypertension"] = False

            extracted_dict["medical_declaration"] = med_dec

        elif doc_type == "DOC-03":
            # Extract Discharge Summary fields
            patient_match = re.search(r"(?:patient name|patient)\s*[:\-]?\s*([A-Za-z\s]{3,30})", md, re.IGNORECASE)
            if patient_match:
                extracted_dict["patient_name"] = patient_match.group(1).strip()

            ip_match = re.search(r"(?:ip no|ip number|admission no|inpatient no)\s*[:\-]?\s*([A-Za-z0-9\/\-]+)", md, re.IGNORECASE)
            if ip_match:
                extracted_dict["ip_number"] = ip_match.group(1).strip()

            adm_match = re.search(r"(?:admission date|date of admission|admitted)\s*[:\-]?\s*([0-9\/\-\.]+)", md, re.IGNORECASE)
            if adm_match:
                extracted_dict["admission_date"] = adm_match.group(1).strip()

            dis_match = re.search(r"(?:discharge date|date of discharge|discharged)\s*[:\-]?\s*([0-9\/\-\.]+)", md, re.IGNORECASE)
            if dis_match:
                extracted_dict["discharge_date"] = dis_match.group(1).strip()

            diag_match = re.search(r"(?:diagnosis|final diagnosis)\s*[:\-]?\s*([A-Za-z0-9\s,\-\.]+?)(?:\n|$)", md, re.IGNORECASE)
            if diag_match:
                extracted_dict["diagnosis"] = diag_match.group(1).strip()

        return schema_cls.model_validate(extracted_dict)
