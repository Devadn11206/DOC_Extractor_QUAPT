"""
Tests for API-004: POST /api/v1/documents/{id}/extract-sync (Milestone 2D, PROVISIONAL).

The real stack runs: real upload (Module A), real handoff and page images in storage, the real ``DocumentService`` /
``DocumentProcessingService.process_stored_page`` and the real Module B loading. Mocks stand only at the genuine external
boundaries of the extraction itself: the routed model extractor (``RoutedExtractor``: classification and field extraction by
a model), Module C's canonical assembly (the envelope) and, where a failure is injected, Module B's model inference.

What is verified: the exact route, authentication and scope, tenant isolation with the concealed 404, the single-page rule
(zero or several pages are a 422), the redacted public response (values and evidence text), the sanitized mapping of every
processing failure, the 30 s deadline with an execution slot that stays occupied until the worker really ends, overload
without a queue, the event loop staying responsive, and that no value, evidence text, path or exception text reaches a
response or an API log record.
"""

import ast
import asyncio
import inspect
import json
import logging
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.contracts.documents import SCOPE_DOCUMENTS_READ, SCOPE_DOCUMENTS_WRITE
from doc_extractor.api.contracts.extraction import ExtractionResponse
from doc_extractor.api.ports.authentication import ApiPrincipal
from doc_extractor.api.services import document_extraction_service as extraction_module
from doc_extractor.api.services.document_extraction_service import ExtractionGate, public_failure
from doc_extractor.api.services.document_service import DocumentService
from doc_extractor.contracts_c import (
    CanonicalExtractionEnvelope,
    DocumentMetadata,
    EvidenceSnippet,
    ExecutionMode,
    ExecutionProvenance,
    ExtractedField,
    FlagItem,
    RunMetadata,
)
from doc_extractor.exceptions import DocumentProcessingError, LLMExecutionError, StorageError, TenantMismatchError
from doc_extractor.malware import MockMalwareScanner
from doc_extractor.pipeline_b import ModuleBPipeline
from doc_extractor.pipeline_c import ModuleCPipeline
from doc_extractor.routing import RoutedExtractor
from tests.conftest import create_minimal_pdf, create_minimal_png
from tests.test_api_documents_page_image import MockAuthProvider
from tests.test_api_documents_upload import _call_app, _empty_body, _run_loop

UPLOAD = "/api/v1/documents"
RW = {"Authorization": "Bearer rw"}
SECRET_VALUE = "SECRET-VALUE-123"
SECRET_TEXT = "SECRET-EVIDENCE-TEXT-456"
SECRET_FLAG = "SECRET-FLAG-MESSAGE-789"
SECRET_MODEL = "SECRET-MODEL-NAME"
SECRET_FALLBACK = "SECRET-FALLBACK-REASON /srv/secret/path"
SECRET_ROUTE = "SECRET-ROUTE-INFO"
SECRET_FILENAME = "SECRET-FILENAME-ABC.png"


def _envelope(document_id: str) -> CanonicalExtractionEnvelope:
    return CanonicalExtractionEnvelope(
        document_id=document_id,
        document_metadata=DocumentMetadata(
            document_type="DOC-01", schema_id="casa_account_opening", schema_version="1.0.0",
            classification_confidence=0.8, status="needs_review", page_count=1, pages_processed=1,
            document_category="structured", language="en",
            pages=[{"page": 1, "model": SECRET_MODEL}],
        ),
        fields={
            "applicant.full_name": ExtractedField(
                value=SECRET_VALUE, confidence=0.91, review_required=False, validation="passed",
                evidence=EvidenceSnippet(page=1, bbox=(10.0, 20.0, 110.0, 40.0), text=SECRET_TEXT),
                additional_evidence=[EvidenceSnippet(page=1, bbox=(1.0, 2.0, 3.0, 4.0), text=SECRET_TEXT + "-2")],
            ),
            "applicant.date_of_birth": ExtractedField(value=None, null_reason="absent", review_required=True, validation="not_applicable"),
        },
        flags=[FlagItem(code="ARITHMETIC_MISMATCH", message=f"total {SECRET_FLAG} {SECRET_VALUE}", fields=["applicant.full_name"])],
        run=RunMetadata(
            run_id="run_api004", model_versions={"vlm": SECRET_MODEL}, prompt_version="1.0.0",
            provenance=ExecutionProvenance(
                mode=ExecutionMode.GENUINE_LLM, model_name=SECRET_MODEL, fallback_reason=SECRET_FALLBACK,
                details={"prompt": "SECRET-PROMPT"},
            ),
            routing=[{"page": 1, "note": SECRET_ROUTE}],
        ),
    )


