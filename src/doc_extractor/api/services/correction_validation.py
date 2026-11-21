"""
Pure correction validation for reviewer corrections (FR-REV-003), Phase 3 / Slice 2A. PROVISIONAL.

``CorrectionValidator.validate`` re-validates PROPOSED corrections of extracted fields against the EXACT schema version the
document was extracted with. It reuses the existing validation code and adds no business rule of its own:

- static schemas DOC-01, DOC-02 and DOC-03 (version 1.0.0): the very functions Module C applies per field (``validate_pan``,
  ``normalize_date_to_iso``, ``normalize_phone_number``, ``validate_aadhaar``, ``validate_ifsc``, ``normalize_currency_amount``),
  the same required-field set, ``validate_chronology`` (DOC-03) and ``validate_arithmetic`` (DOC-02 nominee shares);
  ``tests/test_api_correction_validation.py`` pins this table against ``ModuleCPipeline`` itself;
- dynamic schemas (``schema_lifecycle`` snapshots): ``DynamicSchemaCompiler`` metadata (which also decides what is a repeating
  group: the compiler's shape is the extraction's shape), the dynamic adapter's own ``_validate_and_normalize_field`` and
  ``evaluate_dynamic_rules``. The rules receive the NORMALIZED number of a corrected integer or decimal field (so formatted text
  such as "₹500" cannot slip past a comparison rule), and the raw value for every other field.

EXACT VERSION, NEVER A GUESS. The caller supplies the schema ID and version recorded on the extraction. A schema that cannot
be resolved EXACTLY gives a controlled ``REJECTED`` result: this module never consults the active or latest version, never
rounds a version ("1.2.0" is not "1") and never picks a schema when the ID or version is missing. The snapshot's lifecycle
status is NOT checked (whether a correction may be validated against a retired or non-approved version is a policy question:
M5-D11 stays Open).

RESULTS CARRY CODES ONLY. A result holds fixed machine-readable codes and schema-declared field paths. It never holds a
value (old, new or normalized), evidence text, a client-supplied path or any exception text; a path that is not declared by
the schema is reported without being echoed. The module logs one constant message on a resolver fault (exception class only).
The source ``fields`` mapping is never mutated: the rules run on deep copies.

Scope limits: this validates corrections IN MEMORY. It stores nothing, writes no audit, updates no status and completes no task.
Only the corrected fields and the cross-field rules that concern them are judged; a pre-existing violation elsewhere in the
document is outside the result.
"""

import copy
import logging
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Protocol, Sequence, Tuple

from doc_extractor.api.contracts.extraction import schema_field_paths
from doc_extractor.contracts_c import ExtractedField, FlagItem
from doc_extractor.field_assembly import REQUIRED_FIELD_MISSING
from doc_extractor.validation_rules import (
    normalize_currency_amount,
    normalize_date_to_iso,
    normalize_phone_number,
    validate_aadhaar,
    validate_arithmetic,
    validate_chronology,
    validate_ifsc,
    validate_pan,
)

logger = logging.getLogger("doc_extractor.api.correction_validation")

MAX_CORRECTIONS = 200
MAX_VALUE_LENGTH = 2048
MAX_PATH_LENGTH = 128

_PATH = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")           # ASCII; fullmatch only (no trailing-newline quirk)
_INDEX_SEGMENT = re.compile(r"[0-9]{1,3}")
_SCHEMA_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}")        # starts alphanumeric: ".." and ".x" are not identifiers
_VERSION_INT = re.compile(r"[1-9][0-9]{0,8}")
_VERSION_SEMVER = re.compile(r"([1-9][0-9]{0,8})\.0\.0")             # the repository's own form of integer version N: "N.0.0"
_CODE_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_REPORTABLE_PATH = re.compile(r"[A-Za-z0-9_.\[\]]{1,128}")

STATIC_SCHEMA_VERSION = 1                                            # DOC-01..03 rules are those of version "1.0.0"


