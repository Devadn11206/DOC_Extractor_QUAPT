"""
Checkbox Detection and State Representation for Module B.

Implements:
- FR-PRS-002: Render every checkbox and tick as '[x]' or '[ ]' and record the mark's bounding box.
- Explicit uncertain representation ('[?]') when mark evidence is ambiguous.
- SCH-013: 200-DPI top-left pixel space coordinates for downstream evidence.
- ARC-002: Pluggable engine boundary supporting both OCR candidates and pixel analysis.
"""

from dataclasses import dataclass
from enum import Enum
import math
from typing import Any, Dict, List, Literal, Optional, Tuple
import cv2
import numpy as np

from doc_extractor.contracts_b import BoundingBox, CheckboxMark
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage


class CheckboxState(str, Enum):
    CHECKED = "[x]"
    UNCHECKED = "[ ]"
    UNCERTAIN = "[?]"


@dataclass(frozen=True)
class CandidateCheckboxRegion:
    """
    A candidate checkbox region observed by an upstream layout model,
    contour detector, or visual OCR engine.
    """
    candidate_id: str
    bbox_preprocessed: Tuple[float, float, float, float]
    raw_state_hint: Optional[str] = None  # e.g. "checked", "selected", "unchecked", "empty"
    confidence: Optional[float] = None


class CheckboxDetectorError(ValueError):
    """Raised when checkbox geometry or candidate attributes are malformed."""
    pass


