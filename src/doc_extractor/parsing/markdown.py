"""
Layout and Markdown Reconstruction Utilities for OCR Parsers.

Assembles recognized OCR blocks into reading order and generates
layout-faithful Markdown (FR-PRS-001).
"""

from typing import List, Tuple
from doc_extractor.contracts_b import BoundingBox, LayoutBlock, LayoutElementType


def sort_blocks_reading_order(
    blocks: List[LayoutBlock],
    line_y_tolerance_px: float = 12.0,
) -> List[LayoutBlock]:
    """
    Sorts blocks according to natural top-to-bottom, left-to-right reading order.
    Groups elements into vertical lines within line_y_tolerance_px, then sorts horizontally.
    """
    if not blocks:
        return []

    # Sort primarily by y0, secondary by x0
    sorted_by_y = sorted(blocks, key=lambda b: (b.bbox_preprocessed.y0, b.bbox_preprocessed.x0))

    lines: List[List[LayoutBlock]] = []
    current_line: List[LayoutBlock] = []
    current_line_y = -1.0

    for block in sorted_by_y:
        y0 = block.bbox_preprocessed.y0
        if current_line_y < 0:
            current_line = [block]
            current_line_y = y0
        elif abs(y0 - current_line_y) <= line_y_tolerance_px:
            current_line.append(block)
        else:
            # Finish line: sort horizontally
            current_line.sort(key=lambda b: b.bbox_preprocessed.x0)
            lines.append(current_line)
            current_line = [block]
            current_line_y = y0

    if current_line:
        current_line.sort(key=lambda b: b.bbox_preprocessed.x0)
        lines.append(current_line)

    reordered: List[LayoutBlock] = []
    idx = 0
    for line in lines:
        for b in line:
            # Reconstruct with sequential reading order index
            reordered.append(
                LayoutBlock(
                    block_id=b.block_id,
                    element_type=b.element_type,
                    text=b.text,
                    ocr_confidence=b.ocr_confidence,
                    bbox_preprocessed=b.bbox_preprocessed,
                    bbox_original=b.bbox_original,
                    reading_order_index=idx,
                )
            )
            idx += 1

    return reordered


def generate_page_markdown(blocks: List[LayoutBlock], line_y_tolerance_px: float = 14.0) -> str:
    """
    Generates layout-aware Markdown from ordered layout blocks (FR-PRS-001).
    Merges horizontal blocks belonging to the same visual line (e.g. character grids, key-values).
    """
    if not blocks:
        return ""

    sorted_by_y = sorted(blocks, key=lambda b: (b.bbox_preprocessed.y0, b.bbox_preprocessed.x0))
    lines: List[List[LayoutBlock]] = []
    curr_line: List[LayoutBlock] = []
    curr_y = -1.0

    for b in sorted_by_y:
        y0 = b.bbox_preprocessed.y0
        if curr_y < 0 or abs(y0 - curr_y) <= line_y_tolerance_px:
            curr_line.append(b)
            curr_y = y0
        else:
            curr_line.sort(key=lambda item: item.bbox_preprocessed.x0)
            lines.append(curr_line)
            curr_line = [b]
            curr_y = y0

    if curr_line:
        curr_line.sort(key=lambda item: item.bbox_preprocessed.x0)
        lines.append(curr_line)

    out_paragraphs: List[str] = []
    for line in lines:
        parts: List[str] = []
        is_header = False
        for idx, b in enumerate(line):
            t = b.text.strip()
            if not t:
                continue
            if b.element_type == LayoutElementType.HEADER:
                is_header = True
            if idx > 0:
                prev_b = line[idx - 1]
                gap = b.bbox_preprocessed.x0 - prev_b.bbox_preprocessed.x1
                if gap > 20:
                    parts.append(" ")
                elif gap > 6 and (len(prev_b.text) > 1 or len(t) > 1):
                    parts.append(" ")
            parts.append(t)

        line_str = "".join(parts).strip()
        if not line_str:
            continue

        if is_header and not line_str.startswith("#"):
            out_paragraphs.append(f"## {line_str}")
        else:
            out_paragraphs.append(line_str)

    return "\n\n".join(out_paragraphs).strip()
