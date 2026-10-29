"""
Table Structure Reconstruction for Module B Layout Parsing.

Implements PRD requirements:
- FR-PRS-001: Layout-faithful Markdown with HTML tables (<table><tr><td>...</td></tr></table>).
- FR-PRS-004: Detect tables as typed layout elements.
- SCH-013: 200-DPI top-left pixel space coordinates for bounding boxes.

Independent of specific OCR engines (ARC-002).
"""

from dataclasses import dataclass, field
import html
from typing import Any, Dict, List, Optional, Tuple

from doc_extractor.contracts_b import BoundingBox, HTMLTable
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage


@dataclass(frozen=True)
class CandidateTableCell:
    """A single cell candidate observed by an OCR/layout engine."""
    text: str
    bbox_preprocessed: Tuple[float, float, float, float]
    confidence: Optional[float] = None
    row_hint: Optional[int] = None
    col_hint: Optional[int] = None
    is_header: bool = False
    row_span: int = 1
    col_span: int = 1


@dataclass(frozen=True)
class CandidateTableRegion:
    """
    A detected table region consisting of multiple cell candidates.
    Can be supplied directly by a layout vision model or formed by geometric clustering.
    """
    region_id: str
    cells: List[CandidateTableCell]
    bbox_preprocessed: Optional[Tuple[float, float, float, float]] = None
    is_header_row_present: bool = True


class TableReconstructionError(ValueError):
    """Raised when candidate table geometry is malformed or invalid."""
    pass


