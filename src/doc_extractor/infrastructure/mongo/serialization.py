"""
Safe document serialization for MongoDB (Team Member 3, Phase 2A-2).

``to_document`` turns a pydantic model (or a plain mapping) into a BSON-safe dict; ``from_document`` turns a stored dict
back into a validated model. Rules, each tested:

- Timestamps are UTC. A timezone-aware value is converted to UTC; a NAIVE value is read as UTC (the same rule the review-task
  port states for ordering). BSON stores milliseconds, so ``to_document`` truncates to the millisecond: what is stored is exactly
  what is read back, instead of a value that silently changes on its first round trip.
- Enums are stored as their values; tuples and sets as lists (sets sorted when sortable, so output is deterministic).
- Field names must not start with ``$`` or contain ``.`` or NUL (Mongo operator and path syntax).
- Only plain BSON-safe types are accepted. NaN, infinity, integers beyond 64 bits and unknown types are refused, so a bad value
  fails at the boundary with a typed error instead of deep inside the driver.
- Errors are ``PersistenceDataError`` with a fixed reason; they never echo a field value.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Type, TypeVar

from pydantic import BaseModel, ValidationError

from doc_extractor.infrastructure.mongo.errors import PersistenceDataError

M = TypeVar("M", bound=BaseModel)

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1
_MAX_DEPTH = 32


def ensure_utc(value: datetime) -> datetime:
    """UTC, timezone-aware. A naive datetime is read as UTC. Not truncated (see ``to_bson_datetime``)."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def to_bson_datetime(value: datetime) -> datetime:
    """UTC and truncated to the millisecond, the precision BSON keeps."""
    aware = ensure_utc(value)
    return aware.replace(microsecond=(aware.microsecond // 1000) * 1000)


def _bad(reason: str) -> PersistenceDataError:
    return PersistenceDataError(reason)


def _convert(value: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        raise _bad("document_is_nested_too_deeply")
    if isinstance(value, Enum):                      # first: a ``str``-based enum is also a ``str``
        return _convert(value.value, depth + 1)
    if value is None or isinstance(value, (bool, str, bytes)):
        return value
    if isinstance(value, int):
        if not _INT64_MIN <= value <= _INT64_MAX:
            raise _bad("integer_is_outside_the_64_bit_range")
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _bad("float_is_not_finite")
        return value
    if isinstance(value, datetime):
        return to_bson_datetime(value)
    if isinstance(value, date) or isinstance(value, timedelta):
        raise _bad("unsupported_value_type")
    if isinstance(value, BaseModel):
        return _convert(value.model_dump(mode="python"), depth + 1)
    if isinstance(value, Mapping):
        return _convert_mapping(value, depth)
    if isinstance(value, (list, tuple)):
        return [_convert(item, depth + 1) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_convert(item, depth + 1) for item in value]
        try:
            return sorted(items)
        except TypeError:
            raise _bad("set_items_are_not_orderable") from None
    raise _bad("unsupported_value_type")


def _convert_mapping(value: Mapping, depth: int) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise _bad("field_name_is_not_a_non_empty_string")
        if key.startswith("$") or "." in key or "\x00" in key:
            raise _bad("field_name_uses_reserved_characters")
        result[key] = _convert(item, depth + 1)
    return result


def to_value(value: Any) -> Any:
    """
    One BSON-safe VALUE (scalar, list or nested mapping), with the same rules as ``to_document`` but WITHOUT the rules for a
    document's own top-level field name. For values that are not a whole document, e.g. the operand of an update operator.
    Nested mappings are still checked key by key, so a value cannot smuggle in an operator (``$``) or path (``.``) key.
    """
    return _convert(value, 1)


def to_document(value: Any) -> Dict[str, Any]:
    """A BSON-safe dict from a pydantic model or a mapping. Raises ``PersistenceDataError`` (reason only)."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if not isinstance(value, Mapping):
        raise _bad("document_is_not_a_mapping")
    return _convert_mapping(value, 0)


def _normalize_stored(value: Any) -> Any:
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, Mapping):
        return {key: _normalize_stored(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_stored(item) for item in value]
    return value


def from_document(model: Type[M], document: Mapping[str, Any]) -> M:
    """A validated ``model`` from a stored document. The internal ``_id`` is dropped; datetimes become UTC-aware."""
    if not isinstance(document, Mapping):
        raise _bad("stored_document_is_not_a_mapping")
    data = {key: _normalize_stored(item) for key, item in document.items() if key != "_id"}
    try:
        return model.model_validate(data)
    except ValidationError:
        raise _bad("stored_document_does_not_match_the_model") from None
    except ValueError:
        raise _bad("stored_document_does_not_match_the_model") from None
