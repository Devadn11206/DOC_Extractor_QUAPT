"""
Adapter-aware entry into Module C for routed page results (PRD v1.5 D14, FR-RTE-004).

v1.5 reads each page with whichever specialist the routing table picks: a vision model (``VLMAdapter``,
boxes) or GLM-OCR + a text LLM (``OCRTextAdapter``, no boxes). The app runs that inference page by page
(so it can stream progress and rotate pages). ``ModuleCPipeline`` stays the ONE place that normalizes
values, maps evidence boxes, validates, scores and assembles the canonical envelope.

``RoutedResultsExtractor`` is the seam between the two: it replays the already-obtained page results
through the extractor interface ``ModuleCPipeline`` expects (``extract_with_provenance`` /
``extract_generic_pages``). It performs NO model inference, so nothing is read twice.

Rules it enforces (all deterministic):

* Per page, raw model JSON ``{"path": {"value", "bbox", "uncertain"}}`` becomes typed leaves with the
  same collectors the VLM path uses; a failed page is recorded and does not discard the others.
* A page read by a route without boxes (``route["grounded"] is False``, e.g. OCR + text LLM) can never
  carry evidence: any box is discarded, so Module C nulls those values and raises ``EVIDENCE_MISSING``
  (PRD GR-002 / FR-EXT-004). The unverified readings are kept in the provenance details for review
  tooling only; they are not field values.
* A page that was turned upright before the model read it (``rotation`` clockwise degrees) has its boxes
  mapped back to the stored page's frame, so the Module A geometry (``PageImageRef.page_ref`` through
  ``CoordinateTransform``) stays the authoritative source of the pixel coordinates.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type

from pydantic import BaseModel

from doc_extractor.contracts_b import ModuleBOutputPayload
from doc_extractor.contracts_c import (
    ExecutionMode,
    ExecutionProvenance,
    ExtractionResult,
    GenericExtractionResult,
    PageImageRef,
)
from doc_extractor.vlm_grounding import (
    PageLeaf,
    aggregate_leaves,
    build_partial_model_from_pages,
    collect_generic_leaves,
    collect_schema_leaves,
    unrotate_bbox_norm,
    validate_page_leaves,
)


@dataclass
class RoutedPageResult:
    """One page as read by a routed specialist (see ``RoutedExtractor.extract``)."""

    page_number: int
    raw: Optional[Dict[str, Any]] = None
    provenance: Optional[ExecutionProvenance] = None
    route: Dict[str, Any] = field(default_factory=dict)
    rotation: int = 0                     # clockwise degrees the page was turned before the model read it
    error: Optional[str] = None           # set when this page's extraction failed


class RoutedResultsExtractor:
    """Replays pre-computed routed page results through the Module C extractor interface (no inference)."""

    multimodal = True

    def __init__(self, results: Sequence[RoutedPageResult], prompt_version: str = "1.0.0") -> None:
        self._results = list(results)
        self.prompt_version = prompt_version
        self.last_provenance: Optional[ExecutionProvenance] = None

    # -- labels --------------------------------------------------------------------------------
    @property
    def model_name(self) -> str:
        names: List[str] = []
        for r in self._results:
            name = r.route.get("model_used") or (r.provenance.model_name if r.provenance else None)
            if name and name not in names:
                names.append(str(name))
        return ", ".join(names) or "routed"

    # -- the extractor interface ModuleCPipeline calls -----------------------------------------
    def extract_with_provenance(
        self,
        payload: ModuleBOutputPayload,
        schema_cls: Optional[Type[BaseModel]] = None,
        page_images: Optional[Sequence[PageImageRef]] = None,
    ) -> ExtractionResult:
        if schema_cls is None:
            raise ValueError("A schema-bound extraction needs schema_cls")
        processed, details = self._read(schema_cls, self._available(page_images))
        model, grounding, missing = build_partial_model_from_pages(processed, schema_cls)
        details["required_fields_missing"] = missing
        prov = self._provenance(details)
        self.last_provenance = prov
        return ExtractionResult(payload=model, provenance=prov, grounding=grounding)

    def extract_generic_pages(
        self, page_images: Sequence[PageImageRef], system_prompt: str
    ) -> GenericExtractionResult:
        processed, details = self._read(None, self._available(page_images))
        values, grounding = aggregate_leaves(processed)
        prov = self._provenance(details)
        self.last_provenance = prov
        return GenericExtractionResult(values=values, provenance=prov, grounding=grounding)

    # -- internals -----------------------------------------------------------------------------
    def _ordered(self) -> List[RoutedPageResult]:
        unique: Dict[int, RoutedPageResult] = {}
        for r in self._results:
            unique.setdefault(r.page_number, r)
        return [unique[n] for n in sorted(unique)]

    @staticmethod
    def _regrounded(leaves: Dict[str, PageLeaf], result: RoutedPageResult) -> Dict[str, PageLeaf]:
        grounded = bool(result.route.get("grounded", True))
        out: Dict[str, PageLeaf] = {}
        for path, leaf in leaves.items():
            box: Optional[Tuple[float, float, float, float]] = leaf.bbox_norm
            if not grounded:
                box = None                                  # a route without boxes cannot carry evidence
            elif box is not None and result.rotation:
                box = unrotate_bbox_norm(box, result.rotation)
            out[path] = PageLeaf(value=leaf.value, bbox_norm=box, raw_value=leaf.raw_value, uncertain=leaf.uncertain,
                                confidence=leaf.confidence)
        return out

    @staticmethod
    def _available(page_images: Optional[Sequence[PageImageRef]]) -> Optional[set]:
        """Page numbers that have an explicit image reference (None = the caller supplied none to check)."""
        return {ref.page_number for ref in page_images} if page_images else None

    def _read(
        self, schema_cls: Optional[Type[BaseModel]], available_pages: Optional[set] = None
    ) -> Tuple[List[Tuple[int, Dict[str, PageLeaf]]], Dict[str, Any]]:
        ordered = self._ordered()
        processed: List[Tuple[int, Dict[str, PageLeaf]]] = []
        failed: List[Dict[str, Any]] = []
        empty: List[int] = []
        unverified: Dict[str, Dict[str, Any]] = {}

        for result in ordered:
            try:
                if available_pages is not None and result.page_number not in available_pages:
                    # Without the page's Module A reference there is no geometry to map its boxes with:
                    # the page is reported (PARTIAL_PAGE_COVERAGE), exactly like the direct VLM path does.
                    failed.append({"page": result.page_number, "reason": "no page image reference supplied"})
                    continue
                if result.error:
                    raise RuntimeError(result.error)
                if not isinstance(result.raw, dict):
                    raise ValueError(f"model output is not a JSON object (got {type(result.raw).__name__})")
                leaves = (
                    collect_schema_leaves(result.raw, schema_cls) if schema_cls is not None
                    else collect_generic_leaves(result.raw)
                )
                if schema_cls is not None:
                    validate_page_leaves(schema_cls, leaves)
                leaves = self._regrounded(leaves, result)
            except Exception as exc:
                reason = result.error if result.error else f"{type(exc).__name__}: {exc}"
                failed.append({"page": result.page_number, "reason": reason})
                continue
            processed.append((result.page_number, leaves))
            if not leaves:
                empty.append(result.page_number)
            if not result.route.get("grounded", True) and leaves:
                unverified[str(result.page_number)] = {p: lf.value for p, lf in leaves.items()}

        if not processed:
            reasons = "; ".join(f"page {f['page']}: {f['reason']}" for f in failed) or "no page results supplied"
            raise ValueError(f"No page could be processed ({len(ordered)} attempted): {reasons}")

        details: Dict[str, Any] = {
            "input_modality": "image",
            "pages_attempted": [r.page_number for r in ordered],
            "pages_processed": [n for n, _ in processed],
            "pages_failed": failed,
            "pages_empty": empty,
            "routing": [
                {"page": r.page_number, "rotation": r.rotation, **{k: v for k, v in r.route.items()}}
                for r in ordered
            ],
        }
        if unverified:
            details["ungrounded_pages"] = sorted(int(p) for p in unverified)
            details["unverified_readings"] = unverified     # review aid only: NOT field values
        return processed, details

    def _provenance(self, details: Dict[str, Any]) -> ExecutionProvenance:
        ordered = [r for r in self._ordered() if r.provenance is not None]
        providers: List[str] = []
        for r in ordered:
            p = r.provenance.provider
            if p and p not in providers:
                providers.append(p)
        fallback = [r for r in ordered if r.provenance.fallback_occurred]
        return ExecutionProvenance(
            mode=ExecutionMode.GENUINE_LLM,
            model_name=self.model_name,
            provider="+".join(providers) or "routed",
            prompt_version=self.prompt_version,
            fallback_occurred=bool(fallback),
            fallback_reason=(fallback[0].provenance.fallback_reason if fallback else None),
            details=details,
        )
