"""
Image-driven character-box grid detection for Module B (FR-PRS-003).

Printed application forms (CASA account opening, KYC, PAN, DOB, IFSC, account
number) place handwriting into rows of individual boxes. A whole-page OCR pass
reads those boxes as fragments in the wrong order, so the fields are lost.

This detector works from the image itself:
  1. Finds the cell rectangles by morphological detection of horizontal and
     vertical ruling lines.
  2. Groups cells into rows (one row = one character grid).
  3. Classifies each cell: blank, orange separator/marker (delimiter), or ink.
  4. OCRs each inked cell individually (upscaled, padded crop) so every box
     yields exactly one observation with its row/column hint and cell bbox.

The output is a list of CandidateCharacterGrid objects consumed by the
existing CharacterGridReconstructor, so the rest of the pipeline is unchanged.
The detector never raises on a bad image: it returns [] and logs the reason.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np

from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.character_grids import (
    CandidateCharacterGrid,
    RawCharacterObservation,
)

logger = logging.getLogger(__name__)

# Placeholder for a word gap between boxes. Not in the reconstructor's delimiter set,
# so it survives reconstruction; the extractor turns it into a space.
WORD_GAP = "~"

_OCR_LOCK = threading.Lock()
_OCR_INSTANCE = None


def _get_ocr():
    """Lazily create one shared RapidOCR instance (thread-safe)."""
    global _OCR_INSTANCE
    with _OCR_LOCK:
        if _OCR_INSTANCE is None:
            from rapidocr_onnxruntime import RapidOCR  # local import: optional dependency

            _OCR_INSTANCE = RapidOCR()
        return _OCR_INSTANCE


@dataclass(frozen=True)
class DetectedCell:
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2.0

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0


class BoxGridDetector:
    """Detects boxed character rows in a page image and OCRs each cell."""

    def __init__(
        self,
        min_cell_px: int = 14,
        max_cell_px: int = 140,
        min_cells_per_row: int = 4,
        min_ink_fraction: float = 0.015,
        orange_fraction_delimiter: float = 0.25,
        upscale: float = 4.0,
        pad_px: int = 30,
        min_char_confidence: float = 0.3,
        det_fallback: bool = False,
        min_row_cell_count_ratio: float = 0.5,
    ) -> None:
        self.min_cell_px = min_cell_px
        self.max_cell_px = max_cell_px
        self.min_cells_per_row = min_cells_per_row
        self.min_ink_fraction = min_ink_fraction
        self.orange_fraction_delimiter = orange_fraction_delimiter
        self.upscale = upscale
        self.pad_px = pad_px
        self.min_char_confidence = min_char_confidence
        self.det_fallback = det_fallback
        self.min_row_cell_count_ratio = min_row_cell_count_ratio

    # ------------------------------------------------------------------ public

    def detect(self, loaded_page: LoadedPageImage) -> List[CandidateCharacterGrid]:
        image = loaded_page.image_bgr
        if image is None or image.size == 0:
            return []
        try:
            cells = self.find_cells(image)
            rows = self.group_rows(cells)
            grids: List[CandidateCharacterGrid] = []
            for r_idx, row in enumerate(rows):
                for s_idx, observations in enumerate(self._ocr_row(image, row, row_index=r_idx)):
                    grids.append(
                        CandidateCharacterGrid(
                            grid_id=f"grid_p{loaded_page.page_number}_r{r_idx:02d}_s{s_idx:02d}",
                            observations=observations,
                        )
                    )
            logger.info(
                "Box grid detection: page=%s cells=%d rows=%d grids_with_ink=%d",
                loaded_page.page_number, len(cells), len(rows), len(grids),
            )
            return grids
        except Exception as exc:  # fail soft: whole-page OCR still runs
            logger.warning("Box grid detection failed on page %s: %s", loaded_page.page_number, exc)
            return []

    # ------------------------------------------------------------ cell finding

    def find_cells(self, image_bgr: np.ndarray) -> List[DetectedCell]:
        import cv2

        gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
        # Ruling lines are dark and straight; threshold so dark pixels become 255.
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        h_img, w_img = binary.shape[:2]
        k = max(12, int(min(h_img, w_img) * 0.02))
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1))
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, k))
        h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, h_kernel)
        v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, v_kernel)
        lines = cv2.bitwise_or(h_lines, v_lines)

        # Cells are the holes enclosed by the ruling lines.
        inverted = cv2.bitwise_not(lines)
        contours, _ = cv2.findContours(inverted, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

        cells: List[DetectedCell] = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if not (self.min_cell_px <= w <= self.max_cell_px and self.min_cell_px <= h <= self.max_cell_px):
                continue
            aspect = w / float(h)
            if not (0.5 <= aspect <= 2.0):
                continue
            # A true cell is a rectangle: its contour fills most of its bbox.
            if cv2.contourArea(cnt) < 0.6 * w * h:
                continue
            cells.append(DetectedCell(x0=x, y0=y, x1=x + w, y1=y + h))

        # Drop duplicates / nested rectangles (keep the smallest enclosing-free ones).
        return self._dedupe(cells)

    @staticmethod
    def _dedupe(cells: List[DetectedCell]) -> List[DetectedCell]:
        out: List[DetectedCell] = []
        for c in sorted(cells, key=lambda c: (c.y0, c.x0)):
            if any(abs(c.x0 - o.x0) <= 3 and abs(c.y0 - o.y0) <= 3 for o in out):
                continue
            out.append(c)
        return out

    # ------------------------------------------------------------- row groups

    def group_rows(self, cells: List[DetectedCell]) -> List[List[DetectedCell]]:
        """Cluster cells into horizontal rows; keep only rows that look like grids."""
        if not cells:
            return []
        ordered = sorted(cells, key=lambda c: c.cy)
        rows: List[List[DetectedCell]] = []
        for c in ordered:
            placed = False
            for row in rows:
                ref_h = np.median([r.h for r in row])
                if abs(c.cy - np.median([r.cy for r in row])) <= ref_h * 0.5:
                    row.append(c)
                    placed = True
                    break
            if not placed:
                rows.append([c])

        grids: List[List[DetectedCell]] = []
        for row in rows:
            if len(row) < self.min_cells_per_row:
                continue
            row.sort(key=lambda c: c.x0)
            # Grids are regular: keep cells of similar size as the row's median height.
            med_h = float(np.median([c.h for c in row]))
            regular = [c for c in row if abs(c.h - med_h) <= 0.3 * med_h]
            if len(regular) >= self.min_cells_per_row:
                grids.append(regular)
        return grids

    # --------------------------------------------------------------- cell OCR

    def _ocr_row(self, image_bgr: np.ndarray, row: List[DetectedCell], row_index: int) -> List[List[RawCharacterObservation]]:
        """
        OCR one row of cells and split it into field segments.

        Orange cells are printed field separators, and a visual gap wider than
        half a cell also ends a field. Each segment becomes its own grid so that
        DOB, PAN and customer-ID boxes printed on one line are not merged.
        """
        import cv2

        cell_w = float(np.median([c.w for c in row]))
        segments: List[List[RawCharacterObservation]] = []
        current: List[RawCharacterObservation] = []
        ocr = None
        prev: DetectedCell = None  # type: ignore[assignment]
        last_ink_col = -10

        def close_segment():
            nonlocal current
            while current and current[-1].char == WORD_GAP:
                current.pop()
            if current:
                segments.append(current)
            current = []

        for col_idx, cell in enumerate(row):
            if prev is not None and (cell.x0 - prev.x1) > 0.5 * cell_w:
                close_segment()
            prev = cell

            # Two or more blank boxes between inked boxes mark a word gap ("MR   SHANE").
            if current and col_idx - last_ink_col >= 3:
                current.append(
                    RawCharacterObservation(
                        char=WORD_GAP,
                        bbox_preprocessed=(cell.x0, cell.y0, cell.x1, cell.y1),
                        confidence=1.0,
                        row_hint=row_index,
                        col_hint=col_idx,
                    )
                )

            crop = image_bgr[cell.y0:cell.y1, cell.x0:cell.x1]
            if crop.size == 0:
                continue
            # Inset by 3 px so the ruling lines are not read as ink.
            inner = crop[3:-3, 3:-3] if crop.shape[0] > 8 and crop.shape[1] > 8 else crop
            if inner.size == 0:
                continue

            if self._is_orange(inner):
                close_segment()
                continue

            gray = cv2.cvtColor(inner, cv2.COLOR_BGR2GRAY)
            ink = float(np.mean(gray < 120))
            if ink < self.min_ink_fraction:
                continue  # blank box

            if ocr is None:
                ocr = _get_ocr()
            text, score = self._ocr_cell(ocr, inner)
            if not text:
                continue
            last_ink_col = col_idx
            current.append(
                RawCharacterObservation(
                    char=text,
                    bbox_preprocessed=(cell.x0, cell.y0, cell.x1, cell.y1),
                    confidence=score,
                    row_hint=row_index,
                    col_hint=col_idx,
                )
            )
        close_segment()
        return segments

    def _ocr_cell(self, ocr, inner_bgr: np.ndarray) -> Tuple[str, float]:
        import cv2

        scaled = cv2.resize(
            inner_bgr, None, fx=self.upscale, fy=self.upscale, interpolation=cv2.INTER_CUBIC
        )
        padded = cv2.copyMakeBorder(
            scaled, self.pad_px, self.pad_px, self.pad_px, self.pad_px,
            cv2.BORDER_CONSTANT, value=(255, 255, 255),
        )
        # Recognise the single glyph directly (rec-only returns [text, score]);
        # if that yields nothing, fall back to full detect+recognise (returns [box, text, score]).
        best_text, best_score = self._best_item(ocr, padded, use_det=False)
        if self.det_fallback and best_score < 0.8:  # weak glyph read: also try detect+recognise and keep the better one
            alt_text, alt_score = self._best_item(ocr, padded, use_det=True)
            if alt_score > best_score:
                best_text, best_score = alt_text, alt_score
        # Ruling lines and shading produce punctuation and CJK noise in blank boxes;
        # a form character is ASCII alphanumeric, so anything else is dropped.
        if best_score < self.min_char_confidence or not (best_text.isascii() and best_text.isalnum()):
            return "", 0.0
        return best_text, best_score

    def _best_item(self, ocr, image_bgr: np.ndarray, use_det: bool) -> Tuple[str, float]:
        with _OCR_LOCK:
            if use_det:
                result, _ = ocr(image_bgr)
            else:
                result, _ = ocr(image_bgr, use_det=False, use_cls=False, use_rec=True)
        best_text, best_score = "", 0.0
        for item in result or []:
            text, score = (item[-2], item[-1]) if len(item) >= 3 else (item[0], item[1])
            if float(score) > best_score:
                best_text, best_score = str(text).strip(), float(score)
        return best_text, best_score

    @staticmethod
    def _is_orange(bgr: np.ndarray) -> bool:
        import cv2

        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, (5, 120, 120), (22, 255, 255))
        return float(np.mean(mask > 0)) >= BoxGridDetector._ORANGE_FRACTION

    _ORANGE_FRACTION = 0.25
