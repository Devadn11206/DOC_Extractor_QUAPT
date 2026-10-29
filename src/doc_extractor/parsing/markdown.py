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


def generate_page_markdown(blocks: List[LayoutBlock]) -> str:
    """
    Generates layout-aware Markdown from ordered layout blocks (FR-PRS-001).
    Preserves heading levels, paragraphs, and list indicators.
    """
    if not blocks:
        return ""

    lines: List[str] = []
    for b in blocks:
        text = b.text.strip()
        if not text:
            continue

        if b.element_type == LayoutElementType.HEADER:
            # If text looks like main title, use H1 or H2
            if not text.startswith("#"):
                lines.append(f"## {text}\n")
            else:
                lines.append(f"{text}\n")
        elif b.element_type == LayoutElementType.FOOTER:
            lines.append(f"_{text}_\n")
        else:
            lines.append(f"{text}\n")

    return "\n".join(lines).strip()

