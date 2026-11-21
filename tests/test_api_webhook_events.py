"""
Tests for the webhook event contracts and wire format (PRD v1.5 section 7.1), Phase 4, PROVISIONAL.

Verified: the four events and their validated payloads; unique event identifiers; byte-exact deterministic serialization
(offset-independent, sorted, compact, ASCII); strict parsing (round trip, duplicate keys, NaN, extra or missing keys, non-canonical
timestamps, oversize); that no payload can carry an extracted value, evidence, free text, a person or the tenant; that
refusals are sanitized; and that identifier shapes are the ones the repository already uses. Synthetic data only.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from doc_extractor.api.contracts import webhook_events as module
from doc_extractor.api.contracts.documents import SAFE_DOCUMENT_ID_REGEX
from doc_extractor.api.contracts.webhook_events import (
    EVENT_CLASSES,
    MAX_PAYLOAD_BYTES,
    DocumentCompletedData,
    DocumentCompletedEvent,
    DocumentFailedData,
    DocumentNeedsReviewData,
    SchemaApprovedData,
    WebhookEventError,
    WebhookEventType,
    document_completed,
    document_failed,
    document_needs_review,
    new_event_id,
    parse_event,
    schema_approved,
    serialize_event,
)
from schema_lifecycle.enums import SchemaStatus
from schema_lifecycle.events import LifecycleEvent, LifecycleEventType

T = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
SECRET = "SYNTH-SECRET-VALUE-731"
FIXED_ID = "evt_00000000000000000000000000000001"


def _all_events(occurred_at=T):
    return {
        "document.completed": document_completed(document_id="doc_1", status="reviewed", occurred_at=occurred_at),
        "document.needs_review": document_needs_review(document_id="doc_1", task_id="task_1", occurred_at=occurred_at),
        "document.failed": document_failed(document_id="doc_1", reason_code="STAGE_TIMEOUT", occurred_at=occurred_at),
        "schema.approved": schema_approved(schema_id="loan.form-v2", schema_version=3, occurred_at=occurred_at),
    }


# ============================================================================
# The four events
# ============================================================================

def test_the_four_prd_events_exist_with_their_exact_names():
    assert [member.value for member in WebhookEventType] == [
        "document.completed", "document.needs_review", "document.failed", "schema.approved"]
    declared = {cls.model_fields["event_type"].default for cls in EVENT_CLASSES}
    assert declared == {member.value for member in WebhookEventType}


@pytest.mark.parametrize("event_type", [member.value for member in WebhookEventType])
def test_each_event_has_an_id_a_type_a_timestamp_and_only_its_contract_data(event_type):
    event = _all_events()[event_type]
    assert event.event_type == event_type and event.occurred_at == T and module._matches(module._EVENT_ID, event.event_id)
    wire = json.loads(serialize_event(event))
    assert set(wire) == {"event_id", "event_type", "occurred_at", "data"}
    expected = {
        "document.completed": {"document_id", "status"}, "document.needs_review": {"document_id", "task_id"},
        "document.failed": {"document_id", "reason_code"}, "schema.approved": {"schema_id", "schema_version"},
    }[event_type]
    assert set(wire["data"]) == expected


def test_the_completed_event_covers_exactly_the_two_statuses_of_the_prd_trigger():
    for status in ("auto_approved", "reviewed"):
        assert document_completed(document_id="d", status=status, occurred_at=T).data.status == status
    for status in ("needs_review", "rejected", "failed", "processing", "Reviewed", "", None, 1):
        with pytest.raises(WebhookEventError):
            document_completed(document_id="d", status=status, occurred_at=T)


def test_every_event_is_frozen_and_rejects_extra_fields():
    event = _all_events()["document.completed"]
    with pytest.raises(ValidationError):
        event.event_id = "evt_other_0001"
    with pytest.raises(ValidationError):
        event.data.status = "auto_approved"
    with pytest.raises(ValidationError):
        DocumentCompletedData(document_id="d", status="reviewed", value=SECRET)
    with pytest.raises(ValidationError):
        DocumentCompletedEvent(event_id=FIXED_ID, occurred_at=T, data={"document_id": "d", "status": "reviewed"}, tenant_id="t")
    with pytest.raises(ValidationError):                                                    # data of another event type
        DocumentCompletedEvent(event_id=FIXED_ID, occurred_at=T, data=DocumentFailedData(document_id="d", reason_code="ERROR_X"))
    with pytest.raises(ValidationError):                                                    # a type that does not match the class
        DocumentCompletedEvent(event_id=FIXED_ID, occurred_at=T, event_type="document.failed",
                               data={"document_id": "d", "status": "reviewed"})


# ============================================================================
# Payload validation
# ============================================================================

_BAD_DOCUMENT_IDS = ["", " ", "doc 1", "doc/1", "doc\n", "doc_1\n", "../x", "d" * 129, "dóc", "doc١", None, 7, b"d", ["d"], f"doc {SECRET}"]
_BAD_REASON_CODES = ["", "x", "stage_timeout", "STAGE TIMEOUT", "S", "A" * 65, "1ABC", f"FAILED {SECRET}", None, 7, "ERROR\n", "ÉRROR"]
_BAD_SCHEMA_IDS = ["", ".", "..", ".x", "-x", "a b", "a" * 65, "a/b", "a\n", "é", None, 7, f"schema {SECRET}"]
_BAD_VERSIONS = [0, -1, True, False, "3", 3.0, 3.5, None, 10**10, 1_000_000_000, [3], {"v": 3}]
_BAD_TIMES = [None, "2026-03-01T12:00:00Z", 1_700_000_000, 1.5, datetime(2026, 3, 1, 12, 0), T.date(), object()]
_BAD_EVENT_IDS = ["", "evt", "evt_1", "x" * 129, "evt 0000001", "evt_0000001\n", "_evt00000001", "é" * 10, 7, b"evt_00000001"]


@pytest.mark.parametrize("bad", _BAD_DOCUMENT_IDS)
def test_a_malformed_document_id_is_refused_in_every_event(bad):
    for build in (
        lambda: document_completed(document_id=bad, status="reviewed", occurred_at=T),
        lambda: document_needs_review(document_id=bad, task_id="task_1", occurred_at=T),
        lambda: document_failed(document_id=bad, reason_code="ERROR_X", occurred_at=T),
    ):
        with pytest.raises(WebhookEventError) as caught:
            build()
        assert caught.value.code == WebhookEventError.INVALID_EVENT


@pytest.mark.parametrize("bad", ["", "t k", "t\n", "t" * 129, None, 7, f"task {SECRET}"])
def test_a_malformed_task_id_is_refused(bad):
    with pytest.raises(WebhookEventError):
        document_needs_review(document_id="d", task_id=bad, occurred_at=T)


@pytest.mark.parametrize("bad", _BAD_REASON_CODES)
def test_a_reason_code_must_be_a_fixed_upper_case_token(bad):
    with pytest.raises(WebhookEventError):
        document_failed(document_id="d", reason_code=bad, occurred_at=T)


@pytest.mark.parametrize("bad", _BAD_SCHEMA_IDS)
def test_a_malformed_schema_id_is_refused(bad):
    with pytest.raises(WebhookEventError):
        schema_approved(schema_id=bad, schema_version=1, occurred_at=T)


@pytest.mark.parametrize("bad", _BAD_VERSIONS)
def test_a_schema_version_must_be_a_positive_integer(bad):
    with pytest.raises(WebhookEventError):
        schema_approved(schema_id="s", schema_version=bad, occurred_at=T)


@pytest.mark.parametrize("bad", _BAD_TIMES)
def test_the_occurrence_time_must_be_a_timezone_aware_datetime(bad):
    with pytest.raises(WebhookEventError):
        document_completed(document_id="d", status="reviewed", occurred_at=bad)


@pytest.mark.parametrize("bad", _BAD_EVENT_IDS)
def test_a_malformed_explicit_event_id_is_refused(bad):
    with pytest.raises(WebhookEventError):
        document_completed(document_id="d", status="reviewed", occurred_at=T, event_id=bad)


def test_the_occurrence_time_is_required_there_is_no_clock_default():
    with pytest.raises(TypeError):
        document_completed(document_id="d", status="reviewed")                         # type: ignore[call-arg]
    for factory in (document_completed, document_needs_review, document_failed, schema_approved):
        import inspect

        assert inspect.signature(factory).parameters["occurred_at"].default is inspect.Parameter.empty


def test_a_refusal_is_sanitized_fixed_and_never_echoes_the_input():
    hostile = f"doc {SECRET}"
    with pytest.raises(WebhookEventError) as caught:
        document_completed(document_id=hostile, status="reviewed", occurred_at=T)
    error = caught.value
    assert error.code == "INVALID_EVENT" and str(error) == "The webhook event is not valid."
    assert SECRET not in str(error) and SECRET not in repr(error) and error.__cause__ is None and error.__suppress_context__
    assert WebhookEventError("anything").code == "INVALID_EVENT"
    for code, message in WebhookEventError._MESSAGES.items():
        assert WebhookEventError(code).code == code and str(WebhookEventError(code)) == message


def test_identifier_shapes_are_the_ones_the_repository_already_uses():
    from doc_extractor.api.services import correction_validation, review_routing

    assert DocumentCompletedData(document_id="doc_1-A", status="reviewed")
    assert not SAFE_DOCUMENT_ID_REGEX.fullmatch("doc 1")
    assert module._SCHEMA_ID.pattern == correction_validation._SCHEMA_ID.pattern
    assert module._REASON_CODE.pattern == correction_validation._CODE_TOKEN.pattern == review_routing._CODE_TOKEN.pattern


# ============================================================================
# Unique ids, deterministic serialization
# ============================================================================

def test_event_ids_are_unique_and_well_formed():
    ids = [new_event_id() for _ in range(5000)]
    assert len(set(ids)) == 5000
    assert all(i.startswith("evt_") and len(i) == 36 and module._matches(module._EVENT_ID, i) for i in ids)
    built = [document_completed(document_id="d", status="reviewed", occurred_at=T).event_id for _ in range(500)]
    assert len(set(built)) == 500
    assert document_completed(document_id="d", status="reviewed", occurred_at=T, event_id=FIXED_ID).event_id == FIXED_ID


def test_the_serialization_is_the_exact_canonical_bytes():
    event = document_completed(document_id="doc_1", status="reviewed", occurred_at=T, event_id=FIXED_ID)
    assert serialize_event(event) == (
        b'{"data":{"document_id":"doc_1","status":"reviewed"},"event_id":"evt_00000000000000000000000000000001",'
        b'"event_type":"document.completed","occurred_at":"2026-03-01T12:00:00.000000Z"}'
    )
    schema = schema_approved(schema_id="loan.form-v2", schema_version=3, occurred_at=T, event_id=FIXED_ID)
    assert serialize_event(schema) == (
        b'{"data":{"schema_id":"loan.form-v2","schema_version":3},"event_id":"evt_00000000000000000000000000000001",'
        b'"event_type":"schema.approved","occurred_at":"2026-03-01T12:00:00.000000Z"}'
    )


def test_the_serialization_is_deterministic_ascii_sorted_and_compact():
    for event_type, event in _all_events().items():
        first = serialize_event(event)
        assert first == serialize_event(event) == serialize_event(event.model_copy(deep=True))
        assert first.isascii() and b" " not in first and b"\n" not in first
        decoded = json.loads(first)
        assert list(decoded) == sorted(decoded) and list(decoded["data"]) == sorted(decoded["data"])
        assert first == json.dumps(decoded, sort_keys=True, separators=(",", ":")).encode()


def test_the_same_instant_serializes_identically_whatever_its_offset():
    base = serialize_event(document_completed(document_id="d", status="reviewed", occurred_at=T, event_id=FIXED_ID))
    for offset in (timedelta(hours=5, minutes=30), timedelta(hours=-8), timedelta(0)):
        local = T.astimezone(timezone(offset))
        assert serialize_event(document_completed(document_id="d", status="reviewed", occurred_at=local, event_id=FIXED_ID)) == base


def test_microseconds_are_always_six_digits_and_the_time_is_always_utc_z():
    for moment in (T, T.replace(microsecond=1), T.replace(microsecond=999999), datetime(1, 1, 1, tzinfo=timezone.utc)):
        wire = json.loads(serialize_event(document_completed(document_id="d", status="reviewed", occurred_at=moment, event_id=FIXED_ID)))
        stamp = wire["occurred_at"]
        assert len(stamp) == 27 and stamp.endswith("Z") and stamp[19] == "."
        assert parse_event(serialize_event(document_completed(document_id="d", status="reviewed", occurred_at=moment, event_id=FIXED_ID))).occurred_at == moment


def test_an_event_built_around_the_validators_still_serializes_in_utc():
    """``model_construct`` skips the model validators (which normalize to UTC): the wire time must still be UTC with a ``Z``."""
    from doc_extractor.api.contracts.webhook_events import DocumentCompletedEvent

    india = timezone(timedelta(hours=5, minutes=30))
    event = DocumentCompletedEvent.model_construct(
        event_id=FIXED_ID, event_type="document.completed", occurred_at=T.astimezone(india),
        data=DocumentCompletedData(document_id="d", status="reviewed"))
    assert json.loads(serialize_event(event))["occurred_at"] == "2026-03-01T12:00:00.000000Z"


def test_only_a_validated_event_can_be_serialized():
    for bad in (None, {}, "event", 7, DocumentCompletedData(document_id="d", status="reviewed")):
        with pytest.raises(WebhookEventError) as caught:
            serialize_event(bad)
        assert caught.value.code == "INVALID_EVENT"


# ============================================================================
# Parsing (round trip and strictness)
# ============================================================================

@pytest.mark.parametrize("event_type", [member.value for member in WebhookEventType])
def test_every_event_round_trips_byte_for_byte(event_type):
    event = _all_events()[event_type]
    wire = serialize_event(event)
    parsed = parse_event(wire)
    assert parsed == event and type(parsed) is type(event) and serialize_event(parsed) == wire
    assert parse_event(wire.decode("ascii")) == event                                      # str input too


def _wire(**overrides):
    base = json.loads(serialize_event(document_completed(document_id="doc_1", status="reviewed", occurred_at=T, event_id=FIXED_ID)))
    base.update(overrides)
    return json.dumps(base).encode()


@pytest.mark.parametrize("payload", [
    b"", b"null", b"[]", b'"x"', b"7", b"{", b"not json", b"\xff\xfe", b'{"event_id":"evt_00000000000000000000000000000001"}',
    _wire(extra="x"), _wire(event_type="document.unknown"), _wire(event_type="document.failed"), _wire(event_id="bad"),
    _wire(occurred_at="2026-03-01T12:00:00Z"), _wire(occurred_at="2026-03-01T12:00:00.000000+00:00"),
    _wire(occurred_at="2026-03-01T12:00:00.000Z"), _wire(occurred_at=1_700_000_000), _wire(occurred_at=None),
    _wire(data={"document_id": "doc_1", "status": "reviewed", "value": "x"}), _wire(data={"document_id": "doc_1"}),
    _wire(data={"document_id": "doc 1", "status": "reviewed"}), _wire(data={"document_id": "doc_1", "status": "rejected"}),
    _wire(data=[]), _wire(data=None), _wire(data="x"),
    b'{"event_id":"evt_00000000000000000000000000000001","event_id":"evt_00000000000000000000000000000002"}',
    b'{"data":{"document_id":"a","document_id":"b","status":"reviewed"},"event_id":"evt_00000000000000000000000000000001",'
    b'"event_type":"document.completed","occurred_at":"2026-03-01T12:00:00.000000Z"}',
    b'{"data":{"schema_id":"s","schema_version":NaN},"event_id":"evt_00000000000000000000000000000001",'
    b'"event_type":"schema.approved","occurred_at":"2026-03-01T12:00:00.000000Z"}',
    b'{"data":{"schema_id":"s","schema_version":"3"},"event_id":"evt_00000000000000000000000000000001",'
    b'"event_type":"schema.approved","occurred_at":"2026-03-01T12:00:00.000000Z"}',
    b'{"data":{"schema_id":"s","schema_version":3.0},"event_id":"evt_00000000000000000000000000000001",'
    b'"event_type":"schema.approved","occurred_at":"2026-03-01T12:00:00.000000Z"}',
    b"x" * (MAX_PAYLOAD_BYTES + 1), b"[" * 8000 + b"]" * 8000,
])
def test_a_payload_that_is_not_exactly_a_valid_event_is_refused(payload):
    with pytest.raises(WebhookEventError) as caught:
        parse_event(payload)
    assert caught.value.code == "INVALID_PAYLOAD" and caught.value.__cause__ is None


def test_a_valid_event_padded_beyond_the_size_bound_is_refused():
    wire = serialize_event(_all_events()["document.completed"])
    assert parse_event(wire + b" " * 100) is not None                                          # padding within the bound is harmless JSON
    with pytest.raises(WebhookEventError) as caught:
        parse_event(wire + b" " * MAX_PAYLOAD_BYTES)
    assert caught.value.code == "INVALID_PAYLOAD"
    assert len(wire) < 512                                                                        # a real event is far below the bound


@pytest.mark.parametrize("payload", [None, 7, ["x"], {"a": 1}, bytearray(b"{}"), memoryview(b"{}"), "\ud800"])
def test_a_payload_of_the_wrong_type_is_refused(payload):
    with pytest.raises(WebhookEventError) as caught:
        parse_event(payload)
    assert caught.value.code == "INVALID_PAYLOAD"


def test_a_parse_failure_never_echoes_the_payload():
    hostile = _wire(extra=SECRET)
    with pytest.raises(WebhookEventError) as caught:
        parse_event(hostile)
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value)


# ============================================================================
# Privacy
# ============================================================================

_FORBIDDEN_FIELD_NAMES = {
    "value", "values", "extracted_values", "evidence", "evidence_text", "text", "content", "document_content", "filename", "file_name",
    "prompt", "message", "messages", "reason", "error", "error_message", "diagnostic", "diagnostics", "run", "model", "tenant", "tenant_id",
    "actor", "approver", "author", "principal_id", "reviewer_id", "url", "target_url", "secret", "notes", "metadata", "details", "fields",
}


def test_no_event_model_has_a_field_that_could_carry_content_or_a_person():
    models = list(EVENT_CLASSES) + [DocumentCompletedData, DocumentNeedsReviewData, DocumentFailedData, SchemaApprovedData]
    names = {name for model in models for name in model.model_fields}
    assert not names & _FORBIDDEN_FIELD_NAMES
    assert names == {"event_id", "event_type", "occurred_at", "data", "document_id", "status", "task_id", "reason_code", "schema_id", "schema_version"}


def test_no_free_text_can_enter_any_payload():
    hostile = f"free text with spaces {SECRET}"
    for build in (
        lambda: document_completed(document_id=hostile, status="reviewed", occurred_at=T),
        lambda: document_needs_review(document_id="d", task_id=hostile, occurred_at=T),
        lambda: document_failed(document_id="d", reason_code=hostile, occurred_at=T),
        lambda: schema_approved(schema_id=hostile, schema_version=1, occurred_at=T),
        lambda: document_completed(document_id="d", status="reviewed", occurred_at=T, event_id=hostile),
    ):
        with pytest.raises(WebhookEventError):
            build()


def test_a_schema_approved_event_from_a_real_lifecycle_event_carries_no_person_or_reason():
    lifecycle = LifecycleEvent(
        tenant_id="tenant_alpha", schema_id="loan.form-v2", version=3, event_type=LifecycleEventType.APPROVED,
        actor=f"approver {SECRET}", timestamp=T, from_status=SchemaStatus.TESTING, to_status=SchemaStatus.APPROVED,
        details={"author": f"author {SECRET}", "approver": f"approver {SECRET}", "reason": f"reason {SECRET}", "harness_run_id": "run_1"},
    )
    event = schema_approved(schema_id=lifecycle.schema_id, schema_version=lifecycle.version, occurred_at=lifecycle.timestamp)
    wire = serialize_event(event).decode()
    for forbidden in (SECRET, "tenant_alpha", "approver", "author", "reason", "harness", "run_1", "actor", "TESTING", "status"):
        assert forbidden not in wire, forbidden
    assert json.loads(wire)["data"] == {"schema_id": "loan.form-v2", "schema_version": 3}


def test_a_serialized_event_holds_only_identifiers_codes_and_times():
    for event in _all_events().values():
        wire = json.loads(serialize_event(event))
        for key, value in wire["data"].items():
            assert key in {"document_id", "status", "task_id", "reason_code", "schema_id", "schema_version"}
            assert isinstance(value, (str, int)) and (not isinstance(value, str) or len(value) <= 128)


# ============================================================================
# Hardening F2: a timestamp that UTC cannot represent is a controlled error, never a raw OverflowError
# ============================================================================

UTC = timezone.utc
_PLUS_14, _MINUS_14 = timezone(timedelta(hours=14)), timezone(timedelta(hours=-14))
EXTREME_OFFSET_TIMES = [
    datetime(1, 1, 1, 0, 0, tzinfo=_PLUS_14),                                       # before year 1 in UTC
    datetime(1, 1, 1, 13, 59, 59, 999999, tzinfo=_PLUS_14),
    datetime(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=23, minutes=59))),
    datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=_MINUS_14),                   # past year 9999 in UTC
    datetime(9999, 12, 31, 10, 0, tzinfo=_MINUS_14),
    datetime(9999, 12, 31, 0, 1, tzinfo=timezone(timedelta(hours=-23, minutes=-59))),
]
_FACTORIES = {
    "document.completed": lambda moment: document_completed(document_id="d", status="reviewed", occurred_at=moment),
    "document.needs_review": lambda moment: document_needs_review(document_id="d", task_id="t", occurred_at=moment),
    "document.failed": lambda moment: document_failed(document_id="d", reason_code="ERROR_X", occurred_at=moment),
    "schema.approved": lambda moment: schema_approved(schema_id="s", schema_version=1, occurred_at=moment),
}


@pytest.mark.parametrize("moment", EXTREME_OFFSET_TIMES)
@pytest.mark.parametrize("event_type", sorted(_FACTORIES))
def test_an_unrepresentable_occurrence_time_is_the_controlled_event_error_in_every_factory(event_type, moment):
    with pytest.raises(WebhookEventError) as caught:
        _FACTORIES[event_type](moment)
    error = caught.value
    assert error.code == "INVALID_EVENT" and not isinstance(error, OverflowError) and error.__cause__ is None
    assert str(error) == "The webhook event is not valid."


@pytest.mark.parametrize("moment", EXTREME_OFFSET_TIMES)
def test_an_unrepresentable_time_is_a_validation_error_when_a_model_is_built_directly(moment):
    with pytest.raises(ValidationError) as caught:
        DocumentCompletedEvent(event_id=FIXED_ID, occurred_at=moment, data=DocumentCompletedData(document_id="d", status="reviewed"))
    assert not isinstance(caught.value, OverflowError) and "UTC cannot represent" in str(caught.value)


@pytest.mark.parametrize("moment", EXTREME_OFFSET_TIMES)
def test_serializing_an_unvalidated_event_with_an_unrepresentable_time_is_the_controlled_error(moment):
    """``model_construct`` skips the validators; the conversion at serialization must not leak a raw overflow either."""
    event = DocumentCompletedEvent.model_construct(
        event_id=FIXED_ID, event_type="document.completed", occurred_at=moment,
        data=DocumentCompletedData(document_id="d", status="reviewed"))
    with pytest.raises(WebhookEventError) as caught:
        serialize_event(event)
    assert caught.value.code == "INVALID_EVENT" and not isinstance(caught.value, OverflowError)


def test_the_representable_extremes_are_accepted_and_serialize_exactly():
    lowest = datetime(1, 1, 1, 14, 0, tzinfo=_PLUS_14)                              # exactly 0001-01-01T00:00:00Z
    highest = datetime(9999, 12, 31, 9, 59, 59, 999999, tzinfo=_MINUS_14)           # exactly 9999-12-31T23:59:59.999999Z
    for moment, text in ((lowest, "0001-01-01T00:00:00.000000Z"), (highest, "9999-12-31T23:59:59.999999Z")):
        event = document_completed(document_id="d", status="reviewed", occurred_at=moment, event_id=FIXED_ID)
        wire = serialize_event(event)
        assert json.loads(wire)["occurred_at"] == text and parse_event(wire) == event and serialize_event(parse_event(wire)) == wire


def test_the_canonical_wire_time_at_the_extremes_parses_and_nothing_beyond_exists():
    for text in ("0001-01-01T00:00:00.000000Z", "9999-12-31T23:59:59.999999Z"):
        payload = serialize_event(document_completed(document_id="d", status="reviewed", occurred_at=T, event_id=FIXED_ID)).replace(
            b"2026-03-01T12:00:00.000000Z", text.encode())
        assert parse_event(payload).occurred_at.isoformat(timespec="microseconds").replace("+00:00", "Z") == text
    for text in ("0000-01-01T00:00:00.000000Z", "10000-01-01T00:00:00.000000Z", "9999-12-31T24:00:00.000000Z", "2026-13-01T00:00:00.000000Z"):
        payload = serialize_event(document_completed(document_id="d", status="reviewed", occurred_at=T, event_id=FIXED_ID)).replace(
            b"2026-03-01T12:00:00.000000Z", text.encode())
        with pytest.raises(WebhookEventError):
            parse_event(payload)


# ============================================================================
# Hardening F3: the event id carries 122 random bits (a uuid4 fixes six of its 128), and says so
# ============================================================================

def test_the_event_id_documentation_states_122_random_bits_not_128():
    import inspect

    assert "122 random bits" in inspect.getdoc(new_event_id) and "128 random bits" not in inspect.getdoc(new_event_id)
    assert "128 random bits" not in inspect.getsource(module)
    register = (__import__("pathlib").Path(__file__).resolve().parents[1] / "docs" / "m5-open-decisions.md").read_text(encoding="utf-8")
    flat = " ".join(register.split())
    assert "122 random bits" in flat and "128 random bits" not in flat


def test_the_event_id_generation_is_unchanged_a_uuid4_with_its_fixed_version_and_variant_bits():
    ids = [new_event_id() for _ in range(2000)]
    assert {i[4:][12] for i in ids} == {"4"}                                        # the version nibble: 4 of the 128 bits are fixed
    assert {i[4:][16] for i in ids} <= set("89ab") and len({i[4:][16] for i in ids}) == 4          # the variant: 2 more
    assert all(len(i) == 36 and i.startswith("evt_") for i in ids) and len(set(ids)) == 2000


def test_every_random_position_of_an_event_id_varies_so_no_digit_is_fixed_by_mistake():
    """Only the version and variant digits are constant; every other hex digit of 2,000 ids takes many values (about 16 each)."""
    digits = [new_event_id()[4:] for _ in range(2000)]
    assert all(len(d) == 32 for d in digits)
    for position in range(32):
        seen = {d[position] for d in digits}
        if position == 12:
            assert seen == {"4"}                                                    # the version
        elif position == 16:
            assert seen == set("89ab")                                              # the variant
        else:
            assert len(seen) >= 14, (position, sorted(seen))