class CorrectionCode:
    """The fixed codes of this module. Format and rule codes come from the reused validators (token-checked)."""

    TENANT_REQUIRED = "TENANT_REQUIRED"
    SCHEMA_ID_REQUIRED = "SCHEMA_ID_REQUIRED"
    SCHEMA_ID_INVALID = "SCHEMA_ID_INVALID"
    FIELD_CONTEXT_INVALID = "FIELD_CONTEXT_INVALID"
    SCHEMA_VERSION_REQUIRED = "SCHEMA_VERSION_REQUIRED"
    SCHEMA_VERSION_INVALID = "SCHEMA_VERSION_INVALID"
    SCHEMA_VERSION_NOT_FOUND = "SCHEMA_VERSION_NOT_FOUND"
    SCHEMA_SOURCE_UNAVAILABLE = "SCHEMA_SOURCE_UNAVAILABLE"
    SCHEMA_RESOLUTION_FAILED = "SCHEMA_RESOLUTION_FAILED"
    TOO_MANY_CORRECTIONS = "TOO_MANY_CORRECTIONS"
    INVALID_CORRECTION = "INVALID_CORRECTION"
    INVALID_FIELD_PATH = "INVALID_FIELD_PATH"
    UNKNOWN_FIELD_PATH = "UNKNOWN_FIELD_PATH"
    FIELD_NOT_CORRECTABLE = "FIELD_NOT_CORRECTABLE"
    DUPLICATE_CORRECTION = "DUPLICATE_CORRECTION"
    INVALID_VALUE_TYPE = "INVALID_VALUE_TYPE"
    VALUE_TOO_LONG = "VALUE_TOO_LONG"
    VALUE_NOT_ALLOWED = "VALUE_NOT_ALLOWED"
    REQUIRED_FIELD_MISSING = REQUIRED_FIELD_MISSING                  # the code Module C uses
    INVALID_VALUE = "INVALID_VALUE"                                  # fallback for a validator code that is not a token
    RULE_VIOLATION = "RULE_VIOLATION"                                # fallback for a rule code that is not a token


class CorrectionOutcome(str, Enum):
    VALID = "valid"            # every correction passed; nothing else was judged
    INVALID = "invalid"        # at least one correction or cross-field rule failed
    REJECTED = "rejected"      # controlled failure: no exact schema could be used, so nothing was judged


@dataclass(frozen=True)
class ProposedCorrection:
    """One proposed correction. ``value=None`` (or a blank string) marks the field as null. Scalars only."""

    path: str
    value: Any = None


@dataclass(frozen=True)
class CorrectionIssue:
    """``code`` is fixed. ``paths`` are schema-declared paths of the corrected fields concerned (never client text)."""

    code: str
    paths: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CorrectionValidationResult:
    outcome: CorrectionOutcome
    issues: Tuple[CorrectionIssue, ...] = ()
    schema_id: Optional[str] = None            # echoed only once the exact schema was resolved; None on a rejection
    schema_version: Optional[int] = None       # the canonical integer the validation used; None when it was not resolved
    validated_paths: Tuple[str, ...] = field(default=())   # the corrected paths that were checked, in request order

    @property
    def is_valid(self) -> bool:
        return self.outcome is CorrectionOutcome.VALID

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)


class SchemaSnapshotSource(Protocol):
    """The part of ``schema_lifecycle.repository.SchemaRepository`` used here: an EXACT, tenant-scoped lookup only."""

    def get_snapshot(self, schema_id: str, version: int, tenant_id: str) -> Any:
        ...


# ---- helpers ---------------------------------------------------------------------------------------------------------------

def parse_exact_version(version: Any) -> Optional[int]:
    """
    A canonical integer version, EXACTLY: a positive int (not bool), "N" or "N.0.0". Anything else is None, including
    "1.2.0", "v1", "1.0.0-beta" and "1.0": ``SchemaVersionAdapter.to_canonical`` rounds those down to a major version, which
    would silently select a different schema, so it is deliberately not used.
    """
    if isinstance(version, bool):
        return None
    if isinstance(version, int):
        return version if 1 <= version <= 999_999_999 else None
    if isinstance(version, str):
        text = version.strip()
        if _VERSION_INT.fullmatch(text):
            return int(text)
        match = _VERSION_SEMVER.fullmatch(text)
        if match:
            return int(match.group(1))
    return None


