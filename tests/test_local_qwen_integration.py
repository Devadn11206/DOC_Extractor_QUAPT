"""
Tests for Local Qwen2.5-7B-Instruct Integration in Module C.

Validates:
1. Local model configuration and Settings guardrails (ARC-001, SEC-011).
2. SchemaGuidedExtractor factories (.for_local_ollama, .from_settings).
3. Exact Ollama model naming, provider, and LiteLLM/Instructor parameters.
4. Successful mocked inference for DOC-01, DOC-02, and DOC-03 schemas.
5. Structured output validation and error handling for malformed model outputs.
6. Evidence grounding against Module B OCR layout blocks.
7. Strict no-fallback behavior (LLMExecutionError raised; heuristic never invoked).
8. Permitted fallback behavior when allow_fallback=True (HEURISTIC_FALLBACK).
9. ModuleCPipeline integration with local LLM extractor.
10. Live local probe confirming fail-closed error reporting when Ollama daemon is offline.
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from doc_extractor.config import Settings
from doc_extractor.contracts_b import (
    BoundingBox,
    ClassificationOutput,
    ConfidenceProvenance,
    LayoutBlock,
    LayoutElementType,
    ModuleBOutputPayload,
    MVPClassificationDomain,
    MVPDocumentType,
    PageLayoutOutput,
)
from doc_extractor.contracts_c import (
    CanonicalExtractionEnvelope,
    ExecutionMode,
    ExecutionProvenance,
    ExtractionResult,
)
from doc_extractor.exceptions import LLMExecutionError
from doc_extractor.extractor import SchemaGuidedExtractor
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.schemas import (
    AccountDetails,
    ApplicantDetails,
    CASASchema,
    DischargeSummarySchema,
    InsuranceApplicationSchema,
    MedicationItem,
    NomineeDetails,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def mock_module_b_doc01_payload() -> ModuleBOutputPayload:
    """Fixture producing synthetic Module B payload for DOC-01 with OCR tokens."""
    block_name = LayoutBlock(
        block_id="b_name_001",
        element_type=LayoutElementType.KEY_VALUE_PAIR,
        text="Applicant Name: ARUN VERMA",
        ocr_confidence=0.98,
        bbox_preprocessed=BoundingBox(x0=50, y0=100, x1=250, y1=130),
        bbox_original=BoundingBox(x0=50, y0=100, x1=250, y1=130),
        reading_order_index=1,
    )
    block_pan = LayoutBlock(
        block_id="b_pan_002",
        element_type=LayoutElementType.KEY_VALUE_PAIR,
        text="Permanent Account Number (PAN): ABCDE1234F",
        ocr_confidence=0.99,
        bbox_preprocessed=BoundingBox(x0=50, y0=140, x1=300, y1=170),
        bbox_original=BoundingBox(x0=50, y0=140, x1=300, y1=170),
        reading_order_index=2,
    )
    block_acc = LayoutBlock(
        block_id="b_acc_003",
        element_type=LayoutElementType.KEY_VALUE_PAIR,
        text="Account Type: SAVINGS",
        ocr_confidence=0.97,
        bbox_preprocessed=BoundingBox(x0=50, y0=180, x1=200, y1=210),
        bbox_original=BoundingBox(x0=50, y0=180, x1=200, y1=210),
        reading_order_index=3,
    )
    page = PageLayoutOutput(
        page_number=1,
        markdown_content="Applicant Name: ARUN VERMA\nPermanent Account Number (PAN): ABCDE1234F\nAccount Type: SAVINGS",
        blocks=[block_name, block_pan, block_acc],
        mean_ocr_confidence=0.98,
    )
    return ModuleBOutputPayload(
        run_id="run_qwen_test_001",
        document_id="doc_qwen_test_001",
        tenant_id="tenant_internal",
        status="processing",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.DOC_01_CASA,
            classification_confidence=0.99,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            schema_id="DOC-01",
            schema_version="1.0.0",
        ),
        pages=[page],
        combined_markdown=page.markdown_content,
        parser_model_name="pdfium-text",
        classifier_model_name="heuristic-classifier",
        processing_time_ms=120.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def mock_module_b_doc02_payload() -> ModuleBOutputPayload:
    """Fixture producing synthetic Module B payload for DOC-02 Insurance Application."""
    block = LayoutBlock(
        block_id="b_ins_001",
        element_type=LayoutElementType.KEY_VALUE_PAIR,
        text="Insured Name: PRIYA SHARMA\nCoverage Amount: INR 5,00,000",
        ocr_confidence=0.96,
        bbox_preprocessed=BoundingBox(x0=40, y0=80, x1=350, y1=140),
        bbox_original=BoundingBox(x0=40, y0=80, x1=350, y1=140),
        reading_order_index=1,
    )
    page = PageLayoutOutput(
        page_number=1,
        markdown_content=block.text,
        blocks=[block],
        mean_ocr_confidence=0.96,
    )
    return ModuleBOutputPayload(
        run_id="run_ins_001",
        document_id="doc_ins_001",
        tenant_id="tenant_internal",
        status="processing",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.BFSI,
            document_type=MVPDocumentType.DOC_02_INSURANCE_APP,
            classification_confidence=0.97,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            schema_id="DOC-02",
            schema_version="1.0.0",
        ),
        pages=[page],
        combined_markdown=page.markdown_content,
        parser_model_name="pdfium-text",
        classifier_model_name="heuristic-classifier",
        processing_time_ms=110.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def mock_module_b_doc03_payload() -> ModuleBOutputPayload:
    """Fixture producing synthetic Module B payload for DOC-03 Discharge Summary."""
    block = LayoutBlock(
        block_id="b_dis_001",
        element_type=LayoutElementType.KEY_VALUE_PAIR,
        text="Patient Name: RAJESH GUPTA\nAdmission Date: 2026-01-10\nDischarge Date: 2026-01-15",
        ocr_confidence=0.95,
        bbox_preprocessed=BoundingBox(x0=30, y0=60, x1=400, y1=150),
        bbox_original=BoundingBox(x0=30, y0=60, x1=400, y1=150),
        reading_order_index=1,
    )
    page = PageLayoutOutput(
        page_number=1,
        markdown_content=block.text,
        blocks=[block],
        mean_ocr_confidence=0.95,
    )
    return ModuleBOutputPayload(
        run_id="run_dis_001",
        document_id="doc_dis_001",
        tenant_id="tenant_internal",
        status="processing",
        classification=ClassificationOutput(
            domain=MVPClassificationDomain.HEALTHCARE,
            document_type=MVPDocumentType.DOC_03_DISCHARGE_SUMMARY,
            classification_confidence=0.98,
            confidence_provenance=ConfidenceProvenance.RULE_HEURISTIC,
            schema_id="DOC-03",
            schema_version="1.0.0",
        ),
        pages=[page],
        combined_markdown=page.markdown_content,
        parser_model_name="pdfium-text",
        classifier_model_name="heuristic-classifier",
        processing_time_ms=130.0,
        created_at=datetime.now(timezone.utc).isoformat(),
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


@pytest.fixture
def sample_casa_model() -> CASASchema:
    return CASASchema(
        applicant=ApplicantDetails(
            full_name="ARUN VERMA",
            pan_card_number="ABCDE1234F",
        ),
        account_details=AccountDetails(
            account_type="SAVINGS",
        ),
    )


# ==============================================================================
# 1. Local Model Configuration Validation & Guardrails
# ==============================================================================

class TestSettingsAndConfiguration:
    """Tests environment configuration and zero-egress guardrails (ARC-001, SEC-011)."""

    def test_default_settings_for_local_llm(self):
        settings = Settings()
        assert settings.llm_model_name == "ollama/qwen2.5:7b-instruct"
        assert settings.llm_api_base == "http://localhost:11434"
        assert settings.llm_provider == "ollama"
        assert settings.llm_use_inference is False
        assert settings.llm_allow_fallback is False
        assert settings.llm_timeout_seconds == 30.0
        assert settings.allow_external_egress is False

    def test_settings_custom_override(self):
        settings = Settings(
            llm_model_name="ollama/custom-qwen:latest",
            llm_api_base="http://127.0.0.1:8000",
            llm_provider="vllm",
            llm_use_inference=True,
            llm_allow_fallback=True,
            llm_timeout_seconds=45.0,
        )
        assert settings.llm_model_name == "ollama/custom-qwen:latest"
        assert settings.llm_api_base == "http://127.0.0.1:8000"
        assert settings.llm_provider == "vllm"
        assert settings.llm_use_inference is True
        assert settings.llm_allow_fallback is True
        assert settings.llm_timeout_seconds == 45.0

    @pytest.mark.parametrize(
        "cloud_url",
        [
            "https://api.openai.com/v1",
            "https://api.anthropic.com",
            "https://generativelanguage.googleapis.com/v1beta",
            "https://api.cohere.ai/v1",
            "https://bedrock.amazonaws.com",
        ],
    )
    def test_cloud_endpoints_rejected_by_guardrail(self, cloud_url: str):
        with pytest.raises(ValidationError) as exc_info:
            Settings(llm_api_base=cloud_url)
        assert "violating on-prem zero-egress policy" in str(exc_info.value)

    def test_external_egress_rejected_by_guardrail(self):
        with pytest.raises(ValidationError) as exc_info:
            Settings(allow_external_egress=True)
        assert "violates ARC-001/SEC-011" in str(exc_info.value)


# ==============================================================================
# 2. SchemaGuidedExtractor Configuration & Factory Methods
# ==============================================================================

class TestSchemaGuidedExtractorConfiguration:
    """Validates extractor factory constructors and parameter resolution."""

    def test_for_local_ollama_factory_defaults(self):
        extractor = SchemaGuidedExtractor.for_local_ollama()
        assert extractor.model_name == "ollama/qwen2.5:7b-instruct"
        assert extractor.provider_name == "ollama"
        assert extractor.api_base == "http://localhost:11434"
        assert extractor.use_llm is True
        assert extractor.allow_fallback is False
        assert extractor.timeout_seconds == 30.0

    def test_from_settings_factory(self):
        settings = Settings(
            llm_model_name="ollama/qwen2.5:7b-instruct",
            llm_api_base="http://localhost:11434",
            llm_provider="ollama",
            llm_use_inference=True,
            llm_allow_fallback=False,
            llm_timeout_seconds=25.0,
        )
        extractor = SchemaGuidedExtractor.from_settings(settings)
        assert extractor.model_name == "ollama/qwen2.5:7b-instruct"
        assert extractor.provider_name == "ollama"
        assert extractor.api_base == "http://localhost:11434"
        assert extractor.use_llm is True
        assert extractor.allow_fallback is False
        assert extractor.timeout_seconds == 25.0

    def test_model_name_auto_resolution_to_ollama_prefix(self, mock_module_b_doc01_payload, sample_casa_model):
        """Tests that passing 'Qwen2.5-7B-Instruct' resolves to 'ollama/qwen2.5:7b-instruct'."""
        extractor = SchemaGuidedExtractor(
            model_name="Qwen2.5-7B-Instruct",
            provider_name="ollama",
            api_base="http://localhost:11434",
            use_llm=True,
            allow_fallback=False,
        )

        mock_client = MagicMock()
        mock_raw_completion = MagicMock()
        mock_raw_completion.id = "chatcmpl-qwen-001"
        mock_client.chat.completions.create_with_completion.return_value = (sample_casa_model, mock_raw_completion)

        with patch("instructor.from_litellm", return_value=mock_client):
            res = extractor.extract_with_provenance(mock_module_b_doc01_payload)

        call_kwargs = mock_client.chat.completions.create_with_completion.call_args[1]
        assert call_kwargs["model"] == "ollama/qwen2.5:7b-instruct"
        assert call_kwargs["custom_llm_provider"] == "ollama"
        assert call_kwargs["api_base"] == "http://localhost:11434"
        assert call_kwargs["_skip_mcp_handler"] is True
        assert res.provenance.provider == "ollama"


# ==============================================================================
# 3. Successful Mocked Inference Execution
# ==============================================================================

class TestMockedInferenceExecution:
    """Tests successful structured extraction with LiteLLM/Instructor mocking."""

    def test_successful_mocked_casa_inference(self, mock_module_b_doc01_payload, sample_casa_model):
        extractor = SchemaGuidedExtractor.for_local_ollama()

        mock_client = MagicMock()
        mock_raw_completion = MagicMock()
        mock_raw_completion.id = "ollama-resp-12345"
        mock_client.chat.completions.create_with_completion.return_value = (sample_casa_model, mock_raw_completion)

        with patch("instructor.from_litellm", return_value=mock_client):
            result = extractor.extract_with_provenance(mock_module_b_doc01_payload)

        assert isinstance(result, ExtractionResult)
        assert result.payload == sample_casa_model
        assert result.provenance.mode == ExecutionMode.GENUINE_LLM
        assert result.provenance.model_name == "ollama/qwen2.5:7b-instruct"
        assert result.provenance.provider == "ollama"
        assert result.provenance.fallback_occurred is False
        assert result.provenance.request_id == "ollama-resp-12345"

        # Verify arguments passed to instructor/litellm
        mock_client.chat.completions.create_with_completion.assert_called_once()
        call_kwargs = mock_client.chat.completions.create_with_completion.call_args[1]
        assert call_kwargs["model"] == "ollama/qwen2.5:7b-instruct"
        assert call_kwargs["api_base"] == "http://localhost:11434"
        assert call_kwargs["timeout"] == 30.0
        assert call_kwargs["custom_llm_provider"] == "ollama"
        assert call_kwargs["temperature"] == 0.0
        assert call_kwargs["_skip_mcp_handler"] is True
        assert call_kwargs["response_model"] == CASASchema

    def test_successful_mocked_insurance_inference(self, mock_module_b_doc02_payload):
        ins_model = InsuranceApplicationSchema(
            proposer_name="PRIYA SHARMA",
            sum_insured=500000.0,
            policy_term_years=20,
            nominees=[NomineeDetails(name="AMIT SHARMA", relationship="SPOUSE", share_percentage=100.0)],
        )
        extractor = SchemaGuidedExtractor.for_local_ollama()

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (ins_model, None)

        with patch("instructor.from_litellm", return_value=mock_client):
            result = extractor.extract_with_provenance(mock_module_b_doc02_payload)

        assert isinstance(result.payload, InsuranceApplicationSchema)
        assert result.payload.proposer_name == "PRIYA SHARMA"
        assert result.payload.sum_insured == 500000.0
        assert result.provenance.mode == ExecutionMode.GENUINE_LLM

    def test_successful_mocked_discharge_summary_inference(self, mock_module_b_doc03_payload):
        dis_model = DischargeSummarySchema(
            patient_name="RAJESH GUPTA",
            admission_date="2026-01-10",
            discharge_date="2026-01-15",
            diagnosis="Acute Bronchitis",
            medications=[MedicationItem(medicine_name="Paracetamol", dosage="500mg", frequency="TDS")],
        )
        extractor = SchemaGuidedExtractor.for_local_ollama()

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (dis_model, None)

        with patch("instructor.from_litellm", return_value=mock_client):
            result = extractor.extract_with_provenance(mock_module_b_doc03_payload)

        assert isinstance(result.payload, DischargeSummarySchema)
        assert result.payload.patient_name == "RAJESH GUPTA"
        assert result.payload.admission_date == "2026-01-10"
        assert result.provenance.mode == ExecutionMode.GENUINE_LLM


# ==============================================================================
# 4. Structured Output Validation & Malformed Responses
# ==============================================================================

class TestStructuredOutputValidationAndErrors:
    """Tests validation errors when the model emits malformed or incomplete outputs."""

    def test_dict_model_validation_success(self, mock_module_b_doc01_payload):
        """When LLM returns a valid dict, it is automatically validated into the target schema."""
        raw_dict = {
            "applicant": {"full_name": "ARUN VERMA", "pan_card_number": "ABCDE1234F"},
            "account_details": {"account_type": "SAVINGS"},
        }
        extractor = SchemaGuidedExtractor.for_local_ollama()

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (raw_dict, None)

        with patch("instructor.from_litellm", return_value=mock_client):
            result = extractor.extract_with_provenance(mock_module_b_doc01_payload)

        assert isinstance(result.payload, CASASchema)
        assert result.payload.applicant.full_name == "ARUN VERMA"

    def test_invalid_dict_schema_fails_with_llm_execution_error(self, mock_module_b_doc01_payload):
        """When LLM returns an invalid dict (e.g. invalid types), LLMExecutionError is raised."""
        bad_dict = {
            "applicant": "NotAnObject",  # Should be a dict
        }
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=False)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (bad_dict, None)

        with patch("instructor.from_litellm", return_value=mock_client):
            with pytest.raises(LLMExecutionError) as exc_info:
                extractor.extract_with_provenance(mock_module_b_doc01_payload)

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"
        assert exc_info.value.model_name == "ollama/qwen2.5:7b-instruct"
        assert extractor.last_provenance.fallback_occurred is False

    def test_empty_completion_raises_llm_execution_error(self, mock_module_b_doc01_payload):
        """When LLM returns empty/None completion, LLMExecutionError is raised."""
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=False)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (None, None)

        with patch("instructor.from_litellm", return_value=mock_client):
            with pytest.raises(LLMExecutionError) as exc_info:
                extractor.extract_with_provenance(mock_module_b_doc01_payload)

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"
        assert "empty completion" in str(exc_info.value)
        assert extractor.last_provenance.fallback_occurred is False

    def test_non_schema_object_raises_llm_execution_error(self, mock_module_b_doc01_payload):
        """When LLM returns unexpected object type (e.g. string or list), LLMExecutionError is raised."""
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=False)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (["unexpected", "list"], None)

        with patch("instructor.from_litellm", return_value=mock_client):
            with pytest.raises(LLMExecutionError) as exc_info:
                extractor.extract_with_provenance(mock_module_b_doc01_payload)

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"
        assert extractor.last_provenance.fallback_occurred is False


# ==============================================================================
# 5. Strict No-Fallback and Timeout / Connection Failures
# ==============================================================================

class TestStrictNoFallbackAndFailures:
    """Verifies that timeouts and connection errors fail closed without heuristic fallback."""

    def test_timeout_raises_llm_execution_error_without_fallback(self, mock_module_b_doc01_payload):
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=False)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.side_effect = TimeoutError("Ollama request timed out after 30s")

        with patch("instructor.from_litellm", return_value=mock_client):
            with patch.object(extractor, "_extract_heuristic") as mock_heuristic:
                with pytest.raises(LLMExecutionError) as exc_info:
                    extractor.extract_with_provenance(mock_module_b_doc01_payload)

                # Heuristic extraction must NEVER be called
                mock_heuristic.assert_not_called()

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"
        assert "timed out" in str(exc_info.value)
        assert extractor.last_provenance.fallback_occurred is False
        assert "timed out" in extractor.last_provenance.fallback_reason

    def test_connection_refused_raises_llm_execution_error_without_fallback(self, mock_module_b_doc01_payload):
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=False)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.side_effect = ConnectionRefusedError("[Errno 61] Connection refused")

        with patch("instructor.from_litellm", return_value=mock_client):
            with patch.object(extractor, "_extract_heuristic") as mock_heuristic:
                with pytest.raises(LLMExecutionError) as exc_info:
                    extractor.extract_with_provenance(mock_module_b_doc01_payload)

                # Strict fail-closed: heuristic fast-path not touched
                mock_heuristic.assert_not_called()

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"
        assert "Connection refused" in str(exc_info.value)
        assert extractor.last_provenance.fallback_occurred is False
        assert "Connection refused" in extractor.last_provenance.fallback_reason

    def test_fallback_enabled_permits_heuristic_execution_when_requested(self, mock_module_b_doc01_payload):
        """When allow_fallback=True is explicitly set, fallback to heuristic is permitted and recorded."""
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=True)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.side_effect = RuntimeError("Local model crash")

        with patch("instructor.from_litellm", return_value=mock_client):
            result = extractor.extract_with_provenance(mock_module_b_doc01_payload)

        # Successful fallback
        assert isinstance(result.payload, CASASchema)
        assert result.provenance.mode == ExecutionMode.HEURISTIC_FALLBACK
        assert result.provenance.fallback_occurred is True
        assert "Local model crash" in result.provenance.fallback_reason

    def test_compatibility_extract_llm_strictly_respects_no_fallback_policy(self, mock_module_b_doc01_payload):
        """_extract_llm compatibility method must raise LLMExecutionError and NOT silently fall back."""
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=False)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.side_effect = ConnectionRefusedError("Ollama offline")

        with patch("instructor.from_litellm", return_value=mock_client):
            with patch.object(extractor, "_extract_heuristic") as mock_heuristic:
                with pytest.raises(LLMExecutionError) as exc_info:
                    extractor._extract_llm(mock_module_b_doc01_payload, CASASchema)
                mock_heuristic.assert_not_called()

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"

    def test_compatibility_extract_llm_allows_fallback_when_configured(self, mock_module_b_doc01_payload):
        """_extract_llm compatibility method falls back to heuristic only when allow_fallback=True."""
        extractor = SchemaGuidedExtractor.for_local_ollama(allow_fallback=True)

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.side_effect = RuntimeError("Model error")

        with patch("instructor.from_litellm", return_value=mock_client):
            result = extractor._extract_llm(mock_module_b_doc01_payload, CASASchema)

        assert isinstance(result, CASASchema)


# ==============================================================================
# 6. Evidence Grounding & Module B Integration
# ==============================================================================

class TestEvidenceGroundingWithModuleB:
    """Tests that extracted values are grounded against Module B OCR tokens and layout blocks."""

    def test_evidence_grounding_matches_module_b_ocr_blocks(self, mock_module_b_doc01_payload, sample_casa_model):
        extractor = SchemaGuidedExtractor.for_local_ollama()

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (sample_casa_model, MagicMock(id="id-1"))

        pipeline = ModuleCPipeline(extractor=extractor)

        with patch("instructor.from_litellm", return_value=mock_client):
            envelope = pipeline.process(mock_module_b_doc01_payload)

        assert isinstance(envelope, CanonicalExtractionEnvelope)

        # Verify applicant.full_name is grounded
        name_field = envelope.fields["applicant.full_name"]
        assert name_field.value == "ARUN VERMA"
        assert name_field.evidence is not None
        assert name_field.evidence.bbox[0] == 50.0
        assert name_field.evidence.bbox[1] == 100.0
        assert name_field.evidence.page == 1
        assert "ARUN VERMA" in name_field.evidence.text
        assert name_field.confidence >= 0.9

        # Verify applicant.pan_card_number is grounded
        pan_field = envelope.fields["applicant.pan_card_number"]
        assert pan_field.value == "ABCDE1234F"
        assert pan_field.evidence is not None
        assert pan_field.evidence.bbox[0] == 50.0
        assert pan_field.evidence.bbox[1] == 140.0

    def test_ungrounded_hallucination_is_nullified_with_flag(self, mock_module_b_doc01_payload):
        """Values hallucinated by LLM that do NOT exist in OCR tokens are set to null (FR-EXT-004, GR-002, GR-003)."""
        hallucinated_model = CASASchema(
            applicant=ApplicantDetails(
                full_name="NON_EXISTENT_NAME_XYZ",  # Not in OCR blocks
                pan_card_number="ABCDE1234F",
            ),
            account_details=AccountDetails(account_type="SAVINGS"),
        )
        extractor = SchemaGuidedExtractor.for_local_ollama()

        mock_client = MagicMock()
        mock_client.chat.completions.create_with_completion.return_value = (hallucinated_model, None)

        pipeline = ModuleCPipeline(extractor=extractor)

        with patch("instructor.from_litellm", return_value=mock_client):
            envelope = pipeline.process(mock_module_b_doc01_payload)

        # Field must be nullified per FR-EXT-004 and flagged EVIDENCE_MISSING
        name_field = envelope.fields["applicant.full_name"]
        assert name_field.value is None
        assert name_field.evidence is None
        assert name_field.confidence is None

        flag_codes = [f.code for f in envelope.flags]
        assert "EVIDENCE_MISSING" in flag_codes


# ==============================================================================
# 7. Module C Pipeline Local LLM Factory & Orchestration
# ==============================================================================

class TestModuleCPipelineLocalLLMIntegration:
    """Validates ModuleCPipeline factories and execution provenance metadata integration."""

    def test_pipeline_with_local_llm_factory(self):
        pipeline = ModuleCPipeline.with_local_llm(
            model_name="ollama/qwen2.5:7b-instruct",
            api_base="http://localhost:11434",
            use_llm=True,
            allow_fallback=False,
            timeout_seconds=20.0,
        )
        assert pipeline.extractor.model_name == "ollama/qwen2.5:7b-instruct"
        assert pipeline.extractor.use_llm is True
        assert pipeline.extractor.allow_fallback is False
        assert pipeline.extractor.api_base == "http://localhost:11434"
        assert pipeline.extractor.timeout_seconds == 20.0

    def test_pipeline_from_settings(self):
        settings = Settings(
            llm_model_name="ollama/qwen2.5:7b-instruct",
            llm_api_base="http://localhost:11434",
            llm_use_inference=True,
            llm_allow_fallback=False,
        )
        pipeline = ModuleCPipeline(settings=settings)
        assert pipeline.extractor.model_name == "ollama/qwen2.5:7b-instruct"
        assert pipeline.extractor.use_llm is True
        assert pipeline.extractor.allow_fallback is False

    def test_pipeline_provenance_metadata_in_run(self, mock_module_b_doc01_payload, sample_casa_model):
        pipeline = ModuleCPipeline.with_local_llm()

        mock_client = MagicMock()
        mock_raw_completion = MagicMock()
        mock_raw_completion.id = "qwen-resp-999"
        mock_client.chat.completions.create_with_completion.return_value = (sample_casa_model, mock_raw_completion)

        with patch("instructor.from_litellm", return_value=mock_client):
            envelope = pipeline.process(mock_module_b_doc01_payload)

        assert envelope.run.model_versions["extractor"] == "ollama/qwen2.5:7b-instruct"
        assert envelope.run.model_versions["parser"] == "pdfium-text"
        assert envelope.run.provenance is not None
        assert envelope.run.provenance.mode == ExecutionMode.GENUINE_LLM
        assert envelope.run.provenance.provider == "ollama"
        assert envelope.run.provenance.request_id == "qwen-resp-999"

    def test_pipeline_unknown_document_routes_to_review_before_llm(self, mock_module_b_doc01_payload):
        """Documents classified as UNKNOWN bypass LLM inference completely."""
        mock_module_b_doc01_payload.classification.document_type = MVPDocumentType.UNKNOWN
        mock_module_b_doc01_payload.status = "needs_review"

        pipeline = ModuleCPipeline.with_local_llm()

        with patch.object(pipeline.extractor, "_extract_llm_internal") as mock_llm:
            envelope = pipeline.process(mock_module_b_doc01_payload)
            mock_llm.assert_not_called()

        assert envelope.document_metadata.status == "needs_review"
        assert len(envelope.flags) == 1
        assert envelope.flags[0].code == "CLASSIFICATION_UNCERTAIN"


# ==============================================================================
# 8. Live Local Endpoint Fail-Closed Probe (Unmocked)
# ==============================================================================

class TestLiveLocalEndpointBehavior:
    """
    Verifies actual local socket behavior against the Ollama endpoint.
    When Ollama daemon is offline, proves strict fail-closed rejection with LLMExecutionError.
    """

    def test_live_local_ollama_offline_fails_closed_without_fallback(self, mock_module_b_doc01_payload):
        """
        Executes a live call to http://localhost:11434 with no mocks.
        Demonstrates that when the local model daemon is not running, the system fails closed
        with LLMExecutionError and NEVER falls back to heuristic extraction.
        """
        extractor = SchemaGuidedExtractor.for_local_ollama(
            model_name="ollama/qwen2.5:7b-instruct",
            api_base="http://localhost:11434",
            use_llm=True,
            allow_fallback=False,
            timeout_seconds=2.0,  # Fast timeout for test
        )

        with pytest.raises(LLMExecutionError) as exc_info:
            extractor.extract_with_provenance(mock_module_b_doc01_payload)

        assert exc_info.value.code == "LLM_EXECUTION_FAILED"
        assert exc_info.value.model_name == "ollama/qwen2.5:7b-instruct"

        prov = extractor.last_provenance
        assert prov is not None
        assert prov.mode == ExecutionMode.GENUINE_LLM
        assert prov.provider == "ollama"
        assert prov.fallback_occurred is False
        assert prov.fallback_reason is not None


# Live inference verification against a real Ollama server (text model qwen2.5:7b-instruct; this
# does NOT exercise the Qwen-VL / Transformers multimodal path).
#
# Opt-in integration test: skipped by default, so a normal run never depends on an external service.
# Enable with RUN_OLLAMA_TESTS=1 (optionally OLLAMA_API_BASE=http://host:port). Once enabled, an
# unreachable server or a failed extraction is a genuine FAILURE - it is never skipped or swallowed.
#   pytest -m live_ollama            (selects it; still skipped unless RUN_OLLAMA_TESTS=1)
#   RUN_OLLAMA_TESTS=1 pytest -m live_ollama
@pytest.mark.live_ollama
@pytest.mark.skipif(
    os.environ.get("RUN_OLLAMA_TESTS") != "1",
    reason="Live Ollama integration test is opt-in: set RUN_OLLAMA_TESTS=1 (requires a running Ollama server)",
)
def test_live_qwen_smoke_against_local_ollama(mock_module_b_doc01_payload):
    extractor = SchemaGuidedExtractor.for_local_ollama(
        model_name="ollama/qwen2.5:7b-instruct",
        api_base=os.environ.get("OLLAMA_API_BASE", "http://localhost:11434"),
        use_llm=True,
        allow_fallback=False,
        timeout_seconds=120.0,
    )

    result = extractor.extract_with_provenance(
        mock_module_b_doc01_payload
    )

    print("\n========== QWEN EXTRACTION OUTPUT ==========")
    print(result.payload.model_dump_json(indent=2))

    print("\n========== EXECUTION PROVENANCE ==========")
    print(result.provenance.model_dump_json(indent=2))

    assert result.provenance.mode == ExecutionMode.GENUINE_LLM
    assert result.provenance.fallback_occurred is False
    assert result.provenance.fallback_reason is None
    assert result.provenance.is_genuine_llm()
