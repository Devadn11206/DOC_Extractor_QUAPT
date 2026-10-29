"""
Local OCR Adapter and Reference Parser Engine PoC.

Implements ParserEngine interface (ARC-002) using pluggable backend:
- Supports mock/synthetic backends for deterministic testing.
- Supports pluggable local engines when installed (e.g. Tesseract, PaddleOCR).
- Inverts bounding boxes via CoordinateTransform (SCH-013).
- Generates layout-faithful Markdown and records optical confidence (FR-PRS-001, FR-PRS-005).
"""

from typing import Callable, List, Optional
import numpy as np

from doc_extractor.contracts_b import (
    BoundingBox,
    LayoutBlock,
    LayoutElementType,
    PageLayoutOutput,
)
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.base import (
    ParserEngine,
    ParserEngineError,
    ParserEngineMetadata,
    RawOCRToken,
)
from doc_extractor.parsing.markdown import generate_page_markdown, sort_blocks_reading_order


class LocalOCRParserAdapter(ParserEngine):
    """
    Standard OCR layout parser adapter fulfilling FR-PRS-001 and FR-PRS-005.
    
    Accepts a callable extractor or local backend driver returning List[RawOCRToken].
    Processes tokens into sorted layout blocks, maps coordinates to original 200 DPI space,
    and formats page Markdown.
    """

    def __init__(
        self,
        engine_name: str = "LocalOCR-PoC",
        engine_version: str = "1.0.0",
        raw_extractor: Optional[Callable[[np.ndarray], List[RawOCRToken]]] = None,
        is_operational: bool = True,
        default_confidence_when_missing: float = 0.5,
    ) -> None:
        self.engine_name = engine_name
        self.engine_version = engine_version
        self._raw_extractor = raw_extractor
        self._is_operational = is_operational
        self.default_confidence_when_missing = default_confidence_when_missing

    def get_metadata(self) -> ParserEngineMetadata:
        return ParserEngineMetadata(
            engine_name=self.engine_name,
            engine_version=self.engine_version,
            is_operational=self._is_operational,
            supports_optical_confidence=True,
            description="Local OCR and layout parser adapter with coordinate inversion.",
        )

    def is_operational(self) -> bool:
        return self._is_operational

    def parse_page(self, loaded_page: LoadedPageImage) -> PageLayoutOutput:
        """
        Executes OCR on loaded_page.image_bgr, maps coordinates back to original 200 DPI space,
        and constructs PageLayoutOutput.
        """
        if not self.is_operational():
            raise ParserEngineError(
                message=f"Parser engine '{self.engine_name}' is not operational.",
                engine_name=self.engine_name,
                code="PARSER_UNAVAILABLE",
            )

        if loaded_page.image_bgr is None or loaded_page.image_bgr.size == 0:
            raise ParserEngineError(
                message=f"Cannot parse page {loaded_page.page_number}: image buffer is empty.",
                engine_name=self.engine_name,
                code="EMPTY_IMAGE_BUFFER",
            )

        # 1. Extract raw OCR tokens using configured local extractor
        try:
            if self._raw_extractor is not None:
                raw_tokens = self._raw_extractor(loaded_page.image_bgr)
            else:
                # Default: empty token set (e.g. blank page or placeholder)
                raw_tokens = []
        except Exception as exc:
            raise ParserEngineError(
                message=f"OCR engine extraction failed on page {loaded_page.page_number}: {str(exc)}",
                engine_name=self.engine_name,
                code="OCR_EXTRACTION_FAILED",
                details={"page_number": loaded_page.page_number},
            ) from exc

        # 2. Build layout blocks with SCH-013 coordinate inversion
        page_ref = loaded_page.page_ref
        blocks: List[LayoutBlock] = []

        for idx, token in enumerate(raw_tokens):
            text = (token.text or "").strip()
            if not text:
                continue

            # Ensure preprocessed bounding box is valid
            try:
                bbox_prep = BoundingBox(
                    x0=token.bbox_preprocessed[0],
                    y0=token.bbox_preprocessed[1],
                    x1=token.bbox_preprocessed[2],
                    y1=token.bbox_preprocessed[3],
                )
            except Exception as b_err:
                raise ParserEngineError(
                    message=f"Malformed bounding box from OCR engine on page {loaded_page.page_number}: {str(b_err)}",
                    engine_name=self.engine_name,
                    code="MALFORMED_BOUNDING_BOX",
                    details={"token_text": text, "raw_bbox": token.bbox_preprocessed},
                ) from b_err

            # Invert coordinates to original 200-DPI top-left space (SCH-013)
            try:
                mapped_tuple = CoordinateTransform.map_bbox_preprocessed_to_original(
                    bbox=bbox_prep.to_tuple(),
                    original_width=page_ref.original_width_px,
                    original_height=page_ref.original_height_px,
                    applied_rotation_deg=page_ref.applied_rotation_deg,
                    applied_deskew_deg=page_ref.applied_deskew_deg,
                )
                bbox_orig = BoundingBox.from_tuple(mapped_tuple)
            except Exception as map_err:
                raise ParserEngineError(
                    message=f"Coordinate inversion failed on page {loaded_page.page_number}: {str(map_err)}",
                    engine_name=self.engine_name,
                    code="COORDINATE_INVERSION_ERROR",
                    details={"bbox_prep": bbox_prep.to_tuple()},
                ) from map_err

            # Calibrate optical confidence (FR-PRS-005)
            if token.confidence is not None:
                conf = max(0.0, min(1.0, float(token.confidence)))
            else:
                conf = self.default_confidence_when_missing

            # Categorize basic element type based on text formatting
            if text.isupper() and len(text) > 4:
                elem_type = LayoutElementType.HEADER
            else:
                elem_type = LayoutElementType.PARAGRAPH

            blocks.append(
                LayoutBlock(
                    block_id=f"blk_p{loaded_page.page_number}_{idx:04d}",
                    element_type=elem_type,
                    text=text,
                    ocr_confidence=conf,
                    bbox_preprocessed=bbox_prep,
                    bbox_original=bbox_orig,
                    reading_order_index=idx,
                )
            )

        # 3. Sort blocks by reading order and generate Markdown
        ordered_blocks = sort_blocks_reading_order(blocks)
        markdown = generate_page_markdown(ordered_blocks)

        mean_conf = (
            sum(b.ocr_confidence for b in ordered_blocks) / len(ordered_blocks)
            if ordered_blocks
            else 1.0
        )

        return PageLayoutOutput(
            page_number=loaded_page.page_number,
            markdown_content=markdown,
            blocks=ordered_blocks,
            checkboxes=[],
            key_value_pairs=[],
            tables=[],
            mean_ocr_confidence=round(mean_conf, 4),
        )

