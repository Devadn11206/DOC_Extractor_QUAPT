"""
Review routing and review-task draft generation (FR-CNF-003), Phase 3 / Slice 2B-prep. PROVISIONAL. PURE LOGIC.

``route_extraction`` decides, from a canonical extraction envelope, whether the extraction may be auto-approved or needs a
human review, and for a review builds a ``ReviewTaskDraft``: the tenant and document identity, the FLAGGED field paths only,
fixed codes, and an initial ``unassigned`` state. It stores nothing, updates no document status, creates no task, reads no
clock and writes no audit event: the caller (a future persistence integration) decides what to do with the result.

ONE GATE, NOT TWO. The decision is ``field_assembly.assess_review_requirement``: the very rule that
``assemble_canonical_envelope`` applies to set ``needs_review`` versus ``auto_approved`` (it was extracted from there, not
copied, so the two cannot drift). This module adds no confidence threshold, no validation rule and no review policy. In
particular it never compares a confidence with a threshold: a field's per-field threshold (FR-CNF-002) is applied when the field
is assembled and recorded in its ``review_required`` indicator, which is what this module reads.

What this module adds around the shared gate (integrity guards, none of them a policy):

- IDENTITY. The envelope carries no tenant, so the caller states the tenant and the expected document identifier (from the
  trusted document record). The envelope's own ``document_id`` must equal the expected one; a mismatch is refused.
- CONTRADICTORY OR MISSING METADATA FAILS TOWARD REVIEW. A field whose indicators cannot be trusted (a confidence outside
  [0, 1] or not finite, a ``validation`` or ``review_required`` of the wrong type, a provenance that is not a member of the
  enum, an object that is not an ``ExtractedField``) is a flagged field (``FIELD_METADATA_INVALID``), never an approved one. An
  envelope whose recorded status says ``auto_approved`` while the gate says review is routed to review (``STATUS_CONFLICT``).
  An envelope that cannot be read at all (not an envelope, flags that are not flags) is refused, not guessed.
- ROUTABLE STATUS. Only an envelope whose recorded status is ``auto_approved`` or ``needs_review`` is routed; ``failed``,
  ``processing``, ``reviewed`` and ``rejected`` are not extraction results awaiting a routing decision and are refused.

THE DRAFT HOLDS CODES AND IDENTIFIERS ONLY. It never holds an extracted value, evidence text, a flag message (which can quote
values), a model prompt, a run diagnostic or a document type. Field identifiers go through the SAME mapping as the API-004
response (``api.contracts.extraction._FieldNames``): a schema-canonical path keeps its name, every other field becomes a
positional ``field_N`` and an unmappable flag reference is dropped (only that reference: the routing, the other references and
the flag's own code are kept), so a model-chosen field key (which can be document
content) is never copied. A test pins that the draft's identifiers equal the response's for the same envelope. Flag codes are
copied only when they are upper-case tokens; anything else becomes the fixed ``RULE_VIOLATION``.

NOT PROVIDED, BY DESIGN: a task identifier (assigned by whoever persists the task), a priority and ``slaDueAt`` (PRD section 20
lists them, but no trusted contract supplies a source: ``ReviewTaskRecord`` says they are not modelled yet), and any guarantee of
idempotency or uniqueness (one draft per document needs a persistence-side constraint that this pure function cannot give).
Nor is it atomic with anything: it has no side effect.
"""

import logging
import re
from datetime import datetime
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from doc_extractor.api.contracts.extraction import _FieldNames  # the ONE internal-to-public field identifier mapping (API-004)
from doc_extractor.api.contracts.records import ReviewTaskStatus
from doc_extractor.contracts_b import ConfidenceProvenance
from doc_extractor.contracts_c import CanonicalExtractionEnvelope, ExtractedField, FlagItem
from doc_extractor.field_assembly import ReviewReason, assess_review_requirement

logger = logging.getLogger("doc_extractor.api.review_routing")

_CODE_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{1,63}")                          # same shape as the correction-validation codes
_PUBLIC_PATH = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")           # canonical paths, positions and ``field_N``
MAX_PATH_LENGTH = 128
FALLBACK_FLAG_CODE = "RULE_VIOLATION"                                     # for a flag code that is not an upper-case token
_ROUTABLE_STATUSES = frozenset({"auto_approved", "needs_review"})
_VALIDATION_VALUES = frozenset({"passed", "failed", "not_applicable"})