def _token(code: Any, fallback: str) -> str:
    return code if isinstance(code, str) and _CODE_TOKEN.fullmatch(code) else fallback


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _scalar_problem(value: Any) -> Optional[str]:
    """A fixed code when ``value`` is not an acceptable scalar, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else CorrectionCode.INVALID_VALUE_TYPE
    if isinstance(value, str):
        return CorrectionCode.VALUE_TOO_LONG if len(value) > MAX_VALUE_LENGTH else None
    return CorrectionCode.INVALID_VALUE_TYPE


def _canonical_index_form(path: str) -> str:
    """``nominees.2.share_percentage`` -> ``nominees.share_percentage`` (positions removed; ASCII digits of 1 to 3 characters)."""
    return ".".join(segment for segment in path.split(".") if not _INDEX_SEGMENT.fullmatch(segment))


def _positions_ok(path: str, arrays: FrozenSet[str]) -> bool:
    """
    True when ``path`` carries a position (``0``..``999``) exactly after each repeating-group field and nowhere else:
    ``nominees.2.name`` yes; ``nominees.name`` (no position), ``nominees.name.0`` (position on a leaf) and ``nominees.0.1.name`` no.
    ``arrays`` holds the repeating-group fields as dotted names without positions (``nominees``, ``claims.items``).
    """
    prefix = ""
    after_array_name = False                       # the previous segment named a repeating group, so a position must follow
    for segment in path.split("."):
        if _INDEX_SEGMENT.fullmatch(segment):
            if not after_array_name:
                return False
            after_array_name = False
            continue
        if after_array_name:
            return False
        prefix = f"{prefix}.{segment}" if prefix else segment
        after_array_name = prefix in arrays
    return True                                    # a path ending on the group itself is a container, refused elsewhere


def _match_form(path: str) -> str:
    """A form for comparing a reported rule path (``items[].amount``) with a corrected path (``items.0.amount``)."""
    return _canonical_index_form(path.replace("[]", ""))


# ---- schema models (internal) ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class _FieldSpec:
    required: bool
    container: bool
    text_only: bool                                                        # the validator takes a string: other types are refused
    normalize: Optional[Callable[[Any], Tuple[Any, Optional[str]]]]
    allowed_values: Optional[Tuple[Any, ...]] = None
    value_kind: Optional[str] = None                                       # "integer", "decimal" or "boolean"


class _Schema:
    schema_id: str
    version: int

    def field(self, path: str) -> Optional[_FieldSpec]:  # pragma: no cover - interface
        raise NotImplementedError

    def cross_field(
        self, values: Dict[str, Any], envelope: Dict[str, ExtractedField], corrected: Sequence[str]
    ) -> List[CorrectionIssue]:  # pragma: no cover - interface
        raise NotImplementedError


def _currency(value: Any) -> Tuple[Any, Optional[str]]:
    amount, _currency_code, error = normalize_currency_amount(value)
    return amount, error


# DOC-01..03: field path (positions removed) -> the validator Module C applies to that field (``pipeline_c``).
_STATIC_VALIDATORS: Dict[str, Dict[str, Tuple[Callable[[Any], Tuple[Any, Optional[str]]], bool]]] = {
    "DOC-01": {
        "applicant.pan_card_number": (validate_pan, True),
        "applicant.date_of_birth": (normalize_date_to_iso, True),
        "applicant.phone_number": (normalize_phone_number, True),
        "applicant.aadhaar_number": (validate_aadhaar, True),
        "account_details.initial_deposit_amount": (_currency, False),
        "bank_details.ifsc_code": (validate_ifsc, True),
    },
    "DOC-02": {
        "sum_insured": (_currency, False),
    },
    "DOC-03": {
        "admission_date": (normalize_date_to_iso, True),
        "discharge_date": (normalize_date_to_iso, True),
    },
}
_STATIC_REQUIRED: Dict[str, FrozenSet[str]] = {
    "DOC-01": frozenset({"applicant.full_name"}),
    "DOC-02": frozenset({"proposer_name"}),
    "DOC-03": frozenset({"patient_name"}),
}
STATIC_SCHEMA_IDS = frozenset(_STATIC_VALIDATORS)


class _StaticSchema(_Schema):
    def __init__(self, schema_id: str) -> None:
        from doc_extractor.extractor import SCHEMA_MAP                      # lazy: the extractor module is heavy

        self.schema_id = schema_id
        self.version = STATIC_SCHEMA_VERSION
        self._declared = schema_field_paths(SCHEMA_MAP[schema_id])
        self._kinds, self._arrays = _static_shape(SCHEMA_MAP[schema_id])
        self._validators = _STATIC_VALIDATORS[schema_id]
        self._required = _STATIC_REQUIRED[schema_id]

    def field(self, path: str) -> Optional[_FieldSpec]:
        key = _canonical_index_form(path)
        if key not in self._declared or not _positions_ok(path, self._arrays):
            return None
        container = any(other.startswith(key + ".") for other in self._declared)
        validator, text_only = self._validators.get(key, (None, False))
        return _FieldSpec(
            required=key in self._required, container=container, text_only=text_only, normalize=validator,
            value_kind=self._kinds.get(key),
        )

    def cross_field(
        self, values: Dict[str, Any], envelope: Dict[str, ExtractedField], corrected: Sequence[str]
    ) -> List[CorrectionIssue]:
        issues: List[CorrectionIssue] = []
        if self.schema_id == "DOC-03":
            touched = tuple(p for p in corrected if p in ("admission_date", "discharge_date"))
            if touched:
                admission, _ = normalize_date_to_iso(_text(values.get("admission_date")))
                discharge, _ = normalize_date_to_iso(_text(values.get("discharge_date")))
                flag = validate_chronology(admission, discharge)
                if flag:
                    issues.append(CorrectionIssue(_token(flag.get("code"), CorrectionCode.RULE_VIOLATION), touched))
        if self.schema_id == "DOC-02":
            touched = tuple(p for p in corrected if _canonical_index_form(p) == "nominees.share_percentage")
            if touched:
                shares = [
                    number for key, raw in values.items()
                    if _canonical_index_form(key) == "nominees.share_percentage" and (number := _number(raw)) is not None
                ]
                if len(shares) > 1:                                         # Module C compares only a group of two or more
                    flag = validate_arithmetic(shares, 100.0)
                    if flag:
                        issues.append(CorrectionIssue(_token(flag.get("code"), CorrectionCode.RULE_VIOLATION), touched))
        return issues


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _number(value: Any) -> Optional[float]:
    """A finite number from an int, a float or a numeric string; None otherwise (booleans are not numbers)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def _rule_context_value(raw: Any, normalized: Any, error: Optional[str], spec: "_FieldSpec") -> Any:
    """
    The value the cross-field rules see for one corrected field: the normalized number for an integer or decimal field that
    was normalized without a validator error, else the raw value (unchanged behavior for every other field).
    """
    if (
        spec.normalize is not None and not error and spec.value_kind in ("integer", "decimal")
        and isinstance(normalized, (int, float)) and not isinstance(normalized, bool) and math.isfinite(normalized)
    ):
        return normalized
    return raw


