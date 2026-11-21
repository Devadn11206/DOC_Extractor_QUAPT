"""
Public response contract of API-004 (``POST /api/v1/documents/{id}/extract-sync``), PROVISIONAL.

The extraction pipeline produces an in-memory ``CanonicalExtractionEnvelope`` (PRD section 6.3). It is NEVER serialized
as is: this module defines an explicit public shape and a serializer that copies only fields that are safe to expose.

PROVISIONAL privacy rule (no masking policy is approved yet, SEC-007 / the response-masking decision stay open):

- every extracted VALUE is redacted (``value`` is always ``null``; ``value_present`` says only whether one was extracted);
- the evidence TEXT snippet is never exposed; the evidence GEOMETRY (page and bounding box) is, because it carries no content;
- flag MESSAGES are never exposed (they can quote values); flag ``code`` and the field paths are;
- run diagnostics (provider and model identifiers, fallback reasons and details, routing, grounding, prompts) are never exposed;
- confidence, its provenance, ``null_reason``, ``review_required`` and ``validation`` are exposed;
- FIELD NAMES: in generic or unbound extraction the model chooses the field keys (``routing.py``: "keyed by its printed
  label"), so a key can be document content. A name is exposed only when it is a canonical identifier of the schema that is
  really bound (``document_type`` is a key of ``extractor.SCHEMA_MAP`` and the path belongs to that schema's own field set);
  every other field gets a positional public id (``field_1``, ``field_2``...), through ONE mapping that is also applied to
  ``flags[].fields`` (an unmappable reference is dropped, never copied);
- DOCUMENT TYPE AND SCHEMA ID: both come from the pipeline but are exposed only when they are members of the code-defined set
  (``MVPDocumentType`` plus the generic type). Unlike the free-text metadata below they are required, so any other value is not
  omitted: it fails the serialization, which the extraction service answers as its generic, fixed-reason 500;
- FREE-TEXT METADATA: ``document_category``, ``document_subtype`` and ``language`` come from a model. They are exposed only
  when they equal a member of the constrained set the code already defines (``DocumentCategory``, ``UNSTRUCTURED_SUBTYPES``,
  the ``LANGUAGES`` codes); anything else is omitted (``null``).

The redaction applies to this response only; the internal envelope object is left intact.

This is a deliberate narrowing of the PRD's "envelope" response and is reported as an open conflict, not a decision.
"""

import re
import typing
from typing import Dict, FrozenSet, List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from doc_extractor.contracts_c import CanonicalExtractionEnvelope, EvidenceSnippet, ExtractedField

REDACTION_NOTICE = "redacted"

# A list index inside a canonical path (``nominees.0.name``): one to three ASCII digits and nothing else. Matched with
# ``fullmatch`` on an explicit ``[0-9]`` class: ``\d`` accepts other scripts' digits and ``$`` accepts a trailing newline.
_INDEX_SEGMENT = re.compile(r"[0-9]{1,3}")

# The generic (no bound schema) document type, as the extraction pipeline names it (``ModuleC.process_routed_pages`` default).
GENERIC_DOCUMENT_TYPE = "generic"


class PublicEvidence(BaseModel):
    """Evidence GEOMETRY only: the source text snippet is redacted and is not part of the contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int = Field(..., ge=1)
    bbox: Tuple[float, float, float, float] = Field(..., description="(x0, y0, x1, y1) in pixels of the 200 DPI page image")


class PublicField(BaseModel):
    """One extracted field with its value redacted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: None = Field(default=None, description="Always null: extracted values are redacted (provisional)")
    value_present: bool = Field(..., description="True when a value was extracted (its content is redacted)")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    confidence_provenance: str
    null_reason: Optional[Literal["absent", "illegible"]] = None
    review_required: bool
    validation: Literal["passed", "failed", "not_applicable"]
    evidence: Optional[PublicEvidence] = None
    additional_evidence: List[PublicEvidence] = Field(default_factory=list)