class RoutingReason:
    """Every reason code a decision or draft can carry: the shared gate's codes plus the two integrity guards of this module."""

    FIELD_METADATA_INVALID = "FIELD_METADATA_INVALID"   # a field's indicators are missing or contradictory: it cannot be trusted
    STATUS_CONFLICT = "STATUS_CONFLICT"                 # the recorded status says auto_approved but the gate says review
    GATE = frozenset(member.value for member in ReviewReason)
    ALL = GATE | {FIELD_METADATA_INVALID, STATUS_CONFLICT}


class RoutingErrorCode:
    """The fixed codes of a refused request. No code, and no message, ever carries input."""

    INVALID_ENVELOPE = "INVALID_ENVELOPE"
    TENANT_REQUIRED = "TENANT_REQUIRED"
    DOCUMENT_ID_REQUIRED = "DOCUMENT_ID_REQUIRED"
    DOCUMENT_ID_MISMATCH = "DOCUMENT_ID_MISMATCH"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    STATUS_NOT_ROUTABLE = "STATUS_NOT_ROUTABLE"
    ROUTING_FAILED = "ROUTING_FAILED"


_MESSAGES = {
    RoutingErrorCode.INVALID_ENVELOPE: "The extraction result cannot be read.",
    RoutingErrorCode.TENANT_REQUIRED: "A tenant identifier is required.",
    RoutingErrorCode.DOCUMENT_ID_REQUIRED: "A document identifier is required.",
    RoutingErrorCode.DOCUMENT_ID_MISMATCH: "The extraction result does not belong to the stated document.",
    RoutingErrorCode.INVALID_TIMESTAMP: "A timezone-aware creation time is required.",
    RoutingErrorCode.STATUS_NOT_ROUTABLE: "The extraction result is not awaiting a routing decision.",
    RoutingErrorCode.ROUTING_FAILED: "Review routing failed.",
}