def _kind_ok(value: Any, kind: Optional[str]) -> bool:
    """Whether ``value`` conforms to the schema's declared scalar kind (``None`` kind: no constraint)."""
    if kind is None:
        return True
    if kind == "boolean":
        return isinstance(value, bool)
    number = _number(value)
    if number is None:
        return False
    return number.is_integer() if kind == "integer" else True


def _static_shape(schema_cls: type) -> Tuple[Dict[str, str], FrozenSet[str]]:
    """
    From the real pydantic model of a static schema: path (positions removed) -> "integer", "decimal" or "boolean", and the
    repeating-group fields (a list of models, such as ``nominees``).
    """
    import typing

    from pydantic import BaseModel

    kinds: Dict[str, str] = {}
    arrays: set = set()

    def kind_of(annotation: Any) -> Optional[str]:
        for arg in (typing.get_args(annotation) or (annotation,)):
            if arg is bool:
                return "boolean"
            if arg is int:
                return "integer"
            if arg is float:
                return "decimal"
        return None

    def models_in(annotation: Any) -> List[type]:
        found = []
        for arg in typing.get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                found.append(arg)
            else:
                found.extend(models_in(arg))
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.append(annotation)
        return found

    def walk(model: type, prefix: str, seen: Tuple[type, ...]) -> None:
        for name, info in model.model_fields.items():
            path = prefix + name
            kind = kind_of(info.annotation)
            if kind:
                kinds[path] = kind
            if typing.get_origin(info.annotation) is list and models_in(info.annotation):
                arrays.add(path)
            for sub in models_in(info.annotation):
                if sub not in seen:
                    walk(sub, path + ".", seen + (sub,))

    walk(schema_cls, "", (schema_cls,))
    return kinds, frozenset(arrays)


