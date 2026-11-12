"""
Checkbox state detector for printed forms (PRD FR-PRS-002, FR-EXT-007).

Works from the printed label of each checkbox (found by OCR, with its bounding box):
  1. Looks for a square box just to the left of the label.
  2. Inside the box (ignoring its border), measures how much dark ink there is.
  3. Ink above the threshold means the box is ticked or crossed; otherwise it is empty.

Returns True (ticked), False (empty) or None (no box found, or the reading is too close to call).
No model is used, so it is fast and deterministic. Thresholds were set on synthetic images only;
they must be checked on real forms before the result is trusted without review.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# Ink (dark pixel) fraction inside the box interior above which a box counts as ticked.
TICK_INK_THRESHOLD = 0.06
# Readings between EMPTY_INK and TICK_INK_THRESHOLD are too close to call.
EMPTY_INK_THRESHOLD = 0.02
DARK_PIXEL_VALUE = 110


def _norm(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


MAX_LABEL_EXTRA_CHARS = 8  # a checkbox label is the word itself, not a sentence that contains it


def find_label_bbox(tokens: Sequence[dict], label_words: Sequence[str]) -> Optional[Tuple[int, int, int, int]]:
    """
    First short OCR token that is one of the label words (e.g. "Savings", "Cheque book").
    Long text that merely contains the word (e.g. "Please open a Savings Bank account...") is ignored.
    """
    for tok in tokens:
        t = _norm(tok.get("text", ""))
        if any(w in t and len(t) <= len(w) + MAX_LABEL_EXTRA_CHARS for w in label_words):
            b = tok.get("bbox")
            if b and len(b) == 4:
                return int(b[0]), int(b[1]), int(b[2]), int(b[3])
    return None


def _ink_fraction(gray: np.ndarray) -> float:
    if gray.size == 0:
        return 0.0
    return float(np.mean(gray < DARK_PIXEL_VALUE))


def read_box_left_of_label(image_bgr: np.ndarray, label_bbox: Tuple[int, int, int, int]) -> Optional[float]:
    """Ink fraction inside the box just left of the label, or None if no box is found there."""
    import cv2

    x0, y0, x1, y1 = label_bbox
    h = max(1, y1 - y0)
    # Search window to the left of the label, same height band as the label.
    wx0 = max(0, int(x0 - 2.6 * h))
    wx1 = max(0, int(x0 - 0.1 * h))
    wy0 = max(0, int(y0 - 0.25 * h))
    wy1 = min(image_bgr.shape[0], int(y1 + 0.25 * h))
    if wx1 <= wx0 or wy1 <= wy0:
        return None
    window = image_bgr[wy0:wy1, wx0:wx1]
    gray = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    for cnt in contours:
        bx, by, bw, bh = cv2.boundingRect(cnt)
        if not (0.6 * h <= bw <= 1.6 * h and 0.6 * h <= bh <= 1.6 * h):
            continue
        aspect = bw / float(bh)
        if not (0.7 <= aspect <= 1.4):
            continue
        # The box is the square closest to the label (rightmost square in the window).
        if best is None or bx > best[0]:
            best = (bx, by, bw, bh)
    if best is None:
        return None

    bx, by, bw, bh = best
    inset = max(2, int(0.2 * min(bw, bh)))
    inner = gray[by + inset: by + bh - inset, bx + inset: bx + bw - inset]
    return _ink_fraction(inner)


def read_checkbox(image_bgr: np.ndarray, tokens: Sequence[dict], label_words: Sequence[str]) -> Optional[bool]:
    """True if ticked, False if empty, None if the box cannot be found or the reading is unclear."""
    bbox = find_label_bbox(tokens, label_words)
    if bbox is None:
        return None
    ink = read_box_left_of_label(image_bgr, bbox)
    if ink is None:
        return None
    if ink >= TICK_INK_THRESHOLD:
        return True
    if ink <= EMPTY_INK_THRESHOLD:
        return False
    return None


def detect_checkboxes(
    image_bgr: np.ndarray,
    tokens: Sequence[dict],
    label_map: Dict[str, List[str]],
) -> Dict[str, Optional[bool]]:
    """label_map: field name -> list of label words (uppercase, letters and digits only)."""
    return {field: read_checkbox(image_bgr, tokens, words) for field, words in label_map.items()}