class ReviewRoutingError(Exception):
    """A refused routing request. ``code`` is one of ``RoutingErrorCode``; the message is fixed and never carries input."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _MESSAGES else RoutingErrorCode.ROUTING_FAILED
        super().__init__(_MESSAGES[self.code])


class ReviewRoutingOutcome(str, Enum):
    AUTO_APPROVE = "auto_approve"      # every applicable field passed validation and its confidence gate
    NEEDS_REVIEW = "needs_review"      # at least one document-level or field-level reason requires a human review


# ---- the draft and the decision ----------------------------------------------------------------------------------------------

def _is_public_path(value: Any) -> bool:
    """The ONE definition of a public field identifier a draft may hold (ASCII, bounded, no whitespace)."""
    return isinstance(value, str) and len(value) <= MAX_PATH_LENGTH and _PUBLIC_PATH.fullmatch(value) is not None


def _tokens(values: Tuple[str, ...], allowed: Optional[FrozenSet[str]] = None) -> Tuple[str, ...]:
    for value in values:
        if not isinstance(value, str) or not _CODE_TOKEN.fullmatch(value) or (allowed is not None and value not in allowed):
            raise ValueError("a code is not a fixed token")
    if len(set(values)) != len(values):
        raise ValueError("codes must be distinct")
    return values


class FlaggedField(BaseModel):
    """One flagged field: its public identifier, why the gate flagged it, and the envelope flag codes that name it."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    path: str
    reasons: Tuple[str, ...] = ()
    flag_codes: Tuple[str, ...] = ()

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not _is_public_path(value):
            raise ValueError("not a public field identifier")
        return value

    @field_validator("reasons")
    @classmethod
    def _reasons(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return _tokens(value, RoutingReason.ALL)

    @field_validator("flag_codes")
    @classmethod
    def _flag_codes(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return _tokens(value)

    @model_validator(mode="after")
    def _flagged_for_a_reason(self) -> "FlaggedField":
        if not self.reasons and not self.flag_codes:
            raise ValueError("a flagged field has a reason or a flag code")
        return self


class ReviewTaskDraft(BaseModel):
    """
    A validated review-task DRAFT for a future persistence integration. It is not a ``ReviewTaskRecord`` (no task identifier
    exists yet) and nothing here is stored. ``status`` is always ``unassigned`` (provisional, M5-D11). Holds identifiers and fixed
    codes only: no value, no evidence text, no flag message, no document type.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    tenant_id: str = Field(..., min_length=1)
    document_id: str = Field(..., min_length=1)
    status: ReviewTaskStatus = ReviewTaskStatus.UNASSIGNED
    created_at: datetime
    flagged_fields: Tuple[FlaggedField, ...] = ()
    flag_codes: Tuple[str, ...] = ()
    document_reasons: Tuple[str, ...] = ()

    @field_validator("status")
    @classmethod
    def _unassigned(cls, value: ReviewTaskStatus) -> ReviewTaskStatus:
        if value is not ReviewTaskStatus.UNASSIGNED:
            raise ValueError("a draft is always unassigned")
        return value

    @field_validator("created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    @field_validator("flag_codes")
    @classmethod
    def _flag_codes(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return _tokens(value)

    @field_validator("document_reasons")
    @classmethod
    def _document_reasons(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return _tokens(value, RoutingReason.ALL)

    @model_validator(mode="after")
    def _consistent(self) -> "ReviewTaskDraft":
        paths = [item.path for item in self.flagged_fields]
        if len(set(paths)) != len(paths):
            raise ValueError("a field is flagged once")
        if not (self.flagged_fields or self.flag_codes or self.document_reasons):
            raise ValueError("a draft states why the document needs review")
        known = set(self.flag_codes)
        if any(code not in known for item in self.flagged_fields for code in item.flag_codes):
            raise ValueError("a field's flag code is a flag code of the document")
        return self


class ReviewRoutingDecision(BaseModel):
    """``AUTO_APPROVE`` carries no reason and no draft; ``NEEDS_REVIEW`` carries both. ``reasons``: the distinct reason codes."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    outcome: ReviewRoutingOutcome
    reasons: Tuple[str, ...] = ()
    draft: Optional[ReviewTaskDraft] = None

    @field_validator("reasons")
    @classmethod
    def _reasons(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        return _tokens(value, RoutingReason.ALL)

    @model_validator(mode="after")
    def _outcome_matches_content(self) -> "ReviewRoutingDecision":
        if self.outcome is ReviewRoutingOutcome.AUTO_APPROVE and (self.reasons or self.draft is not None):
            raise ValueError("an auto-approval has no reason and no draft")
        if self.outcome is ReviewRoutingOutcome.NEEDS_REVIEW and (not self.reasons or self.draft is None):
            raise ValueError("a review has reasons and a draft")
        return self

    @property
    def needs_review(self) -> bool:
        return self.outcome is ReviewRoutingOutcome.NEEDS_REVIEW


# ---- the decision ------------------------------------------------------------------------------------------------------------

def _field_metadata_trustworthy(field: Any) -> bool:
    """Whether the indicators the gate reads are present and of the declared types (the model validators can be bypassed)."""
    if not isinstance(field, ExtractedField):
        return False
    confidence = field.confidence
    if confidence is not None and (
        isinstance(confidence, bool) or not isinstance(confidence, (int, float))
        or not 0.0 <= confidence <= 1.0                                    # NaN and infinity fail this comparison too
    ):
        return False
    return (
        isinstance(field.review_required, bool)
        and field.validation in _VALIDATION_VALUES
        and isinstance(field.confidence_provenance, ConfidenceProvenance)
    )


def _safe_flag_code(code: Any) -> str:
    return code if isinstance(code, str) and _CODE_TOKEN.fullmatch(code) else FALLBACK_FLAG_CODE


def _readable(envelope: Any) -> bool:
    if not isinstance(envelope, CanonicalExtractionEnvelope):
        return False
    fields, flags = envelope.fields, envelope.flags
    return (
        isinstance(fields, dict) and all(isinstance(key, str) for key in fields)
        and isinstance(flags, list)
        and all(isinstance(flag, FlagItem) and isinstance(flag.fields, (list, tuple)) for flag in flags)
        and isinstance(envelope.document_id, str) and isinstance(envelope.document_metadata.document_type, str)
    )


def _blank(value: Any) -> bool:
    return not isinstance(value, str) or not value.strip()


def route_extraction(
    envelope: CanonicalExtractionEnvelope, *, tenant_id: str, document_id: str, now: datetime
) -> ReviewRoutingDecision:
    """
    Decide ``AUTO_APPROVE`` or ``NEEDS_REVIEW`` for ``envelope`` and, for a review, build the task draft. Deterministic: the same
    inputs always give the same result. Reads the envelope, never modifies it. ``now`` is the creation time of the draft (the
    function reads no clock); it must be timezone-aware. ``tenant_id`` and ``document_id`` come from the trusted document record;
    the envelope's own ``document_id`` must equal ``document_id``.

    Raises ``ReviewRoutingError`` (a fixed code, no input echoed) when the request cannot be routed.
    """
    if _blank(tenant_id):
        raise ReviewRoutingError(RoutingErrorCode.TENANT_REQUIRED)
    if _blank(document_id):
        raise ReviewRoutingError(RoutingErrorCode.DOCUMENT_ID_REQUIRED)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ReviewRoutingError(RoutingErrorCode.INVALID_TIMESTAMP)
    try:
        if not _readable(envelope):
            raise ReviewRoutingError(RoutingErrorCode.INVALID_ENVELOPE)
        if envelope.document_id != document_id:
            raise ReviewRoutingError(RoutingErrorCode.DOCUMENT_ID_MISMATCH)
        status = envelope.document_metadata.status
        if status not in _ROUTABLE_STATUSES:
            raise ReviewRoutingError(RoutingErrorCode.STATUS_NOT_ROUTABLE)
        return _decide(envelope, tenant_id, document_id, now, status)
    except ReviewRoutingError:
        raise
    except Exception as exc:                                       # noqa: BLE001 - never propagates its text
        logger.error("Review routing fault (reason=routing_failed, exception=%s)", type(exc).__name__)
        raise ReviewRoutingError(RoutingErrorCode.ROUTING_FAILED) from None


def _decide(
    envelope: CanonicalExtractionEnvelope, tenant_id: str, document_id: str, now: datetime, status: str
) -> ReviewRoutingDecision:
    untrusted = {key for key, field in envelope.fields.items() if not _field_metadata_trustworthy(field)}
    trusted = {key: field for key, field in envelope.fields.items() if key not in untrusted}

    # THE shared gate, on the fields whose indicators can be read. ``payload_status`` is not an input here: the recorded status
    # is handled below, so that a reason is never attributed to the upstream payload when the fields explain the review.
    assessment = assess_review_requirement(
        envelope.document_metadata.document_type, trusted, envelope.flags, payload_status="processing"
    )
    document_reasons: List[str] = [reason.value for reason in assessment.document_reasons]
    field_reasons: Dict[str, List[str]] = {key: [reason.value for reason in reasons] for key, reasons in assessment.field_reasons}
    for key in untrusted:
        field_reasons.setdefault(key, []).append(RoutingReason.FIELD_METADATA_INVALID)

    gate_says_review = bool(document_reasons) or bool(field_reasons)
    if status == "needs_review" and not gate_says_review:
        document_reasons.append(ReviewReason.UPSTREAM_NEEDS_REVIEW.value)      # the recorded status is the only signal
    elif status == "auto_approved" and gate_says_review:
        document_reasons.append(RoutingReason.STATUS_CONFLICT)                 # never approve on contradictory metadata

    if not document_reasons and not field_reasons:
        return ReviewRoutingDecision(outcome=ReviewRoutingOutcome.AUTO_APPROVE)

    # ---- the draft: flagged fields only, public identifiers, fixed codes ----
    names = _FieldNames.for_envelope(envelope)
    flag_codes: List[str] = []
    codes_by_path: Dict[str, List[str]] = {}
    for flag in envelope.flags:
        code = _safe_flag_code(flag.code)
        if code not in flag_codes:
            flag_codes.append(code)
        for reference in flag.fields:
            path = names.public_reference(reference)
            if not _is_public_path(path):                          # an unmappable reference is dropped, never the whole routing
                continue
            if code not in codes_by_path.setdefault(path, []):
                codes_by_path[path].append(code)

    flagged: List[FlaggedField] = []
    for key in envelope.fields:                                    # the envelope's order, then flag-only references
        path = names.public(key)
        reasons = field_reasons.get(key, [])
        codes = codes_by_path.pop(path, [])
        if reasons or codes:
            flagged.append(FlaggedField(path=path, reasons=tuple(reasons), flag_codes=tuple(codes)))
    for path, codes in codes_by_path.items():
        flagged.append(FlaggedField(path=path, flag_codes=tuple(codes)))

    draft = ReviewTaskDraft(
        tenant_id=tenant_id, document_id=document_id, status=ReviewTaskStatus.UNASSIGNED, created_at=now,
        flagged_fields=tuple(flagged), flag_codes=tuple(flag_codes), document_reasons=tuple(document_reasons),
    )
    distinct = tuple(dict.fromkeys(
        document_reasons + [reason for item in flagged for reason in item.reasons]
    ))
    return ReviewRoutingDecision(outcome=ReviewRoutingOutcome.NEEDS_REVIEW, reasons=distinct, draft=draft)
