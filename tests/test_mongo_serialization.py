"""
MongoDB serialization tests (Team Member 3, Phase 2A-2). Pure unit tests: no server, no driver.

Verified: UTC handling and millisecond truncation, enums, collections, reserved field names, non-finite and oversized numbers,
unsupported types, the round trip into the real record models, and that errors never echo a value.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest
from pydantic import BaseModel

from doc_extractor.api.contracts.records import ApiKeyRecord, ReviewTaskRecord, ReviewTaskStatus
from doc_extractor.infrastructure.mongo.errors import PersistenceDataError
from doc_extractor.infrastructure.mongo.serialization import ensure_utc, from_document, to_bson_datetime, to_document

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
SECRET = "Value-Marker-That-Must-Not-Leak-4410"


class Color(str, Enum):
    RED = "red"


class Plain(Enum):
    ONE = 1


class Sample(BaseModel):
    name: str
    when: datetime
    color: Color = Color.RED
    tags: tuple = ()


# ---- time -------------------------------------------------------------------------------------------------------------

def test_an_aware_datetime_is_converted_to_utc():
    assert ensure_utc(datetime(2026, 3, 1, 12, 0, tzinfo=IST)) == datetime(2026, 3, 1, 6, 30, tzinfo=UTC)
    assert ensure_utc(datetime(2026, 3, 1, 12, 0, tzinfo=IST)).utcoffset() == timedelta(0)


def test_a_naive_datetime_is_read_as_utc():
    assert ensure_utc(datetime(2026, 3, 1, 12, 0)) == datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def test_bson_precision_is_one_millisecond_and_truncates_toward_the_past():
    assert to_bson_datetime(datetime(2026, 3, 1, 12, 0, 0, 123_999, tzinfo=UTC)).microsecond == 123_000
    assert to_bson_datetime(datetime(2026, 3, 1, 12, 0, 0, 999, tzinfo=UTC)).microsecond == 0
    assert to_bson_datetime(datetime(2026, 3, 1, 12, 0, 0, 5_000, tzinfo=UTC)).microsecond == 5_000


def test_the_stored_value_equals_what_is_read_back():
    stored = to_document({"at": datetime(2026, 3, 1, 12, 0, 0, 123_456, tzinfo=IST)})["at"]
    assert stored == datetime(2026, 3, 1, 6, 30, 0, 123_000, tzinfo=UTC) and stored.tzinfo is UTC
    assert to_document({"at": stored})["at"] == stored                      # idempotent: a second round trip changes nothing


def test_datetimes_inside_nested_structures_are_normalized():
    document = to_document({"a": {"b": [datetime(2026, 1, 1, 1, 0, tzinfo=IST)]}})
    assert document["a"]["b"][0] == datetime(2025, 12, 31, 19, 30, tzinfo=UTC)


# ---- values -----------------------------------------------------------------------------------------------------------

def test_enums_are_stored_as_their_values_including_str_enums():
    document = to_document({"s": Color.RED, "p": Plain.ONE})
    assert document == {"s": "red", "p": 1} and type(document["s"]) is str


def test_tuples_and_sets_become_lists_with_sets_sorted():
    assert to_document({"t": (1, 2), "s": {3, 1, 2}, "f": frozenset({"b", "a"})}) == {"t": [1, 2], "s": [1, 2, 3], "f": ["a", "b"]}


def test_a_set_of_unorderable_items_is_refused():
    with pytest.raises(PersistenceDataError) as caught:
        to_document({"s": {1, "a"}})
    assert caught.value.details["reason"] == "set_items_are_not_orderable"


def test_a_pydantic_model_and_a_mapping_serialize_alike():
    model = Sample(name="n", when=datetime(2026, 3, 1, tzinfo=UTC), tags=("x",))
    assert to_document(model) == {"name": "n", "when": datetime(2026, 3, 1, tzinfo=UTC), "color": "red", "tags": ["x"]}
    assert to_document(model.model_dump()) == to_document(model)


def test_none_booleans_bytes_and_numbers_pass_through():
    assert to_document({"n": None, "b": True, "y": b"\x00\x01", "i": 5, "f": 1.5}) == {"n": None, "b": True, "y": b"\x00\x01", "i": 5, "f": 1.5}


@pytest.mark.parametrize("value, reason", [
    (float("nan"), "float_is_not_finite"), (float("inf"), "float_is_not_finite"), (float("-inf"), "float_is_not_finite"),
    (2**63, "integer_is_outside_the_64_bit_range"), (-(2**63) - 1, "integer_is_outside_the_64_bit_range"),
    (Decimal("1.5"), "unsupported_value_type"), (date(2026, 1, 1), "unsupported_value_type"), (timedelta(1), "unsupported_value_type"),
    (object(), "unsupported_value_type"), (lambda: 1, "unsupported_value_type"),
])
def test_unsupported_or_unsafe_values_are_refused_at_the_boundary(value, reason):
    with pytest.raises(PersistenceDataError) as caught:
        to_document({"v": value})
    assert caught.value.details["reason"] == reason


def test_the_64_bit_limits_are_accepted():
    assert to_document({"a": 2**63 - 1, "b": -(2**63)}) == {"a": 2**63 - 1, "b": -(2**63)}


@pytest.mark.parametrize("key", ["$set", "$where", "a.b", "has\x00nul", "", 5, None])
def test_reserved_or_invalid_field_names_are_refused(key):
    with pytest.raises(PersistenceDataError):
        to_document({key: 1})
    with pytest.raises(PersistenceDataError):
        to_document({"nested": {key: 1}})


def test_the_depth_is_bounded():
    value = {}
    cursor = value
    for _ in range(40):
        cursor["x"] = {}
        cursor = cursor["x"]
    with pytest.raises(PersistenceDataError) as caught:
        to_document(value)
    assert caught.value.details["reason"] == "document_is_nested_too_deeply"


@pytest.mark.parametrize("not_a_document", [None, 5, "text", [1], ("a",)])
def test_only_mappings_and_models_are_documents(not_a_document):
    with pytest.raises(PersistenceDataError):
        to_document(not_a_document)


@pytest.mark.parametrize("bad", [float("nan"), object(), 2**70])
def test_an_error_never_echoes_the_offending_value(bad):
    with pytest.raises(PersistenceDataError) as caught:
        to_document({"field": {"inner": bad, "marker": SECRET}})
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value.details) and "field" not in str(caught.value)


def test_the_input_is_never_modified():
    original = {"when": datetime(2026, 3, 1, 12, 0, 0, 999, tzinfo=IST), "tags": {"b", "a"}}
    snapshot = {"when": original["when"], "tags": set(original["tags"])}
    to_document(original)
    assert original == snapshot


# ---- the real record models -------------------------------------------------------------------------------------------

def _task(**overrides):
    values = dict(task_id="t1", document_id="d1", tenant_id="tenant_a", status=ReviewTaskStatus.CLAIMED, assigned_reviewer_id="r1",
                  created_at=datetime(2026, 3, 1, 12, 0, 0, 123_456, tzinfo=IST), metadata={"flags": ["A"], "n": 2})
    values.update(overrides)
    return ReviewTaskRecord(**values)


def test_a_review_task_round_trips_through_a_stored_document_with_millisecond_precision():
    document = to_document(_task())
    assert document["status"] == "claimed" and document["created_at"].tzinfo is UTC
    document["_id"] = "internal-object-id"                                          # what the server adds
    restored = from_document(ReviewTaskRecord, document)
    assert restored.status is ReviewTaskStatus.CLAIMED and restored.tenant_id == "tenant_a"
    assert restored.created_at == datetime(2026, 3, 1, 6, 30, 0, 123_000, tzinfo=UTC)
    assert restored.metadata == {"flags": ["A"], "n": 2}


def test_an_api_key_record_round_trips_and_a_naive_stored_time_is_read_as_utc():
    record = ApiKeyRecord(key_id="key_0123456789abcdef", tenant_id="tenant_a", name="n", key_hash="a" * 64, key_prefix="de_live_",
                          scopes=["documents:read"], roles=["integrator"])
    document = to_document(record)
    document["created_at"] = document["created_at"].replace(tzinfo=None)          # a driver without tz_aware
    restored = from_document(ApiKeyRecord, document)
    assert restored.created_at.tzinfo is not None and restored.created_at.utcoffset() == timedelta(0)
    assert restored.key_hash == "a" * 64 and restored.scopes == ["documents:read"]


def test_a_stored_document_that_does_not_fit_the_model_is_a_typed_error_without_its_content():
    bad = to_document(_task())
    bad["status"] = "no-such-status"
    bad["reviewer_notes"] = SECRET
    with pytest.raises(PersistenceDataError) as caught:
        from_document(ReviewTaskRecord, bad)
    assert caught.value.details["reason"] == "stored_document_does_not_match_the_model"
    assert SECRET not in str(caught.value) and SECRET not in repr(caught.value.details) and "no-such-status" not in str(caught.value)


def test_a_stored_document_naming_an_unsafe_tenant_is_refused_by_the_model():
    bad = to_document(_task())
    bad["tenant_id"] = "../other"
    with pytest.raises(PersistenceDataError):
        from_document(ReviewTaskRecord, bad)


def test_the_internal_id_is_not_part_of_the_model_and_the_input_is_not_modified():
    document = to_document(_task())
    document["_id"] = "x"
    snapshot = dict(document)
    from_document(ReviewTaskRecord, document)
    assert document == snapshot


def test_a_non_mapping_stored_document_is_refused():
    with pytest.raises(PersistenceDataError):
        from_document(ReviewTaskRecord, ["not", "a", "mapping"])  # type: ignore[arg-type]