def _make_api(tmp_path, **settings_overrides):
    routed = MagicMock(spec=RoutedExtractor)
    routed.classify.return_value = {
        "category": "structured", "document_type": None, "subtype": None, "language": "en", "confidence": None,
        "method": "vlm", "rotation": 0, "router_model": "test_router", "filled_by": None,
    }
    routed.extract.return_value = (
        {"full_name": {"value": SECRET_VALUE, "bbox": [10, 10, 50, 20]}},
        ExecutionProvenance(mode=ExecutionMode.GENUINE_LLM, model_name="vlm_model"),
        {"model_used": "vlm_model", "fallback_occurred": False, "grounded": True},
    )
    module_c = MagicMock(spec=ModuleCPipeline)
    envelope_holder = {}

    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path), **settings_overrides)
    container = ApplicationContainer(
        api_settings=settings, auth_provider=MockAuthProvider(), malware_scanner=MockMalwareScanner(),
        routed_extractor=routed, module_c=module_c,
    )
    app = create_app(settings=settings, container=container)
    principals = app.state.auth_provider.principals
    principals["rw"] = ApiPrincipal(
        principal_id="rw", tenant_id="tenant_alpha", scopes={SCOPE_DOCUMENTS_READ, SCOPE_DOCUMENTS_WRITE}, is_active=True
    )
    principals["beta-rw"] = ApiPrincipal(
        principal_id="beta", tenant_id="tenant_beta", scopes={SCOPE_DOCUMENTS_READ, SCOPE_DOCUMENTS_WRITE}, is_active=True
    )
    principals["alpha-reader"] = ApiPrincipal(
        principal_id="reader", tenant_id="tenant_alpha", scopes={SCOPE_DOCUMENTS_READ}, is_active=True
    )
    api = {
        "app": app, "client": TestClient(app, raise_server_exceptions=False), "routed": routed, "module_c": module_c,
        "tmp_path": tmp_path, "holder": envelope_holder, "container": container,
    }

    def assemble_for_document(*args, **kwargs):
        # Module C assembles the envelope for the document being processed: found among its positional arguments
        document_id = args[0].document_id                        # the first argument is Module B's payload
        envelope = _envelope(document_id)
        envelope_holder["envelope"] = envelope
        envelope_holder["snapshot"] = envelope.model_dump_json()
        return envelope

    module_c.process_routed_pages.side_effect = assemble_for_document
    return api


@pytest.fixture
def api(tmp_path):
    return _make_api(tmp_path)


