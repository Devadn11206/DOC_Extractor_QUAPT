# Decision: text-only evidence for DOC-01 handwritten values

Status: accepted (product owner decision, keep option), pending review.

PRD references: GR-002 (no value without evidence), FR-EXT-004 (page, bbox and snippet for every value),
GR-009 (handwriting never auto-approved).

## What the code does
- A value with a printed match in the page's OCR text keeps its box evidence (`source: ocr_box`).
- A value with no printed match (typically handwriting) is kept, marked `source: model_text` with no bbox,
  and always sent to review (`EVIDENCE_TEXT_ONLY`). It is never auto-approved.
- Checkbox values need their printed label on the page, or they are null (`CHECKBOX_LABEL_NOT_FOUND`).
- Values of fewer than three characters with no printed match are dropped (`EVIDENCE_TOO_SHORT`).

## Why
A strict rule (null without printed evidence) would leave most handwritten fields empty. Reviewers would
retype them, and the tool would save little work. With this exception no unverified handwritten value is
auto-approved.

## Conditions
1. This exception applies only to values routed to review. It does not allow auto-approval.
2. Revisit when the handwriting reader is measured on the labelled set. If most handwritten fields are
   right, move to the strict rule.