class PublicFlag(BaseModel):
    """A business-rule or anomaly flag without its human-readable message (which may quote values)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    fields: List[str] = Field(default_factory=list)


class PublicDocumentMetadata(BaseModel):
    """``document_type`` and ``schema_id`` are exposed only when they are members of the code-defined set (see ``_allowed_ids``)."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    document_type: str
    schema_id: str
    schema_version: str
    classification_confidence: float
    status: str
    page_count: int
    document_category: Optional[str] = None
    document_subtype: Optional[str] = None
    language: Optional[str] = None
    pages_processed: Optional[int] = None

    @field_validator("document_type", "schema_id")
    @classmethod
    def _code_defined(cls, value: str) -> str:
        """Unlike the free-text metadata (omitted when unknown) these are required: an unknown value fails the whole response."""
        if not isinstance(value, str) or value not in _allowed_ids():
            raise ValueError("not a code-defined identifier")
        return value


class PublicRun(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    prompt_version: str
    confidence_calibrated: Optional[bool] = None


class Redaction(BaseModel):
    """Makes the provisional redaction explicit in every response."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    values: str = REDACTION_NOTICE
    evidence_text: str = REDACTION_NOTICE
    flag_messages: str = REDACTION_NOTICE
    field_names: str = "schema-defined identifiers only; others are positional (field_N)"
    free_text_metadata: str = "category, subtype and language omitted unless in a constrained set"
    policy: str = "provisional: no approved masking policy yet"


class ExtractionResponse(BaseModel):
    """API-004 200 response: the canonical envelope's safe subset, with values and evidence text redacted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_metadata: PublicDocumentMetadata
    fields: Dict[str, PublicField] = Field(default_factory=dict)
    flags: List[PublicFlag] = Field(default_factory=list)
    run: PublicRun
    redaction: Redaction = Field(default_factory=Redaction)

    @classmethod
    def from_envelope(cls, envelope: CanonicalExtractionEnvelope) -> "ExtractionResponse":
        """Copy only the safe fields. The envelope is read, never modified."""
        meta = envelope.document_metadata
        names = _FieldNames.for_envelope(envelope)
        return cls(
            document_id=envelope.document_id,
            document_metadata=PublicDocumentMetadata(
                document_type=meta.document_type,
                schema_id=meta.schema_id,
                schema_version=str(meta.schema_version),
                classification_confidence=meta.classification_confidence,
                status=meta.status,
                page_count=meta.page_count,
                document_category=_constrained(meta.document_category, _allowed_categories()),
                document_subtype=_constrained(meta.document_subtype, _allowed_subtypes()),
                language=_constrained(meta.language, _allowed_languages()),
                pages_processed=meta.pages_processed,
            ),
            fields={names.public(name): _public_field(field) for name, field in envelope.fields.items()},
            flags=[
                PublicFlag(code=flag.code, fields=[p for p in (names.public_reference(f) for f in flag.fields) if p])
                for flag in envelope.flags
            ],
            run=PublicRun(
                run_id=envelope.run.run_id,
                prompt_version=envelope.run.prompt_version,
                confidence_calibrated=envelope.run.confidence_calibrated,
            ),
        )


# ---- constrained free-text metadata -----------------------------------------------------------------------------------------

def _allowed_ids() -> FrozenSet[str]:
    """
    The identifiers the code defines for ``document_type`` AND ``schema_id``: the members of ``MVPDocumentType`` (the three
    ``extractor.SCHEMA_MAP`` schemas, whose schema id is their document type, and ``UNKNOWN``) plus the generic type. Every
    pipeline path that sets either field uses one of these (``pipeline_c``, ``processing_service``, the schema registry).
    """
    from doc_extractor.contracts_b import MVPDocumentType

    return frozenset(member.value for member in MVPDocumentType) | {GENERIC_DOCUMENT_TYPE}


def _allowed_categories() -> FrozenSet[str]:
    from doc_extractor.routing import DocumentCategory          # lazy: the routing module is heavy

    return frozenset(member.value for member in DocumentCategory)


def _allowed_subtypes() -> FrozenSet[str]:
    from doc_extractor.routing import UNSTRUCTURED_SUBTYPES

    return frozenset(UNSTRUCTURED_SUBTYPES)


def _allowed_languages() -> FrozenSet[str]:
    from doc_extractor.routing import LANGUAGES

    return frozenset(LANGUAGES.values())


def _constrained(value: Optional[str], allowed: FrozenSet[str]) -> Optional[str]:
    """The model-provided string only when it is exactly a member of the code-defined constrained set, else ``None``."""
    return value if isinstance(value, str) and value in allowed else None


# ---- field identifiers ------------------------------------------------------------------------------------------------------

def schema_field_paths(schema_cls: type) -> FrozenSet[str]:
    """Every dotted field path of a pydantic schema (nested models and lists of models included): its authoritative field set."""
    from pydantic import BaseModel

    def models_in(annotation: object) -> List[type]:
        found = []
        for arg in typing.get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                found.append(arg)
            else:
                found.extend(models_in(arg))
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.append(annotation)
        return found

    def walk(model: type, prefix: str, seen: Tuple[type, ...]) -> set:
        paths = set()
        for name, info in model.model_fields.items():
            path = prefix + name
            paths.add(path)
            for sub in models_in(info.annotation):
                if sub not in seen:                               # a self-referencing schema cannot loop
                    paths |= walk(sub, path + ".", seen + (sub,))
        return paths

    return frozenset(walk(schema_cls, "", (schema_cls,)))


def _bound_field_set(document_type: str) -> Optional[FrozenSet[str]]:
    """The field set of the schema actually bound for ``document_type`` (a key of ``extractor.SCHEMA_MAP``), else ``None``."""
    from doc_extractor.extractor import SCHEMA_MAP            # lazy: the extractor module is heavy

    schema_cls = SCHEMA_MAP.get(document_type) if isinstance(document_type, str) else None
    return schema_field_paths(schema_cls) if schema_cls is not None else None


def _canonical(path: object, bound: Optional[FrozenSet[str]]) -> bool:
    """True when ``path`` belongs to the bound schema's field set (list indices such as ``nominees.0.name`` are positions)."""
    if bound is None or not isinstance(path, str) or not path:
        return False
    segments = path.split(".")
    return all(segments) and ".".join(s for s in segments if not _INDEX_SEGMENT.fullmatch(s)) in bound


class _FieldNames:
    """
    The ONE internal-to-public field identifier mapping of a response. Canonical schema identifiers keep their name; every
    other field gets ``field_N`` in order of appearance. Flag references go through the same mapping, so no model-chosen
    name is ever copied, and an unknown reference is dropped.
    """

    def __init__(self, bound: Optional[FrozenSet[str]], keys: List[str]) -> None:
        self._bound = bound
        self._mapping: Dict[str, str] = {}
        taken = {key for key in keys if _canonical(key, bound)}
        counter = 0
        for key in keys:
            if _canonical(key, bound):
                self._mapping[key] = key
                continue
            while True:
                counter += 1
                candidate = f"field_{counter}"
                if candidate not in taken:
                    break
            taken.add(candidate)
            self._mapping[key] = candidate

    @classmethod
    def for_envelope(cls, envelope: CanonicalExtractionEnvelope) -> "_FieldNames":
        return cls(_bound_field_set(envelope.document_metadata.document_type), list(envelope.fields))

    def public(self, key: str) -> str:
        return self._mapping[key]

    def public_reference(self, reference: object) -> Optional[str]:
        """A flag's field reference as a public identifier, or ``None`` (dropped) when it cannot be mapped safely."""
        if isinstance(reference, str) and reference in self._mapping:
            return self._mapping[reference]
        if _canonical(reference, self._bound):                    # a schema path that has no field entry of its own
            return reference
        return None


def _public_evidence(evidence: EvidenceSnippet) -> PublicEvidence:
    return PublicEvidence(page=evidence.page, bbox=evidence.bbox)


def _public_field(field: ExtractedField) -> PublicField:
    provenance = field.confidence_provenance
    return PublicField(
        value_present=field.value is not None,
        confidence=field.confidence,
        confidence_provenance=getattr(provenance, "value", str(provenance)),
        null_reason=field.null_reason,
        review_required=field.review_required,
        validation=field.validation,
        evidence=_public_evidence(field.evidence) if field.evidence is not None else None,
        additional_evidence=[_public_evidence(item) for item in field.additional_evidence],
    )