class _Capture(logging.Handler):
    """Every record as its FORMATTED message plus its attributes (independent of any other test's logging setup)."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        try:
            message = record.getMessage()
        except Exception:                                   # noqa: BLE001
            message = str(record.msg)
        self.records.append(message + " " + json.dumps(record.__dict__, default=str))

    @property
    def text(self) -> str:
        return "\n".join(self.records)


_LOGGER_NAMES = ("", "doc_extractor", "doc_extractor.api.errors", "doc_extractor.api.extraction")


@pytest.fixture
def logs():
    handler = _Capture()
    targets = [logging.getLogger(name) for name in _LOGGER_NAMES]
    saved = [(t, t.level, t.disabled) for t in targets]
    for target in targets:
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
        target.disabled = False
    try:
        yield handler
    finally:
        for target, level, disabled in saved:
            target.removeHandler(handler)
            target.setLevel(level)
            target.disabled = disabled


def _upload(api, content=None, name=SECRET_FILENAME, ct="image/png", headers=RW):
    content = create_minimal_png() if content is None else content
    response = api["client"].post(UPLOAD, files={"file": (name, content, ct)}, headers=headers)
    assert response.status_code == 202, response.text
    return response.json()["document_id"]


def _extract(api, document_id, headers=RW):
    return api["client"].post(f"{UPLOAD}/{document_id}/extract-sync", headers=headers)


def _doc_dir(api, document_id, tenant="tenant_alpha"):
    return api["tmp_path"] / tenant / document_id


def _error(response):
    return response.json()["error"]


def _assert_generic_500(response, *forbidden):
    assert response.status_code == 500, response.text
    body = _error(response)
    assert body["code"] == "INTERNAL_ERROR" and body["details"] == {} and body["request_id"]
    for leak in (*forbidden, "SECRET", "/srv", "Traceback"):
        assert leak not in response.text


# ============================================================================
# Success, response contract and redaction
# ============================================================================

def test_extract_sync_returns_the_redacted_public_envelope(api, logs):
    document_id = _upload(api)
    response = _extract(api, document_id)
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.json()
    assert set(body) == {"document_id", "document_metadata", "fields", "flags", "run", "redaction"}
    assert body["document_id"] == document_id
    assert body["document_metadata"]["document_type"] == "DOC-01" and body["document_metadata"]["page_count"] == 1
    assert body["redaction"] == {
        "values": "redacted", "evidence_text": "redacted", "flag_messages": "redacted",
        "field_names": "schema-defined identifiers only; others are positional (field_N)",
        "free_text_metadata": "category, subtype and language omitted unless in a constrained set",
        "policy": "provisional: no approved masking policy yet",
    }
    name = body["fields"]["applicant.full_name"]
    assert name["value"] is None and name["value_present"] is True, "the value is redacted, only its presence is shown"
    assert name["confidence"] == 0.91 and name["confidence_provenance"] == "uncalibrated" and name["validation"] == "passed"
    assert name["evidence"] == {"page": 1, "bbox": [10.0, 20.0, 110.0, 40.0]}, "geometry only, no text"
    assert name["additional_evidence"] == [{"page": 1, "bbox": [1.0, 2.0, 3.0, 4.0]}]
    absent = body["fields"]["applicant.date_of_birth"]
    assert absent["value"] is None and absent["value_present"] is False and absent["null_reason"] == "absent"
    assert absent["review_required"] is True and absent["evidence"] is None
    assert body["flags"] == [{"code": "ARITHMETIC_MISMATCH", "fields": ["applicant.full_name"]}]
    assert body["run"] == {"run_id": "run_api004", "prompt_version": "1.0.0", "confidence_calibrated": False}
    # the existing pipeline ran once, for page 1
    assert api["module_c"].process_routed_pages.call_count == 1


def test_nothing_sensitive_reaches_the_response_or_any_log_record(api, logs):
    document_id = _upload(api)
    response = _extract(api, document_id)
    assert response.status_code == 200
    for secret in (SECRET_VALUE, SECRET_TEXT, SECRET_FLAG, SECRET_MODEL, SECRET_FALLBACK, SECRET_ROUTE, "SECRET-PROMPT",
                   SECRET_FILENAME, "/srv/secret", str(api["tmp_path"]), '"model_versions"', '"provenance"', '"routing"'):
        assert secret not in response.text, f"{secret!r} in the response"
    for secret in (SECRET_VALUE, SECRET_TEXT, SECRET_FLAG, SECRET_FALLBACK, SECRET_ROUTE, "SECRET-PROMPT", SECRET_FILENAME):
        assert secret not in logs.text, f"{secret!r} in a log record"


def test_the_internal_envelope_is_left_intact_by_the_redaction(api):
    document_id = _upload(api)
    assert _extract(api, document_id).status_code == 200
    envelope = api["holder"]["envelope"]
    assert envelope.fields["applicant.full_name"].value == SECRET_VALUE
    assert envelope.fields["applicant.full_name"].evidence.text == SECRET_TEXT
    assert envelope.flags[0].message and SECRET_FLAG in envelope.flags[0].message
    # the serializer itself reads without writing: dumping before and after gives the same internal object
    before = envelope.model_dump_json()
    ExtractionResponse.from_envelope(envelope)
    assert envelope.model_dump_json() == before


def test_the_public_serializer_is_explicit_and_forbids_extra_fields():
    envelope = _envelope("doc_0123456789ab")
    public = ExtractionResponse.from_envelope(envelope)
    dumped = public.model_dump_json()
    for secret in (SECRET_VALUE, SECRET_TEXT, SECRET_FLAG, SECRET_MODEL, SECRET_FALLBACK, SECRET_ROUTE):
        assert secret not in dumped
    assert ExtractionResponse.model_config["extra"] == "forbid"
    with pytest.raises(Exception):
        ExtractionResponse(**{**public.model_dump(), "evidence_text": "x"})
    from doc_extractor.api.contracts.extraction import PublicField
    with pytest.raises(Exception):
        PublicField(value="not redacted", value_present=True, confidence_provenance="x", review_required=False, validation="passed")


# ============================================================================
# M1: model-generated names and free-text metadata never reach the response
# ============================================================================

HOSTILE_KEY = "John Doe 4111"
HOSTILE_KEY_2 = "ssn 123-45-6789 holder"
HOSTILE_META = {"category": "John Doe 4111 category", "subtype": "SECRET subtype Jane Roe", "language": "xx-SECRET-LANG"}
_HOSTILE_STRINGS = (HOSTILE_KEY, HOSTILE_KEY_2, "John", "Doe", "4111", "123-45-6789", "Jane Roe", "SECRET subtype", "xx-SECRET-LANG",
                    "ssn")


def _hostile_envelope(document_type="generic", fields=None, flag_refs=None, meta=None, schema_id=None):
    meta = HOSTILE_META if meta is None else meta
    return CanonicalExtractionEnvelope(
        document_id="doc_0123456789ab",
        document_metadata=DocumentMetadata(
            document_type=document_type, schema_id=schema_id or document_type, schema_version="1.0.0", classification_confidence=0.7,
            status="needs_review", page_count=1, pages_processed=1, document_category=meta["category"],
            document_subtype=meta["subtype"], language=meta["language"],
        ),
        fields=fields if fields is not None else {
            HOSTILE_KEY: ExtractedField(value="SECRET-V", confidence=0.5, evidence=EvidenceSnippet(page=1, bbox=(1.0, 2.0, 3.0, 4.0), text="SECRET-T")),
            HOSTILE_KEY_2: ExtractedField(value=None, null_reason="absent"),
        },
        flags=[FlagItem(code="EVIDENCE_MISSING", message=f"no evidence for '{HOSTILE_KEY}'",
                        fields=flag_refs if flag_refs is not None else [HOSTILE_KEY, HOSTILE_KEY_2, "never.seen John Doe 4111"])],
        run=RunMetadata(run_id="run_x"),
    )


def _serialized(envelope):
    return ExtractionResponse.from_envelope(envelope).model_dump_json()


def _assert_no_hostile_text(text):
    for hostile in _HOSTILE_STRINGS:
        assert hostile not in text, f"{hostile!r} reached the serialized response: {text}"


def test_generic_extraction_gets_positional_identifiers_and_no_model_generated_name():
    envelope = _hostile_envelope()
    public = ExtractionResponse.from_envelope(envelope)
    text = public.model_dump_json()
    _assert_no_hostile_text(text)
    assert list(public.fields) == ["field_1", "field_2"]
    assert public.fields["field_1"].value_present is True and public.fields["field_2"].null_reason == "absent"
    # the flag's raw references went through the same mapping; the reference nobody extracted is dropped, never copied
    assert [f.fields for f in public.flags] == [["field_1", "field_2"]]
    # free-text metadata is omitted (none of the hostile values is in a constrained set)
    meta = public.document_metadata
    assert (meta.document_category, meta.document_subtype, meta.language) == (None, None, None)


def test_a_document_type_that_is_not_a_bound_schema_is_not_trusted_even_with_a_canonical_looking_key():
    """A schema id or document type that merely exists is not a binding: only a key of SCHEMA_MAP is."""
    for document_type in ("generic", "DOC-99", "casa_account_opening", "UNKNOWN"):
        envelope = _hostile_envelope(
            document_type=document_type,
            fields={"applicant.full_name": ExtractedField(value="SECRET-V")},
            flag_refs=["applicant.full_name"],
        )
        public = ExtractionResponse.from_envelope(envelope)
        assert list(public.fields) == ["field_1"], document_type
        assert public.flags[0].fields == ["field_1"]
        assert "applicant" not in public.model_dump_json()


def test_a_bound_schema_exposes_only_its_own_canonical_identifiers_and_remaps_the_rest():
    envelope = _hostile_envelope(
        document_type="DOC-02", schema_id="DOC-02",
        fields={
            "nominees.0.name": ExtractedField(value="SECRET-V"),
            HOSTILE_KEY: ExtractedField(value="SECRET-V2"),
            "medical_declaration.has_hypertension": ExtractedField(value=None, null_reason="absent"),
            "nominees.John Doe 4111": ExtractedField(value="x"),                  # a schema-looking prefix is not enough
            "medical_declaration.has_diabetes": ExtractedField(value=True),
        },
        flag_refs=["nominees.0.name", HOSTILE_KEY, "nominees.John Doe 4111", "nominees.share_percentage", "nope"],
    )
    public = ExtractionResponse.from_envelope(envelope)
    assert list(public.fields) == [
        "nominees.0.name", "field_1", "medical_declaration.has_hypertension", "field_2", "medical_declaration.has_diabetes",
    ]
    # canonical names stay (also a schema path that has no entry of its own); model-chosen ones are positional; unknown ones are dropped
    assert public.flags[0].fields == ["nominees.0.name", "field_1", "field_2", "nominees.share_percentage"]
    text = public.model_dump_json()
    _assert_no_hostile_text(text)
    assert "nope" not in text


@pytest.mark.parametrize("document_type, path", [
    ("DOC-01", "applicant.full_name"), ("DOC-01", "account_details.initial_deposit_amount"), ("DOC-02", "medical_declaration.has_diabetes"),
    ("DOC-03", "patient_name"), ("DOC-03", "medications.0.medicine_name"), ("DOC-02", "nominees.2.share_percentage"),
])
def test_real_schema_paths_including_list_indices_are_recognised_as_canonical(document_type, path):
    public = ExtractionResponse.from_envelope(_hostile_envelope(
        document_type=document_type, fields={path: ExtractedField(value="v")}, flag_refs=[path]))
    assert list(public.fields) == [path] and public.flags[0].fields == [path]


@pytest.mark.parametrize("path", ["nominees.1234.name", "nominees.-1.name", "nominees..name", ".applicant", "applicant.", "applicant.full_name.extra",
                                  "nominees.1a.name", "nominees.x.name", ""])
def test_malformed_or_foreign_paths_are_never_canonical(path):
    public = ExtractionResponse.from_envelope(_hostile_envelope(
        document_type="DOC-01", fields={path: ExtractedField(value="v")}, flag_refs=[path]))
    assert list(public.fields) == ["field_1"]
    assert public.flags[0].fields == ["field_1"]


def test_positional_ids_never_collide_with_a_canonical_identifier():
    public = ExtractionResponse.from_envelope(_hostile_envelope(
        document_type="DOC-01", fields={"field_1": ExtractedField(value="a"), "applicant.full_name": ExtractedField(value="b"),
                                        HOSTILE_KEY: ExtractedField(value="c")}, flag_refs=[]))
    assert len(set(public.fields)) == 3 and "applicant.full_name" in public.fields
    assert HOSTILE_KEY not in public.fields


def test_a_positional_id_is_never_the_name_of_a_canonical_identifier():
    from doc_extractor.api.contracts.extraction import _FieldNames
    names = _FieldNames(frozenset({"field_1"}), ["John Doe 4111", "field_1", "other"])      # a schema that owns the name field_1
    assert names.public("field_1") == "field_1"
    assert names.public("John Doe 4111") == "field_2" and names.public("other") == "field_3"
    assert len({names.public(k) for k in ("John Doe 4111", "field_1", "other")}) == 3


@pytest.mark.parametrize("meta, expected", [
    ({"category": "structured", "subtype": "brochure", "language": "en"}, ("structured", "brochure", "en")),
    ({"category": "handwritten", "subtype": "menu card", "language": "te"}, ("handwritten", "menu card", "te")),
    ({"category": "Structured", "subtype": "Brochure", "language": "EN"}, (None, None, None)),         # exact members only
    ({"category": "structured\nJohn Doe", "subtype": "brochure; John Doe", "language": "en John"}, (None, None, None)),
    ({"category": "", "subtype": "", "language": ""}, (None, None, None)),
])
def test_free_text_metadata_is_exposed_only_when_it_is_a_member_of_the_constrained_set(meta, expected):
    public = ExtractionResponse.from_envelope(_hostile_envelope(meta=meta))
    got = public.document_metadata
    assert (got.document_category, got.document_subtype, got.language) == expected
    assert "John" not in public.model_dump_json()


def test_the_serializer_leaves_the_internal_envelope_untouched():
    envelope = _hostile_envelope(document_type="DOC-01", fields={
        "applicant.full_name": ExtractedField(value="SECRET-V", evidence=EvidenceSnippet(page=1, bbox=(1.0, 2.0, 3.0, 4.0), text="SECRET-T")),
        HOSTILE_KEY: ExtractedField(value="x")})
    before = envelope.model_dump_json()
    ExtractionResponse.from_envelope(envelope)
    assert envelope.model_dump_json() == before
    assert HOSTILE_KEY in envelope.fields and envelope.document_metadata.document_subtype == HOSTILE_META["subtype"]
    assert HOSTILE_KEY in envelope.flags[0].message


def test_hostile_model_output_through_the_real_route_never_reaches_the_response_or_logs(tmp_path, logs):
    """End to end: Module C (stubbed boundary) returns an envelope full of hostile names; the HTTP response and the logs are clean."""
    api = _make_api(tmp_path)
    document_id = _upload(api)
    api["module_c"].process_routed_pages.side_effect = lambda payload_b, *a, **k: _hostile_envelope(
        document_type="generic", fields=None).model_copy(update={"document_id": payload_b.document_id})
    response = _extract(api, document_id)
    assert response.status_code == 200, response.text
    _assert_no_hostile_text(response.text)
    _assert_no_hostile_text(logs.text)
    body = response.json()
    assert sorted(body["fields"]) == ["field_1", "field_2"] and body["flags"][0]["fields"] == ["field_1", "field_2"]
    meta = body["document_metadata"]                    # the real service sets these from its classifier: only constrained members survive
    assert meta["document_category"] in (None, "structured", "unstructured", "handwritten") and meta["language"] in (None, "en", "hi", "te", "ta", "ml")
    assert meta["document_subtype"] in (None, "brochure", "pamphlet", "menu card", "CASA leaflet", "invitation card", "other")


def test_the_route_is_in_the_openapi_contract_with_its_errors(api):
    schema = api["app"].openapi()
    operation = schema["paths"]["/api/v1/documents/{document_id}/extract-sync"]["post"]
    assert {"200", "401", "403", "404", "422", "429", "500", "502", "504"} <= set(operation["responses"])
    assert "exactly one page" in operation["description"] and "redact" in operation["description"].lower()


# ============================================================================
# Authentication, scope, tenant isolation
# ============================================================================

def test_authentication_and_the_provisional_write_scope_are_required(api):
    document_id = _upload(api)
    assert api["client"].post(f"{UPLOAD}/{document_id}/extract-sync").status_code == 401
    forbidden = _extract(api, document_id, headers={"Authorization": "Bearer alpha-reader"})
    assert forbidden.status_code == 403 and _error(forbidden)["code"] == "FORBIDDEN_SCOPE"
    assert api["module_c"].process_routed_pages.call_count == 0


def test_another_tenants_document_and_an_unknown_id_are_the_same_concealed_404(api):
    document_id = _upload(api)
    foreign = _extract(api, document_id, headers={"Authorization": "Bearer beta-rw"})
    unknown = _extract(api, "doc_000000000000")
    for response in (foreign, unknown):
        assert response.status_code == 404
        body = _error(response)
        assert body["code"] == "DOCUMENT_NOT_FOUND" and body["details"] == {}
    assert _error(foreign)["message"] == _error(unknown)["message"]
    assert api["module_c"].process_routed_pages.call_count == 0 and api["routed"].extract.call_count == 0


@pytest.mark.parametrize("bad_id", ["..", "a" * 300, "doc_..%2f..%2fx", "doc 1", "doc%00x"])
def test_malformed_ids_are_the_concealed_404_without_processing(api, bad_id):
    response = api["client"].post(f"{UPLOAD}/{bad_id}/extract-sync", headers=RW)
    assert response.status_code in (404, 422)
    assert "Traceback" not in response.text and str(api["tmp_path"]) not in response.text
    assert api["module_c"].process_routed_pages.call_count == 0


def test_a_tenant_mismatch_raised_by_the_service_is_concealed_as_404(api, monkeypatch):
    document_id = _upload(api)

    def mismatch(self, *args, **kwargs):
        raise TenantMismatchError(message="alpha vs beta SECRET-TENANT")

    monkeypatch.setattr(DocumentService, "process_stored_page", mismatch)
    response = _extract(api, document_id)
    assert response.status_code == 404 and _error(response)["code"] == "DOCUMENT_NOT_FOUND"
    assert "SECRET-TENANT" not in response.text and "tenant_" not in response.text


# ============================================================================
# The single-page rule
# ============================================================================

def test_a_multipage_document_is_a_422_and_no_page_is_chosen(api):
    document_id = _upload(api, create_minimal_pdf(3), name="three.pdf", ct="application/pdf")
    response = _extract(api, document_id)
    assert response.status_code == 422
    error = _error(response)
    assert error["code"] == "VALIDATION_ERROR"
    detail = error["details"]["validation_errors"][0]
    assert detail["type"] == "unsupported_page_count" and detail["location"] == ["path", "document_id"]
    assert detail["message"] == "Synchronous extraction needs a document with exactly one page."
    assert api["module_c"].process_routed_pages.call_count == 0 and api["routed"].extract.call_count == 0


def test_a_zero_page_document_is_a_422(api):
    document_id = _upload(api)
    record_path = _doc_dir(api, document_id) / "document_record.json"
    record = json.loads(record_path.read_text())
    record["page_count"] = 0
    record_path.write_text(json.dumps(record))
    response = _extract(api, document_id)
    assert response.status_code == 422 and _error(response)["details"]["validation_errors"][0]["type"] == "unsupported_page_count"
    assert api["module_c"].process_routed_pages.call_count == 0


def test_a_single_page_pdf_is_accepted(api):
    document_id = _upload(api, create_minimal_pdf(1) + b"\n%one\n", name="one.pdf", ct="application/pdf")
    assert _extract(api, document_id).status_code == 200


# ============================================================================
# Processing failures: the REAL service's failed results, mapped and sanitized
# ============================================================================

def _break_handoff(api, document_id, how):
    path = _doc_dir(api, document_id) / "module_a_handoff.json"
    if how == "missing":
        path.unlink()
        return
    data = json.loads(path.read_text())
    if how == "garbage":
        path.write_text("{not json")
    elif how == "malformed":
        path.write_text(json.dumps({"document_id": document_id}))
    elif how == "other_page":
        data["pages"][0]["page_number"] = 2
        path.write_text(json.dumps(data))
    elif how == "rejected":
        data["status"] = "rejected"
        data["error"] = {"code": "UNSUPPORTED_FILE", "message": "SECRET rejection text"}
        path.write_text(json.dumps(data))
    elif how == "other_document":
        data["document_id"] = "doc_ffffffffffff"
        path.write_text(json.dumps(data))
    elif how == "no_page_image":
        for image in (_doc_dir(api, document_id) / "preprocessed").glob("*.png"):
            image.unlink()


@pytest.mark.parametrize("how, reason", [
    ("missing", "extract_handoff_not_found"),
    ("garbage", None),                                  # an unreadable stored file: a storage fault (generic 500)
    ("malformed", "extract_handoff_malformed"),
    ("other_document", "extract_handoff_malformed"),
    ("other_page", "extract_page_not_found"),
    ("no_page_image", "extract_module_b_error"),        # Module B cannot load the page image: its failed result
])
def test_stored_data_failures_are_sanitized_generic_500s(api, logs, how, reason):
    document_id = _upload(api)
    _break_handoff(api, document_id, how)
    response = _extract(api, document_id)
    _assert_generic_500(response, document_id)
    assert api["module_c"].process_routed_pages.call_count == 0
    if reason:
        assert any(f"reason={reason}" in record for record in logs.records), (how, reason)


def test_a_rejected_handoff_is_the_unsupported_file_error(api):
    document_id = _upload(api)
    _break_handoff(api, document_id, "rejected")
    response = _extract(api, document_id)
    assert response.status_code == 400
    body = _error(response)
    assert body["code"] == "UNSUPPORTED_FILE" and body["message"] == "This file type isn't supported." and body["details"] == {}
    assert "SECRET" not in response.text


@pytest.mark.parametrize("inject, reason", [
    ("module_b", "extract_module_b_error"),
    ("routing", "extract_routing_failed"),
    ("assembly", "extract_assembly_failed"),
])
def test_model_and_routing_failures_are_sanitized_generic_500s(api, monkeypatch, logs, inject, reason):
    document_id = _upload(api)
    boom = RuntimeError("SECRET-EXC /srv/secret/path CUDA out of memory")
    if inject == "module_b":
        monkeypatch.setattr(ModuleBPipeline, "process_handoff", lambda self, *a, **k: (_ for _ in ()).throw(boom))
    elif inject == "routing":
        api["routed"].classify.side_effect = boom
    else:
        api["module_c"].process_routed_pages.side_effect = boom
    response = _extract(api, document_id)
    _assert_generic_500(response, "CUDA", "RuntimeError")
    assert any(f"reason={reason}" in record for record in logs.records), inject


@pytest.mark.parametrize("raised, status, code", [
    (DocumentProcessingError(message="SECRET processing detail", details={"path": "/srv/secret"}), 500, "INTERNAL_ERROR"),
    (StorageError("SECRET storage /srv/secret", details={"reason": "disk_read_failed"}), 500, "INTERNAL_ERROR"),
    (LLMExecutionError(message="SECRET provider detail", details={"endpoint": "https://secret.example"}), 502, "LLM_EXECUTION_FAILED"),
    (RuntimeError("SECRET untyped /srv/secret"), 500, "INTERNAL_ERROR"),
    (ValueError("SECRET value error"), 500, "INTERNAL_ERROR"),
])
def test_exceptions_from_the_service_are_sanitized(api, monkeypatch, raised, status, code):
    document_id = _upload(api)

    def explode(self, *args, **kwargs):
        raise raised

    monkeypatch.setattr(DocumentService, "process_stored_page", explode)
    response = _extract(api, document_id)
    assert response.status_code == status and _error(response)["code"] == code
    assert _error(response)["details"] == {} and _error(response)["request_id"]
    for leak in ("SECRET", "/srv", "secret.example", "Traceback"):
        assert leak not in response.text


def test_public_failure_maps_every_known_processing_error_code():
    from types import SimpleNamespace
    codes = ["HANDOFF_NOT_FOUND", "MALFORMED_HANDOFF", "PAGE_NOT_FOUND", "STORAGE_ERROR", "STORAGE_RESOLUTION_FAILED",
             "INGESTION_ERROR", "MODULE_B_ERROR", "ROUTING_FAILED", "ROTATION_FAILED", "CANONICAL_ASSEMBLY_FAILED"]
    reasons = set()
    for code in codes:
        error = public_failure(SimpleNamespace(status="failed", error={"code": code, "message": "SECRET"}), "req-1")
        assert isinstance(error, StorageError) and error.details["reason"].startswith("extract_") and "SECRET" not in str(error)
        reasons.add(error.details["reason"])
    assert len(reasons) == len(codes), "every code has its own fixed reason"
    unknown = public_failure(SimpleNamespace(status="failed", error={"code": "WHATEVER", "message": "SECRET"}), "req-1")
    assert unknown.details == {"reason": "extract_failed"}
    assert public_failure(SimpleNamespace(status="failed", error=None), None).details == {"reason": "extract_failed"}


# ============================================================================
# Deadline, execution capacity and overload
# ============================================================================

def _block_extraction(monkeypatch, release: threading.Event, started: threading.Event):
    original = DocumentService.process_stored_page

    def slow(self, *args, **kwargs):
        started.set()
        release.wait(timeout=20)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(DocumentService, "process_stored_page", slow)


def _wait_until(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_a_timeout_is_a_sanitized_504_and_the_slot_stays_busy_until_the_worker_finishes(tmp_path, monkeypatch, logs):
    api = _make_api(tmp_path, extract_sync_timeout_seconds=0.4, max_concurrent_extractions=1)
    document_id = _upload(api)
    release, started = threading.Event(), threading.Event()
    _block_extraction(monkeypatch, release, started)

    t0 = time.monotonic()
    timed_out = _extract(api, document_id)
    elapsed = time.monotonic() - t0
    assert started.is_set()
    assert timed_out.status_code == 504 and elapsed < 5
    body = _error(timed_out)
    assert body["code"] == "EXTRACTION_TIMEOUT" and body["details"] == {} and body["request_id"]
    assert body["message"] == "The extraction did not finish in time."
    for leak in (document_id, SECRET_FILENAME, str(tmp_path), "Traceback"):
        assert leak not in timed_out.text
    gate = api["app"].state.extraction_gate
    assert gate.active == 1, "the abandoned worker still holds its slot"
    assert sum(1 for t in threading.enumerate() if t.name == "extract-sync") == 1

    # the only slot is busy: refused at once, no queue, no second worker thread
    t0 = time.monotonic()
    refused = _extract(api, document_id)
    assert refused.status_code == 429 and time.monotonic() - t0 < 2
    assert _error(refused)["code"] == "RATE_LIMITED" and refused.headers["Retry-After"] == "5"
    assert sum(1 for t in threading.enumerate() if t.name == "extract-sync") == 1

    release.set()
    assert _wait_until(lambda: gate.active == 0), "the slot is returned when the worker really ends"
    assert not any(t.name == "extract-sync" for t in threading.enumerate() if t.is_alive()) or _wait_until(
        lambda: not any(t.name == "extract-sync" and t.is_alive() for t in threading.enumerate()))
    assert _extract(api, document_id).status_code == 200
    assert any("reason=extract_timeout" in r for r in logs.records) and any("reason=extract_overloaded" in r for r in logs.records)
    assert SECRET_VALUE not in logs.text


def test_overload_is_refused_promptly_while_a_running_extraction_completes(tmp_path, monkeypatch):
    api = _make_api(tmp_path, max_concurrent_extractions=1)
    document_id = _upload(api)
    release, started = threading.Event(), threading.Event()
    _block_extraction(monkeypatch, release, started)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", _extract(api, document_id)))
    first.start()
    assert started.wait(5)
    t0 = time.monotonic()
    refused = _extract(api, document_id)
    assert refused.status_code == 429 and time.monotonic() - t0 < 2
    health = api["client"].get("/api/v1/health")
    assert health.status_code == 200
    release.set()
    first.join(20)
    assert results["first"].status_code == 200
    assert api["app"].state.extraction_gate.active == 0


@pytest.mark.parametrize("failure", ["exception", "tenant", "success"])
def test_the_slot_is_returned_after_success_and_after_failures(tmp_path, monkeypatch, failure):
    api = _make_api(tmp_path, max_concurrent_extractions=1)
    document_id = _upload(api)
    if failure == "exception":
        monkeypatch.setattr(DocumentService, "process_stored_page", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    elif failure == "tenant":
        monkeypatch.setattr(DocumentService, "process_stored_page", lambda self, *a, **k: (_ for _ in ()).throw(TenantMismatchError("x")))
    _extract(api, document_id)
    assert _wait_until(lambda: api["app"].state.extraction_gate.active == 0)


def test_gate_keeps_the_slot_after_a_cancelled_request_until_the_worker_ends():
    release = threading.Event()
    started = threading.Event()

    def work():
        started.set()
        release.wait(10)
        return "done"

    async def scenario():
        gate = ExtractionGate(max_concurrent=1, timeout_seconds=30)
        task = asyncio.create_task(gate.run(work, request_id="r", document_id="d"))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()                                          # the client disconnected
        with pytest.raises(asyncio.CancelledError):
            await task
        busy = gate.active
        with pytest.raises(Exception) as refused:
            await gate.run(lambda: "x", request_id="r", document_id="d")
        release.set()
        for _ in range(200):
            if gate.active == 0:
                break
            await asyncio.sleep(0.02)
        return busy, type(refused.value).__name__, gate.active

    busy, refusal, final = _run_loop(scenario)
    assert busy == 1 and refusal == "RateLimitExceededError" and final == 0


def test_gate_late_worker_result_after_a_timeout_is_discarded_safely():
    release = threading.Event()

    async def scenario():
        gate = ExtractionGate(max_concurrent=1, timeout_seconds=0.1)
        with pytest.raises(Exception) as timed_out:
            await gate.run(lambda: release.wait(10) or "late", request_id="r", document_id="d")
        release.set()
        for _ in range(200):
            if gate.active == 0:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)                               # the worker's late delivery runs and is ignored
        return type(timed_out.value).__name__, gate.active

    name, active = _run_loop(scenario)
    assert name == "ExtractionTimeoutError" and active == 0


def test_gate_worker_exceptions_reach_the_request_and_free_the_slot():
    async def scenario():
        gate = ExtractionGate(max_concurrent=1, timeout_seconds=5)
        with pytest.raises(ValueError):
            await gate.run(lambda: (_ for _ in ()).throw(ValueError("boom")), request_id="r", document_id="d")
        assert gate.active == 0
        return await gate.run(lambda: 42, request_id="r", document_id="d")

    assert _run_loop(scenario) == 42


def test_the_event_loop_and_health_stay_responsive_while_extractions_run_and_time_out(tmp_path, monkeypatch):
    api = _make_api(tmp_path, max_concurrent_extractions=1, extract_sync_timeout_seconds=0.6)
    document_id = _upload(api)
    release, started = threading.Event(), threading.Event()
    _block_extraction(monkeypatch, release, started)
    headers = [(b"authorization", b"Bearer rw")]

    async def scenario():
        slow = asyncio.create_task(_call_app(api["app"], "POST", f"{UPLOAD}/{document_id}/extract-sync", headers))
        while not started.is_set():
            await asyncio.sleep(0.01)
        t0 = time.monotonic()
        health = [await asyncio.wait_for(_call_app(api["app"], "GET", "/api/v1/health"), 2) for _ in range(5)]
        health_seconds = time.monotonic() - t0
        refused = await asyncio.wait_for(_call_app(api["app"], "POST", f"{UPLOAD}/{document_id}/extract-sync", headers), 2)
        timed_out = await asyncio.wait_for(slow, 5)
        release.set()
        return [h[0] for h in health], health_seconds, refused[0], timed_out[0]

    statuses, health_seconds, refused, timed_out = _run_loop(scenario)
    assert statuses == [200] * 5 and health_seconds < 1.5
    assert refused == 429 and timed_out == 504
    assert _wait_until(lambda: api["app"].state.extraction_gate.active == 0)


# ============================================================================
# Settings, logging discipline and regression
# ============================================================================

def test_the_extraction_settings_are_bounded_with_the_prd_deadline_as_default():
    defaults = ApiSettings(app_env="development")
    assert (defaults.max_concurrent_extractions, defaults.extract_sync_timeout_seconds) == (2, 30.0)
    for bad in ({"extract_sync_timeout_seconds": 0}, {"extract_sync_timeout_seconds": 30.001}, {"extract_sync_timeout_seconds": float("inf")},
                {"extract_sync_timeout_seconds": float("nan")}, {"extract_sync_timeout_seconds": -1},
                {"max_concurrent_extractions": 0}, {"max_concurrent_extractions": 65}):
        with pytest.raises(Exception):
            ApiSettings(app_env="development", **bad)
    ok = ApiSettings(app_env="development", extract_sync_timeout_seconds=0.001, max_concurrent_extractions=64)
    assert ok.extract_sync_timeout_seconds == 0.001 and ok.max_concurrent_extractions == 64


def test_the_extraction_settings_are_read_from_the_environment(monkeypatch):
    from doc_extractor.api.config import load_api_settings
    monkeypatch.setenv("DOC_API_ENV", "development")
    monkeypatch.setenv("DOC_API_MAX_CONCURRENT_EXTRACTIONS", "3")
    monkeypatch.setenv("DOC_API_EXTRACT_SYNC_TIMEOUT_SECONDS", "12.5")
    settings = load_api_settings()
    assert settings.max_concurrent_extractions == 3 and settings.extract_sync_timeout_seconds == 12.5
    monkeypatch.setenv("DOC_API_EXTRACT_SYNC_TIMEOUT_SECONDS", "31")
    with pytest.raises(Exception):
        load_api_settings()


def test_the_extraction_service_logs_only_constant_messages_with_fixed_reasons_and_identifiers():
    tree = ast.parse(inspect.getsource(extraction_module))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"]
    assert len(calls) == 2
    for call in calls:
        assert isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        names = {a.id if isinstance(a, ast.Name) else None for a in call.args[1:]}
        assert names <= {"REASON_OVERLOAD", "REASON_TIMEOUT", "request_id", "document_id"}
        assert not call.keywords


def test_the_upload_and_read_endpoints_are_unaffected(api):
    document_id = _upload(api)
    detail = api["client"].get(f"{UPLOAD}/{document_id}", headers=RW)
    assert detail.status_code == 200 and detail.json()["document_id"] == document_id
    assert api["client"].get(f"{UPLOAD}/{document_id}/pages/1", headers=RW).status_code == 200
    assert [d["document_id"] for d in api["client"].get(UPLOAD, headers=RW).json()["content"]] == [document_id]
    assert _extract(api, document_id).status_code == 200
    again = api["client"].get(f"{UPLOAD}/{document_id}", headers=RW).json()
    assert again["result"] is None, "nothing is persisted by the extraction"