class TableReconstructor:
    """
    Reconstructs structured tabular data and generates valid HTML tables.
    
    Guarantees:
    - Preserves row and column order.
    - Generates clean, well-formed HTML (<table>...</table>) with HTML-escaped text.
    - Computes bounding box enclosing all cells, mapped to original 200-DPI space (SCH-013).
    - Preserves confidence scoring.
    - Rejects malformed or degenerate candidate geometry cleanly.
    """

    def __init__(
        self,
        row_y_tolerance_px: float = 12.0,
        col_x_tolerance_px: float = 20.0,
        default_confidence_when_missing: float = 0.5,
    ) -> None:
        self.row_y_tolerance_px = row_y_tolerance_px
        self.col_x_tolerance_px = col_x_tolerance_px
        self.default_confidence_when_missing = default_confidence_when_missing

    def reconstruct_table(
        self,
        candidate_region: CandidateTableRegion,
        loaded_page: LoadedPageImage,
    ) -> HTMLTable:
        """
        Reconstructs a single candidate table region into a validated HTMLTable contract.
        
        Raises:
            TableReconstructionError: If region has 0 cells, malformed bounds, or degenerate geometry.
        """
        if not candidate_region.cells:
            raise TableReconstructionError("Cannot reconstruct table: candidate region contains 0 cells.")

        # 1. Validate cell bounding boxes and compute enclosing preprocessed bounding box
        valid_cells: List[CandidateTableCell] = []
        min_x0 = float("inf")
        min_y0 = float("inf")
        max_x1 = float("-inf")
        max_y1 = float("-inf")

        confidences: List[float] = []

        for cell in candidate_region.cells:
            bx0, by0, bx1, by1 = cell.bbox_preprocessed
            if bx1 < bx0 or by1 < by0 or bx0 < 0 or by0 < 0:
                raise TableReconstructionError(
                    f"Malformed cell bounding box ({bx0}, {by0}, {bx1}, {by1}) in table {candidate_region.region_id}."
                )
            min_x0 = min(min_x0, bx0)
            min_y0 = min(min_y0, by0)
            max_x1 = max(max_x1, bx1)
            max_y1 = max(max_y1, by1)

            c_val = (
                cell.confidence
                if cell.confidence is not None
                else self.default_confidence_when_missing
            )
            confidences.append(max(0.0, min(1.0, float(c_val))))
            valid_cells.append(cell)

        # 2. Cluster cells into rows and columns
        # Check if row_hint and col_hint are explicitly provided
        has_explicit_grid = all(
            c.row_hint is not None and c.col_hint is not None for c in valid_cells
        )

        if has_explicit_grid:
            grid = self._build_explicit_grid(valid_cells)
        else:
            grid = self._cluster_cells_geometrically(valid_cells)

        row_count = len(grid)
        col_count = max(len(row) for row in grid) if grid else 0

        if row_count == 0 or col_count == 0:
            raise TableReconstructionError(
                f"Degenerate table structure in region {candidate_region.region_id}: {row_count} rows, {col_count} columns."
            )

        # 3. Generate HTML representation
        html_content = self._render_html_table(grid, candidate_region.is_header_row_present)

        # 4. Map enclosing bounding box to original 200-DPI space (SCH-013)
        page_ref = loaded_page.page_ref
        enclosing_prep = (min_x0, min_y0, max_x1, max_y1)
        orig_tuple = CoordinateTransform.map_bbox_preprocessed_to_original(
            bbox=enclosing_prep,
            original_width=page_ref.original_width_px,
            original_height=page_ref.original_height_px,
            applied_rotation_deg=page_ref.applied_rotation_deg,
            applied_deskew_deg=page_ref.applied_deskew_deg,
        )
        bbox_orig = BoundingBox.from_tuple(orig_tuple)

        mean_conf = sum(confidences) / len(confidences) if confidences else 1.0

        return HTMLTable(
            table_id=candidate_region.region_id,
            html_content=html_content,
            bbox_original=bbox_orig,
            row_count=row_count,
            col_count=col_count,
            ocr_confidence=round(mean_conf, 4),
        )

    def _build_explicit_grid(
        self,
        cells: List[CandidateTableCell],
    ) -> List[List[CandidateTableCell]]:
        """Constructs grid from cells with pre-assigned row and column indices."""
        max_r = max(c.row_hint for c in cells)  # type: ignore
        max_c = max(c.col_hint for c in cells)  # type: ignore

        # Initialize grid with dummy empty cells
        grid: List[List[Optional[CandidateTableCell]]] = [
            [None for _ in range(max_c + 1)] for _ in range(max_r + 1)
        ]

        for c in cells:
            r = c.row_hint  # type: ignore
            col = c.col_hint  # type: ignore
            grid[r][col] = c

        # Replace any empty cell with an empty CandidateTableCell
        final_grid: List[List[CandidateTableCell]] = []
        for r_idx, row in enumerate(grid):
            clean_row: List[CandidateTableCell] = []
            for c_idx, cell in enumerate(row):
                if cell is not None:
                    clean_row.append(cell)
                else:
                    clean_row.append(
                        CandidateTableCell(
                            text="",
                            bbox_preprocessed=(0.0, 0.0, 0.0, 0.0),
                            confidence=1.0,
                            row_hint=r_idx,
                            col_hint=c_idx,
                        )
                    )
            final_grid.append(clean_row)

        return final_grid

    def _cluster_cells_geometrically(
        self,
        cells: List[CandidateTableCell],
    ) -> List[List[CandidateTableCell]]:
        """Clusters unordered bounding boxes into rows and columns geometrically."""
        # 1. Sort primarily by vertical coordinate y0
        sorted_by_y = sorted(cells, key=lambda c: (c.bbox_preprocessed[1], c.bbox_preprocessed[0]))

        # Group into rows
        raw_rows: List[List[CandidateTableCell]] = []
        curr_row: List[CandidateTableCell] = []
        curr_y_min = -1.0
        curr_y_max = -1.0

        for c in sorted_by_y:
            y0, y1 = c.bbox_preprocessed[1], c.bbox_preprocessed[3]
            if curr_y_min < 0:
                curr_row = [c]
                curr_y_min = y0
                curr_y_max = y1
            elif y0 <= (curr_y_max - 2.0) or abs(y0 - curr_y_min) <= self.row_y_tolerance_px:
                # Cells overlap vertically or start near the current row baseline
                curr_row.append(c)
                curr_y_min = min(curr_y_min, y0)
                curr_y_max = max(curr_y_max, y1)
            else:
                curr_row.sort(key=lambda item: item.bbox_preprocessed[0])
                raw_rows.append(curr_row)
                curr_row = [c]
                curr_y_min = y0
                curr_y_max = y1

        if curr_row:
            curr_row.sort(key=lambda item: item.bbox_preprocessed[0])
            raw_rows.append(curr_row)

        return raw_rows

    def _render_html_table(
        self,
        grid: List[List[CandidateTableCell]],
        has_header_row: bool,
    ) -> str:
        """Renders grid into valid HTML table representation."""
        html_lines = ["<table>"]

        for r_idx, row in enumerate(grid):
            html_lines.append("  <tr>")
            is_th = has_header_row and (r_idx == 0 or all(c.is_header for c in row))
            tag = "th" if is_th else "td"

            for cell in row:
                escaped_text = html.escape(cell.text.strip())
                attrs = ""
                if cell.row_span > 1:
                    attrs += f' rowspan="{cell.row_span}"'
                if cell.col_span > 1:
                    attrs += f' colspan="{cell.col_span}"'

                html_lines.append(f"    <{tag}{attrs}>{escaped_text}</{tag}>")

            html_lines.append("  </tr>")

        html_lines.append("</table>")
        return "\n".join(html_lines)