class CheckboxDetector:
    """
    Detects and classifies checkbox marks, producing validated CheckboxMark contracts.
    
    Supports:
    1. Candidate-driven classification using state hints from upstream vision models.
    2. Deterministic pixel-intensity analysis over loaded image regions when hints are absent.
    3. Explicit '[?]' state for ambiguous marks (fill ratios near boundary).
    """

    def __init__(
        self,
        checked_threshold: float = 0.22,
        unchecked_threshold: float = 0.08,
        min_box_dim_px: float = 8.0,
        max_box_dim_px: float = 120.0,
        default_uncalibrated_confidence: float = 0.5,
    ) -> None:
        self.checked_threshold = checked_threshold
        self.unchecked_threshold = unchecked_threshold
        self.min_box_dim_px = min_box_dim_px
        self.max_box_dim_px = max_box_dim_px
        self.default_uncalibrated_confidence = default_uncalibrated_confidence

    def process_candidate(
        self,
        candidate: CandidateCheckboxRegion,
        loaded_page: LoadedPageImage,
    ) -> CheckboxMark:
        """
        Validates, classifies, and maps a single checkbox candidate.
        """
        x0, y0, x1, y1 = candidate.bbox_preprocessed
        w = x1 - x0
        h = y1 - y0

        if x1 < x0 or y1 < y0 or x0 < 0 or y0 < 0:
            raise CheckboxDetectorError(
                f"Malformed checkbox bounding box ({x0}, {y0}, {x1}, {y1}) in candidate {candidate.candidate_id}."
            )

        if w < self.min_box_dim_px or h < self.min_box_dim_px:
            raise CheckboxDetectorError(
                f"Degenerate checkbox dimensions {w}x{h} in candidate {candidate.candidate_id}."
            )

        # Check bounds against image dimensions
        img_h, img_w = loaded_page.image_bgr.shape[:2] if loaded_page.image_bgr is not None else (0, 0)
        if x0 >= img_w or y0 >= img_h or x1 > img_w or y1 > img_h:
            raise CheckboxDetectorError(
                f"Checkbox bounding box ({x0}, {y0}, {x1}, {y1}) exceeds image boundaries ({img_w}x{img_h})."
            )

        # 1. Determine checkbox state and confidence
        state: CheckboxState
        conf: float

        if candidate.raw_state_hint is not None:
            # Classification based on upstream engine hint
            hint_clean = candidate.raw_state_hint.strip().lower()
            if hint_clean in ("checked", "selected", "[x]", "tick", "cross", "true"):
                state = CheckboxState.CHECKED
            elif hint_clean in ("unchecked", "unselected", "[ ]", "empty", "false"):
                state = CheckboxState.UNCHECKED
            else:
                state = CheckboxState.UNCERTAIN

            if candidate.confidence is not None:
                conf = max(0.0, min(1.0, float(candidate.confidence)))
            else:
                conf = self.default_uncalibrated_confidence
        else:
            # 2. Pixel-level fallback analysis on the cropped region
            state, conf = self._analyze_pixels(loaded_page.image_bgr, candidate.bbox_preprocessed)

        # 3. Coordinate inversion (SCH-013)
        page_ref = loaded_page.page_ref
        orig_tuple = CoordinateTransform.map_bbox_preprocessed_to_original(
            bbox=candidate.bbox_preprocessed,
            original_width=page_ref.original_width_px,
            original_height=page_ref.original_height_px,
            applied_rotation_deg=page_ref.applied_rotation_deg,
            applied_deskew_deg=page_ref.applied_deskew_deg,
        )

        bbox_prep = BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)
        bbox_orig = BoundingBox.from_tuple(orig_tuple)

        return CheckboxMark(
            mark_id=candidate.candidate_id,
            state=state.value,
            ocr_confidence=round(conf, 4),
            bbox_preprocessed=bbox_prep,
            bbox_original=bbox_orig,
            is_uncertain=(state == CheckboxState.UNCERTAIN),
        )

    def _analyze_pixels(
        self,
        image_bgr: np.ndarray,
        bbox: Tuple[float, float, float, float],
    ) -> Tuple[CheckboxState, float]:
        """
        Analyzes pixel dark fill ratio inside the candidate box inner area.
        Avoids border pixels to isolate the interior checkmark.
        """
        if image_bgr is None or image_bgr.size == 0:
            return CheckboxState.UNCERTAIN, self.default_uncalibrated_confidence

        img_h, img_w = image_bgr.shape[:2]
        x0, y0, x1, y1 = [int(round(v)) for v in bbox]

        # Clamp to image boundaries
        x0 = max(0, min(img_w - 1, x0))
        y0 = max(0, min(img_h - 1, y0))
        x1 = max(0, min(img_w, x1))
        y1 = max(0, min(img_h, y1))

        if (x1 - x0) <= 4 or (y1 - y0) <= 4:
            return CheckboxState.UNCERTAIN, self.default_uncalibrated_confidence

        # Crop box
        crop = image_bgr[y0:y1, x0:x1]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if len(crop.shape) == 3 else crop

        # Otsu binarization (foreground marks become white/255)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # Inset margin to exclude the box border (20% inset on all sides)
        ch, cw = binary.shape
        inset_x = max(1, int(round(cw * 0.20)))
        inset_y = max(1, int(round(ch * 0.20)))

        interior = binary[inset_y : ch - inset_y, inset_x : cw - inset_x]
        if interior.size == 0:
            return CheckboxState.UNCERTAIN, self.default_uncalibrated_confidence

        dark_pixels = np.count_nonzero(interior)
        fill_ratio = dark_pixels / float(interior.size)

        if fill_ratio >= self.checked_threshold:
            # High fill -> Checked mark (e.g. tick, cross, filled square)
            conf = min(1.0, 0.85 + (fill_ratio - self.checked_threshold) * 0.3)
            return CheckboxState.CHECKED, conf
        elif fill_ratio <= self.unchecked_threshold:
            # Low fill -> Unchecked box
            conf = min(1.0, 0.85 + (self.unchecked_threshold - fill_ratio) * 0.5)
            return CheckboxState.UNCHECKED, conf
        else:
            # Ambiguous region between unchecked and checked -> Uncertain
            conf = self.default_uncalibrated_confidence
            return CheckboxState.UNCERTAIN, conf

