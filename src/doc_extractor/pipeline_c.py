"""
Module C Canonical Envelope and Pipeline Orchestrator.
Adheres strictly to PRD §6.3 Canonical Output Envelope, FR-VAL-001..004, FR-CNF-001..003.
"""

from typing import Any, Dict, List, Literal, Optional, Tuple
from pydantic import BaseModel, Field

from doc_extractor.contracts_b import ModuleBOutputPayload
from doc_extractor.extractor import SchemaGuidedExtractor, find_evidence_for_value
from doc_extractor.validation_rules import (
    normalize_date_to_iso,
    normalize_phone_number,
    validate_pan,
    validate_chronology,
    validate_arithmetic,
)


class EvidenceModel(BaseModel):
    page: int
    bbox: List[float] = Field(description="[x0, y0, x1, y1] in pixels (SCH-013)")
    text: str


class FieldOutput(BaseModel):
    value: Any = None
    confidence: float = Field(ge=0.0, le=1.0)
    null_reason: Optional[Literal["absent", "illegible"]] = None
    review_required: bool = False
    validation: Literal["passed", "failed", "not_applicable"] = "passed"
    evidence: Optional[EvidenceModel] = None


class FlagItem(BaseModel):
    code: str
    message: Optional[str] = None
    fields: List[str] = Field(default_factory=list)


class DocumentMetadata(BaseModel):
    document_type: str
    schema_id: str
    schema_version: Any
    classification_confidence: float
    status: Literal["processing", "auto_approved", "needs_review", "reviewed", "rejected", "failed"]
    page_count: int


class RunMetadata(BaseModel):
    run_id: str
    model_versions: Dict[str, str]
    prompt_version: str


class CanonicalOutputEnvelope(BaseModel):
    """PRD §6.3 Output Envelope."""
    document_id: str
    document_metadata: DocumentMetadata
    fields: Dict[str, FieldOutput]
    flags: List[FlagItem] = Field(default_factory=list)
    run: RunMetadata