class _DynamicSchema(_Schema):
    """A ``schema_lifecycle`` snapshot, compiled with the existing compiler and judged with the existing adapter code."""

    def __init__(self, snapshot: Any) -> None:
        from doc_extractor.dynamic_adapter import DynamicExtractionAdapter      # lazy: heavy imports
        from schema_lifecycle.compiler import DynamicSchemaCompiler

        compiled = DynamicSchemaCompiler.compile(snapshot.definition)
        self.schema_id = snapshot.schema_id
        self.version = snapshot.version
        self._meta: Dict[str, Any] = dict(getattr(compiled, "__field_metadata__", {}))
        self._rules = list(getattr(compiled, "__rules__", []))
        from schema_lifecycle.enums import FieldType as _FieldType

        self._arrays = frozenset(
            key.replace("[]", "") for key, meta in self._meta.items() if meta.field_type == _FieldType.ARRAY
        )
        # The adapter's per-field normalizer reads only the metadata, never the adapter's state; the instance is built
        # without a manager and with an explicit (unused) extractor so that no model is loaded.
        self._adapter = DynamicExtractionAdapter(manager=None, extractor=object())          # type: ignore[arg-type]

    def _resolve(self, path: str) -> Optional[Any]:
        """The same path-to-metadata resolution as ``DynamicExtractionAdapter.extract_and_assemble``."""
        if path in self._meta:
            return self._meta[path]
        bracket = re.sub(r"\.\d+(\.|$)", r"[]\1", path)
        if bracket in self._meta:
            return self._meta[bracket]
        bracket_alt = re.sub(r"\.\d+", "[]", path)
        if bracket_alt in self._meta:
            return self._meta[bracket_alt]
        unindexed = re.sub(r"\.\d+(?=\.|$)", "", path)
        return self._meta.get(unindexed)

    def field(self, path: str) -> Optional[_FieldSpec]:
        from schema_lifecycle.enums import FieldType

        meta = self._resolve(path)
        if meta is None or not _positions_ok(path, self._arrays):
            return None
        kind = None
        if meta.field_type == FieldType.INTEGER:
            kind = "integer"
        elif meta.field_type == FieldType.DECIMAL:
            kind = "decimal"
        elif meta.field_type == FieldType.BOOLEAN:
            kind = "boolean"
        return _FieldSpec(
            required=bool(meta.required),
            container=meta.field_type in (FieldType.OBJECT, FieldType.ARRAY),
            text_only=False,
            normalize=lambda value, _meta=meta: self._adapter._validate_and_normalize_field(value, _meta),
            allowed_values=tuple(meta.allowed_values) if meta.allowed_values else None,
            value_kind=kind,
        )

    def cross_field(
        self, values: Dict[str, Any], envelope: Dict[str, ExtractedField], corrected: Sequence[str]
    ) -> List[CorrectionIssue]:
        from doc_extractor.rule_evaluator import evaluate_dynamic_rules

        flags: List[FlagItem] = []
        evaluate_dynamic_rules(rules=self._rules, flat_dict=values, fields_envelope=envelope, flags=flags)
        corrected_forms = {_match_form(path): path for path in corrected}
        issues: List[CorrectionIssue] = []
        for flag in flags:                                                   # the flag MESSAGE is dropped: it can quote values
            reported = [p for p in flag.fields if isinstance(p, str) and _REPORTABLE_PATH.fullmatch(p)]
            touched = tuple(dict.fromkeys(
                corrected_forms[_match_form(p)] for p in reported if _match_form(p) in corrected_forms
            ))
            if touched or not reported:                                      # a rule naming no field cannot be shown irrelevant
                issues.append(CorrectionIssue(_token(flag.code, CorrectionCode.RULE_VIOLATION), touched))
        return issues


