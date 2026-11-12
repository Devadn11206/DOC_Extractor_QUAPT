"""Document classification and routing (FR-CLS-001/002/004, GR-004)."""

from doc_extractor.document_router import classify_text


def test_casa_text_is_classified_as_doc01():
    c = classify_text("CASA ACCOUNT OPENING FORM FOR INDIVIDUALS Cust ID Initial deposit Savings")
    assert c["doc_type"] == "CASA_ACCOUNT_OPENING" and c["document_id"] == "DOC-01" and c["domain"] == "BFSI"


def test_insurance_text_is_classified_as_doc02():
    c = classify_text("Secure Life insurance proposal policy term sum insured nominee premium")
    assert c["doc_type"] == "INSURANCE_APPLICATION" and c["document_id"] == "DOC-02"


def test_discharge_text_is_classified_as_doc03_healthcare():
    c = classify_text("Hospital discharge summary admission date diagnosis medication")
    assert c["doc_type"] == "DISCHARGE_SUMMARY" and c["domain"] == "Healthcare"


def test_unrelated_document_is_unknown():
    c = classify_text("Electricity bill units consumed tariff meter reading due date")
    assert c["doc_type"] == "UNKNOWN" and c["document_id"] is None


def test_mixed_evidence_below_threshold_is_unknown():
    c = classify_text("policy")  # one weak word
    assert c["doc_type"] == "UNKNOWN"
