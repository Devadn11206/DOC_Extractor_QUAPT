"""
Character-Grid Reconstruction for Module B.

Implements PRD FR-PRS-003:
- Merges segmented character grids (e.g. '|J|O|H|N|' or boxed PAN/account numbers)
  into a single cohesive value before extraction without extraneous spaces.
- Preserves spatial evidence (per-character bounding boxes) mapped to original 200 DPI space (SCH-013).
- Distinguishes genuine upstream confidence from uncalibrated defaults.
- Handles multi-row grids, variable widths, missing/overlapping observations, and ambiguity honestly.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from doc_extractor.contracts_b import (
    BoundingBox,
    CharacterBoxEvidence,
    CharacterGridMark,
    LayoutBlock,
    LayoutElementType,
)
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage


@dataclass(frozen=True)
class RawCharacterObservation:
    """An individual character observation detected in a grid box."""
    char: str
    bbox_preprocessed: Tuple[float, float, float, float]
    confidence: Optional[float] = None
    row_hint: Optional[int] = None
    col_hint: Optional[int] = None
    source_box_id: Optional[str] = None


@dataclass(frozen=True)
class CandidateCharacterGrid:
    """A cluster or candidate group of character box observations forming a grid."""
    grid_id: str
    observations: List[RawCharacterObservation]
    delimiter_chars: Tuple[str, ...] = ("|", "[", "]", "_", "-", " ")


class CharacterGridError(ValueError):
    """Raised when character grid geometry is invalid or degenerate."""
    pass


class CharacterGridReconstructor:
    """
    Reconstructs segmented character grids into single values with evidence traceability.
    """

    def __init__(
        self,
        row_y_tolerance_px: float = 12.0,
        overlap_iou_threshold: float = 0.5,
        default_uncalibrated_confidence: float = 0.5,
    ) -> None:
        self.row_y_tolerance_px = row_y_tolerance_px
        self.overlap_iou_threshold = overlap_iou_threshold
        self.default_uncalibrated_confidence = default_uncalibrated_confidence

    def reconstruct_grid(
        self,
        candidate_grid: CandidateCharacterGrid,
        loaded_page: LoadedPageImage,
    ) -> CharacterGridMark:
        """
        Merges character observations into a single string while preserving per-character evidence.
        """
        if not candidate_grid.observations:
            raise CharacterGridError(
                f"Cannot reconstruct character grid {candidate_grid.grid_id}: observations list is empty."
            )

        # 1. Filter out pure box delimiters if present
        clean_obs: List[RawCharacterObservation] = []
        for obs in candidate_grid.observations:
            ch = obs.char.strip()
            if not ch or ch in candidate_grid.delimiter_chars:
                continue

            # Validate bounding box
            x0, y0, x1, y1 = obs.bbox_preprocessed
            if x1 < x0 or y1 < y0 or x0 < 0 or y0 < 0:
                raise CharacterGridError(
                    f"Malformed character bounding box ({x0}, {y0}, {x1}, {y1}) in grid {candidate_grid.grid_id}."
                )
            clean_obs.append(obs)

        if not clean_obs:
            raise CharacterGridError(
                f"No printable character observations found in grid {candidate_grid.grid_id} after delimiter filtering."
            )

        # 2. Check for explicit row/col hints vs geometric sorting
        has_explicit_hints = all(
            obs.row_hint is not None and obs.col_hint is not None for obs in clean_obs
        )

        has_ambiguity = False

        if has_explicit_hints:
            # Sort by row_hint then col_hint
            ordered_obs = sorted(clean_obs, key=lambda o: (o.row_hint, o.col_hint))  # type: ignore
        else:
            ordered_obs, has_ambiguity = self._order_geometrically(clean_obs)

        # 3. Deduplicate overlapping observations
        deduped_obs, dedup_ambiguity = self._deduplicate_overlapping(ordered_obs)
        if dedup_ambiguity:
            has_ambiguity = True

        # 4. Map each character box to original 200-DPI coordinates (SCH-013)
        page_ref = loaded_page.page_ref
        box_evidence_list: List[CharacterBoxEvidence] = []
        confidences: List[float] = []

        min_x0 = float("inf")
        min_y0 = float("inf")
        max_x1 = float("-inf")
        max_y1 = float("-inf")

        merged_chars: List[str] = []

        for idx, obs in enumerate(deduped_obs):
            bx0, by0, bx1, by1 = obs.bbox_preprocessed
            min_x0 = min(min_x0, bx0)
            min_y0 = min(min_y0, by0)
            max_x1 = max(max_x1, bx1)
            max_y1 = max(max_y1, by1)

            # Map to original 200-DPI space
            orig_tuple = CoordinateTransform.map_bbox_preprocessed_to_original(
                bbox=obs.bbox_preprocessed,
                original_width=page_ref.original_width_px,
                original_height=page_ref.original_height_px,
                applied_rotation_deg=page_ref.applied_rotation_deg,
                applied_deskew_deg=page_ref.applied_deskew_deg,
            )

            c_val = (
                obs.confidence
                if obs.confidence is not None
                else self.default_uncalibrated_confidence
            )
            bounded_conf = max(0.0, min(1.0, float(c_val)))
            confidences.append(bounded_conf)

            box_evidence_list.append(
                CharacterBoxEvidence(
                    char=obs.char,
                    char_index=idx,
                    bbox_preprocessed=BoundingBox(x0=bx0, y0=by0, x1=bx1, y1=by1),
                    bbox_original=BoundingBox.from_tuple(orig_tuple),
                    ocr_confidence=round(bounded_conf, 4),
                    is_uncertain=(obs.confidence is None or bounded_conf < 0.6),
                )
            )
            merged_chars.append(obs.char)

        reconstructed_string = "".join(merged_chars)

        # Enclosing grid bounding boxes
        enclosing_prep = (min_x0, min_y0, max_x1, max_y1)
        enclosing_orig = CoordinateTransform.map_bbox_preprocessed_to_original(
            bbox=enclosing_prep,
            original_width=page_ref.original_width_px,
            original_height=page_ref.original_height_px,
            applied_rotation_deg=page_ref.applied_rotation_deg,
            applied_deskew_deg=page_ref.applied_deskew_deg,
        )

        mean_conf = sum(confidences) / len(confidences) if confidences else 1.0

        return CharacterGridMark(
            grid_id=candidate_grid.grid_id,
            reconstructed_text=reconstructed_string,
            mean_ocr_confidence=round(mean_conf, 4),
            bbox_preprocessed=BoundingBox(x0=min_x0, y0=min_y0, x1=max_x1, y1=max_y1),
            bbox_original=BoundingBox.from_tuple(enclosing_orig),
            character_boxes=box_evidence_list,
            has_ambiguity=has_ambiguity,
        )

    def _order_geometrically(
        self,
        observations: List[RawCharacterObservation],
    ) -> Tuple[List[RawCharacterObservation], bool]:
        """Orders characters top-to-bottom across rows and left-to-right within rows."""
        sorted_by_y = sorted(observations, key=lambda o: (o.bbox_preprocessed[1], o.bbox_preprocessed[0]))

        rows: List[List[RawCharacterObservation]] = []
        curr_row: List[RawCharacterObservation] = []
        curr_y = -1.0
        ambiguity = False

        for o in sorted_by_y:
            y0 = o.bbox_preprocessed[1]
            if curr_y < 0:
                curr_row = [o]
                curr_y = y0
            elif abs(y0 - curr_y) <= self.row_y_tolerance_px:
                curr_row.append(o)
            else:
                curr_row.sort(key=lambda item: item.bbox_preprocessed[0])
                rows.append(curr_row)
                curr_row = [o]
                curr_y = y0

        if curr_row:
            curr_row.sort(key=lambda item: item.bbox_preprocessed[0])
            rows.append(curr_row)

        ordered: List[RawCharacterObservation] = []
        for r in rows:
            ordered.extend(r)

        return ordered, ambiguity

    def _deduplicate_overlapping(
        self,
        observations: List[RawCharacterObservation],
    ) -> Tuple[List[RawCharacterObservation], bool]:
        """
        Eliminates duplicate overlapping detections at the same box location.
        When two detections overlap significantly (IoU >= threshold), retains the one
        with higher confidence rather than blindly keeping the first candidate.
        """
        deduped: List[RawCharacterObservation] = []
        has_overlap_ambiguity = False

        for obs in observations:
            match_idx = -1
            for idx, existing in enumerate(deduped):
                iou = self._compute_iou(obs.bbox_preprocessed, existing.bbox_preprocessed)
                if iou >= self.overlap_iou_threshold:
                    match_idx = idx
                    has_overlap_ambiguity = True
                    break

            if match_idx >= 0:
                # Overlap found: replace if obs has higher confidence
                existing = deduped[match_idx]
                obs_conf = obs.confidence if obs.confidence is not None else -1.0
                ext_conf = existing.confidence if existing.confidence is not None else -1.0
                if obs_conf > ext_conf:
                    deduped[match_idx] = obs
            else:
                deduped.append(obs)

        return deduped, has_overlap_ambiguity

    @staticmethod
    def _compute_iou(
        boxA: Tuple[float, float, float, float],
        boxB: Tuple[float, float, float, float],
    ) -> float:
        xA = max(boxA[0], boxB[0])
        yA = max(boxA[1], boxB[1])
        xB = min(boxA[2], boxB[2])
        yB = min(boxA[3], boxB[3])

        interArea = max(0.0, xB - xA) * max(0.0, yB - yA)
        boxAArea = max(0.0, boxA[2] - boxA[0]) * max(0.0, boxA[3] - boxA[1])
        boxBArea = max(0.0, boxB[2] - boxB[0]) * max(0.0, boxB[3] - boxB[1])

        unionArea = boxAArea + boxBArea - interArea
        return interArea / unionArea if unionArea > 0 else 0.0