# ---- the validator ---------------------------------------------------------------------------------------------------------

class CorrectionValidator:
    """
    Validates proposed corrections against the exact schema version of the extraction. ``schema_source`` is an exact,
    tenant-scoped snapshot lookup (``SchemaRepository.get_snapshot``); ``None`` leaves only the static schemas available.
    """

    def __init__(self, schema_source: Optional[SchemaSnapshotSource] = None) -> None:
        self._source = schema_source

    def validate(
        self,
        *,
        tenant_id: Any,
        schema_id: Any,
        schema_version: Any,
        fields: Mapping[str, ExtractedField],
        corrections: Sequence[ProposedCorrection],
    ) -> CorrectionValidationResult:
        """
        ``fields`` is the extracted field context (read, never modified); ``corrections`` are the proposed corrections.
        Returns ``REJECTED`` (nothing judged) when the exact schema cannot be used, else ``VALID`` or ``INVALID``.
        """
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            return self._rejected(CorrectionCode.TENANT_REQUIRED)
        if _is_blank(schema_id):
            return self._rejected(CorrectionCode.SCHEMA_ID_REQUIRED)
        if not isinstance(schema_id, str) or not _SCHEMA_ID.fullmatch(schema_id):
            return self._rejected(CorrectionCode.SCHEMA_ID_INVALID)
        if _is_blank(schema_version):
            return self._rejected(CorrectionCode.SCHEMA_VERSION_REQUIRED)
        version = parse_exact_version(schema_version)
        if version is None:
            return self._rejected(CorrectionCode.SCHEMA_VERSION_INVALID)

        if not isinstance(fields, Mapping) or not all(isinstance(f, ExtractedField) for f in fields.values()):
            return self._rejected(CorrectionCode.FIELD_CONTEXT_INVALID)
        resolved, failure = self._resolve(tenant_id, schema_id, version)
        if resolved is None:
            return self._rejected(failure or CorrectionCode.SCHEMA_RESOLUTION_FAILED)

        if not isinstance(corrections, (list, tuple)) or len(corrections) > MAX_CORRECTIONS:
            return CorrectionValidationResult(
                CorrectionOutcome.INVALID, (CorrectionIssue(CorrectionCode.TOO_MANY_CORRECTIONS),), resolved.schema_id, resolved.version
            )

        issues: List[CorrectionIssue] = []
        seen: set = set()
        values: Dict[str, Any] = {key: copy.deepcopy(getattr(f, "value", None)) for key, f in dict(fields).items()}
        envelope: Dict[str, ExtractedField] = {key: f.model_copy(deep=True) for key, f in dict(fields).items()}
        for correction in corrections:
            problem = self._check_one(resolved, correction, seen, values)
            if problem is None:
                continue
            code, path = problem
            issues.append(CorrectionIssue(code, (path,) if path else ()))
        accepted_paths = [p for p in (c.path for c in corrections if isinstance(c, ProposedCorrection)) if p in seen]
        validated_set = tuple(dict.fromkeys(accepted_paths))
        if not any(issue.code in _STRUCTURAL_CODES for issue in issues):
            issues.extend(resolved.cross_field(values, envelope, validated_set))
        outcome = CorrectionOutcome.INVALID if issues else CorrectionOutcome.VALID
        return CorrectionValidationResult(outcome, tuple(issues), resolved.schema_id, resolved.version, validated_set)

    # ---- internals ------------------------------------------------------------------------------------------------------

    @staticmethod
    def _rejected(code: str) -> CorrectionValidationResult:
        """A controlled rejection. The schema ID is NOT echoed: it is echoed only once an exact schema has been resolved."""
        return CorrectionValidationResult(CorrectionOutcome.REJECTED, (CorrectionIssue(code),), None, None)

    def _resolve(self, tenant_id: str, schema_id: str, version: int) -> Tuple[Optional[_Schema], Optional[str]]:
        """The exact schema, or (None, fixed code). Static ids are judged by the code's own rules, never by a lookup."""
        if schema_id in STATIC_SCHEMA_IDS:
            if version != STATIC_SCHEMA_VERSION:
                return None, CorrectionCode.SCHEMA_VERSION_NOT_FOUND
            return _StaticSchema(schema_id), None
        if self._source is None:
            return None, CorrectionCode.SCHEMA_SOURCE_UNAVAILABLE
        try:
            snapshot = self._source.get_snapshot(schema_id, version, tenant_id)       # EXACT: never latest, never active
            if snapshot is None:
                return None, CorrectionCode.SCHEMA_VERSION_NOT_FOUND
            if (
                getattr(snapshot, "tenant_id", None) != tenant_id
                or getattr(snapshot, "schema_id", None) != schema_id
                or getattr(snapshot, "version", None) != version
            ):
                return None, CorrectionCode.SCHEMA_RESOLUTION_FAILED               # an adapter returned a different schema
            return _DynamicSchema(snapshot), None
        except Exception as exc:                                                       # noqa: BLE001 - never propagates its text
            logger.error("Correction schema resolution fault (reason=schema_resolution_failed, exception=%s)", type(exc).__name__)
            return None, CorrectionCode.SCHEMA_RESOLUTION_FAILED

    @staticmethod
    def _check_one(
        schema: _Schema, correction: Any, seen: set, values: Dict[str, Any]
    ) -> Optional[Tuple[str, Optional[str]]]:
        """(code, declared path or None) for the first problem of one correction; on success the merged value is recorded."""
        if not isinstance(correction, ProposedCorrection):
            return CorrectionCode.INVALID_CORRECTION, None
        path = correction.path
        if not isinstance(path, str) or len(path) > MAX_PATH_LENGTH or not _PATH.fullmatch(path):
            return CorrectionCode.INVALID_FIELD_PATH, None
        spec = schema.field(path)
        if spec is None:
            return CorrectionCode.UNKNOWN_FIELD_PATH, None                              # the client's path is not echoed
        if spec.container:
            return CorrectionCode.FIELD_NOT_CORRECTABLE, path
        if path in seen:
            return CorrectionCode.DUPLICATE_CORRECTION, path
        seen.add(path)

        value = correction.value
        problem = _scalar_problem(value)
        if problem:
            return problem, path
        if _is_blank(value):                                                            # a null correction (FR-REV-002)
            values[path] = None
            return (CorrectionCode.REQUIRED_FIELD_MISSING, path) if spec.required else None
        if spec.text_only and not isinstance(value, str):
            return CorrectionCode.INVALID_VALUE_TYPE, path
        normalized, error = (value, None)
        if spec.normalize is not None:
            try:
                normalized, error = spec.normalize(value)
            except Exception as exc:                                                    # noqa: BLE001 - the validators are pure
                logger.error("Correction field validator fault (reason=validator_failed, exception=%s)", type(exc).__name__)
                return CorrectionCode.INVALID_VALUE, path
        # The rules judge what the field will hold. A reviewer types free text ("₹500", "1,0"), and the comparison rules need
        # a number: for an integer or decimal field whose normalization succeeded, the NORMALIZED number is the rule context;
        # otherwise (no normalizer, a validator error, any other kind) the raw value, as before. Never a silent skip.
        values[path] = _rule_context_value(value, normalized, error, spec)
        if error:
            return _token(error, CorrectionCode.INVALID_VALUE), path
        if not _kind_ok(normalized if spec.normalize is not None and not error else value, spec.value_kind):
            return CorrectionCode.INVALID_VALUE_TYPE, path
        if spec.allowed_values is not None and value not in spec.allowed_values and normalized not in spec.allowed_values:
            return CorrectionCode.VALUE_NOT_ALLOWED, path
        return None


_STRUCTURAL_CODES = frozenset({
    CorrectionCode.INVALID_CORRECTION, CorrectionCode.INVALID_FIELD_PATH, CorrectionCode.UNKNOWN_FIELD_PATH,
    CorrectionCode.DUPLICATE_CORRECTION, CorrectionCode.FIELD_NOT_CORRECTABLE,
})