class ModuleCPipeline:
    """
    Module C Pipeline Orchestrator.
    Takes ModuleBOutputPayload, extracts structured fields, validates them,
    computes calibrated confidences, flags exceptions, and emits CanonicalOutputEnvelope.
    """

    def __init__(self, extractor: Optional[SchemaGuidedExtractor] = None):
        self.extractor = extractor or SchemaGuidedExtractor()

    def process(self, payload: ModuleBOutputPayload) -> CanonicalOutputEnvelope:
        if payload.status == "needs_review" or payload.classification.document_type.value == "UNKNOWN":
            # Pass-through review status without extraction
            return CanonicalOutputEnvelope(
                document_id=payload.document_id,
                document_metadata=DocumentMetadata(
                    document_type=payload.classification.document_type.value,
                    schema_id=payload.classification.schema_id or "UNKNOWN",
                    schema_version=payload.classification.schema_version or "1.0.0",
                    classification_confidence=payload.classification.classification_confidence,
                    status="needs_review",
                    page_count=len(payload.pages),
                ),
                fields={},
                flags=[FlagItem(code="CLASSIFICATION_UNCERTAIN", message="Document routed to review prior to extraction")],
                run=RunMetadata(
                    run_id=payload.run_id,
                    model_versions={"parser": payload.parser_model_name, "extractor": self.extractor.model_name},
                    prompt_version="1.0.0",
                ),
            )

        # Run extraction
        extracted_data = self.extractor.extract(payload)
        doc_type = payload.classification.document_type.value

        fields_envelope: Dict[str, FieldOutput] = {}
        flags: List[FlagItem] = []
        requires_document_review = False

        # Flatten and validate based on schema type
        if doc_type == "DOC-01":
            app = getattr(extracted_data, "applicant", None)
            acc = getattr(extracted_data, "account_details", None)

            # full_name
            name_val = getattr(app, "full_name", None) if app else None
            fields_envelope["applicant.full_name"] = self._create_field_entry(
                name_val, payload, required=True
            )

            # pan_card_number
            pan_val = getattr(app, "pan_card_number", None) if app else None
            norm_pan, pan_err = validate_pan(pan_val)
            fields_envelope["applicant.pan_card_number"] = self._create_field_entry(
                norm_pan, payload, error=pan_err, required=False
            )

            # date_of_birth
            dob_val = getattr(app, "date_of_birth", None) if app else None
            norm_dob, dob_err = normalize_date_to_iso(dob_val)
            fields_envelope["applicant.date_of_birth"] = self._create_field_entry(
                norm_dob, payload, error=dob_err, required=False
            )

            # phone_number
            phone_val = getattr(app, "phone_number", None) if app else None
            norm_phone, phone_err = normalize_phone_number(phone_val)
            fields_envelope["applicant.phone_number"] = self._create_field_entry(
                norm_phone, payload, error=phone_err, required=False
            )

            # initial_deposit_amount
            dep_val = getattr(acc, "initial_deposit_amount", None) if acc else None
            fields_envelope["account_details.initial_deposit_amount"] = self._create_field_entry(
                dep_val, payload, required=False
            )

            # cheque_book
            opt_in = getattr(acc, "opt_in_services", None) if acc else None
            chq_val = getattr(opt_in, "cheque_book", None) if opt_in else None
            fields_envelope["account_details.opt_in_services.cheque_book"] = self._create_field_entry(
                chq_val, payload, required=False
            )

        elif doc_type == "DOC-02":
            p_name = getattr(extracted_data, "proposer_name", None)
            fields_envelope["proposer_name"] = self._create_field_entry(p_name, payload, required=True)

            sum_ins = getattr(extracted_data, "sum_insured", None)
            fields_envelope["sum_insured"] = self._create_field_entry(sum_ins, payload, required=False)

            p_term = getattr(extracted_data, "policy_term_years", None)
            fields_envelope["policy_term_years"] = self._create_field_entry(p_term, payload, required=False)

            med = getattr(extracted_data, "medical_declaration", None)
            diab = getattr(med, "has_diabetes", None) if med else None
            fields_envelope["medical_declaration.has_diabetes"] = self._create_field_entry(diab, payload, required=False)

        elif doc_type == "DOC-03":
            pat_name = getattr(extracted_data, "patient_name", None)
            fields_envelope["patient_name"] = self._create_field_entry(pat_name, payload, required=True)

            ip_no = getattr(extracted_data, "ip_number", None)
            fields_envelope["ip_number"] = self._create_field_entry(ip_no, payload, required=False)

            adm_date = getattr(extracted_data, "admission_date", None)
            norm_adm, adm_err = normalize_date_to_iso(adm_date)
            fields_envelope["admission_date"] = self._create_field_entry(norm_adm, payload, error=adm_err, required=False)

            dis_date = getattr(extracted_data, "discharge_date", None)
            norm_dis, dis_err = normalize_date_to_iso(dis_date)
            fields_envelope["discharge_date"] = self._create_field_entry(norm_dis, payload, error=dis_err, required=False)

            diag = getattr(extracted_data, "diagnosis", None)
            fields_envelope["diagnosis"] = self._create_field_entry(diag, payload, required=False)

            # Chronology check
            chronology_flag = validate_chronology(norm_adm, norm_dis)
            if chronology_flag:
                flags.append(FlagItem(**chronology_flag))
                requires_document_review = True
                if "admission_date" in fields_envelope:
                    fields_envelope["admission_date"].review_required = True
                if "discharge_date" in fields_envelope:
                    fields_envelope["discharge_date"].review_required = True

        # Check overall fields review_required
        for f_name, f_entry in fields_envelope.items():
            if f_entry.review_required or f_entry.validation == "failed":
                requires_document_review = True

        # SCH-012: if upstream reports failed, preserve it and skip auto-approve logic
        if payload.status == "failed":
            final_status: Literal["auto_approved", "needs_review", "failed"] = "failed"
        else:
            final_status = "needs_review" if requires_document_review else "auto_approved"

        return CanonicalOutputEnvelope(
            document_id=payload.document_id,
            document_metadata=DocumentMetadata(
                document_type=payload.classification.document_type.value,
                schema_id=payload.classification.schema_id or "DOC_SCHEMA",
                schema_version=payload.classification.schema_version or "1.0.0",
                classification_confidence=payload.classification.classification_confidence,
                status=final_status,
                page_count=len(payload.pages),
            ),
            fields=fields_envelope,
            flags=flags,
            run=RunMetadata(
                run_id=payload.run_id,
                model_versions={"parser": payload.parser_model_name, "extractor": self.extractor.model_name},
                prompt_version="1.0.0",
            ),
        )

    def _create_field_entry(
        self,
        value: Any,
        payload: ModuleBOutputPayload,
        error: Optional[str] = None,
        required: bool = False,
    ) -> FieldOutput:
        """Helper to create a validated FieldOutput with evidence and calibrated confidence."""
        if value is None:
            return FieldOutput(
                value=None,
                confidence=0.0,
                null_reason="absent",
                review_required=required,
                validation="not_applicable" if not required else "failed",
                evidence=None,
            )

        evidence_dict = find_evidence_for_value(value, payload.pages)
        evidence_model = None
        optical_conf = 1.0

        if evidence_dict:
            evidence_model = EvidenceModel(
                page=evidence_dict["page"],
                bbox=evidence_dict["bbox"],
                text=evidence_dict["text"],
            )
            optical_conf = evidence_dict.get("ocr_confidence", 1.0)
        elif not isinstance(value, bool):
            # GR-002 / FR-EXT-004: No non-null value without pixel-grounded evidence.
            # Set to null and flag EVIDENCE_MISSING so it routes to human review.
            return FieldOutput(
                value=None,
                confidence=0.0,
                null_reason="absent",
                review_required=True,
                validation="failed",
                evidence=None,
            )

        has_validation_failed = (error is not None)
        validation_status: Literal["passed", "failed"] = "failed" if has_validation_failed else "passed"

        # Calibrated confidence: optical score × penalty for validation failure
        confidence = round(optical_conf * (0.5 if has_validation_failed else 0.95), 2)
        review_required = has_validation_failed or (confidence < 0.75)

        return FieldOutput(
            value=value,
            confidence=confidence,
            null_reason=None,
            review_required=review_required,
            validation=validation_status,
            evidence=evidence_model,
        )
