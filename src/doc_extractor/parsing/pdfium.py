"""Native PDF text extraction with page-level OCR fallback."""

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

import pypdfium2 as pdfium

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
)
from doc_extractor.parsing.markdown import generate_page_markdown


class PdfiumTextParserAdapter(ParserEngine):
    """Extract embedded PDF text with geometry-aware layout blocks."""

    def get_metadata(self) -> ParserEngineMetadata:
        try:
            pdfium_version = version("pypdfium2")
        except PackageNotFoundError:
            pdfium_version = "unknown"

        return ParserEngineMetadata(
            engine_name="PDFium-Native-Text",
            engine_version=pdfium_version,
            is_operational=self.is_operational(),
            supports_optical_confidence=False,
            description="Native PDF text extraction; confidence is not optical OCR confidence.",
        )

    def is_operational(self) -> bool:
        return pdfium is not None

    def parse_page(self, loaded_page: LoadedPageImage) -> PageLayoutOutput:
        source_path = loaded_page.source_pdf_path

        if source_path is None:
            raise ParserEngineError(
                message="Original PDF is unavailable for native text extraction.",
                engine_name="PDFium-Native-Text",
                code="SOURCE_PDF_UNAVAILABLE",
                details={"page_number": loaded_page.page_number},
            )

        if not source_path.is_file():
            raise ParserEngineError(
                message="Original PDF source file does not exist.",
                engine_name="PDFium-Native-Text",
                code="SOURCE_PDF_MISSING",
                details={"page_number": loaded_page.page_number},
            )

        page_ref = loaded_page.page_ref

        # Native boxes map directly only when the source page geometry has
        # not been rotated or deskewed by the ingestion pipeline.
        if page_ref.applied_rotation_deg != 0 or abs(page_ref.applied_deskew_deg) > 1e-4:
            raise ParserEngineError(
                message="Native PDF coordinates are incompatible with transformed page geometry.",
                engine_name="PDFium-Native-Text",
                code="UNSUPPORTED_PDF_GEOMETRY",
                details={"page_number": loaded_page.page_number},
            )

        document = None
        page = None
        text_page = None

        try:
            document = pdfium.PdfDocument(str(source_path))
            page_index = loaded_page.page_number - 1

            if page_index < 0 or page_index >= len(document):
                raise ParserEngineError(
                    message="Page number is outside the source PDF.",
                    engine_name="PDFium-Native-Text",
                    code="SOURCE_PAGE_OUT_OF_RANGE",
                    details={"page_number": loaded_page.page_number},
                )

            page = document[page_index]

            if page.get_rotation() != 0:
                raise ParserEngineError(
                    message="Rotated PDF pages require visual OCR.",
                    engine_name="PDFium-Native-Text",
                    code="UNSUPPORTED_PDF_GEOMETRY",
                    details={"page_number": loaded_page.page_number},
                )

            width_pt, height_pt = page.get_size()
            scale = float(loaded_page.dpi) / 72.0

            expected_width = width_pt * scale
            expected_height = height_pt * scale

            # Avoid applying guessed geometry to an unexpectedly transformed page.
            if (
                abs(expected_width - page_ref.original_width_px) > 3
                or abs(expected_height - page_ref.original_height_px) > 3
            ):
                raise ParserEngineError(
                    message="PDF page dimensions do not align with Module A rendered dimensions.",
                    engine_name="PDFium-Native-Text",
                    code="UNSUPPORTED_PDF_GEOMETRY",
                    details={"page_number": loaded_page.page_number},
                )

            text_page = page.get_textpage()
            text = text_page.get_text_range()
            char_count = text_page.count_chars()

            if char_count != len(text):
                raise ParserEngineError(
                    message="PDFium character indices do not align with extracted text.",
                    engine_name="PDFium-Native-Text",
                    code="PDFIUM_TEXT_INDEX_MISMATCH",
                    details={"page_number": loaded_page.page_number},
                )

            meaningful_chars = sum(not char.isspace() for char in text)
            if meaningful_chars < 8:
                raise ParserEngineError(
                    message="Page has insufficient embedded text for native extraction.",
                    engine_name="PDFium-Native-Text",
                    code="NO_USABLE_EMBEDDED_TEXT",
                    details={"page_number": loaded_page.page_number},
                )

            blocks = []
            offset = 0
            covered_chars = 0
            line_index = 0

            for raw_line in text.splitlines(keepends=True):
                line = raw_line.rstrip("\r\n")
                line_start = offset
                offset += len(raw_line)

                clean_text = line.strip()
                if not clean_text:
                    continue

                # Keep PDFium's extracted line text intact, while measuring
                # the enclosing box from the non-whitespace characters.
                char_indices = [
                    line_start + index
                    for index, char in enumerate(line)
                    if not char.isspace()
                ]

                boxes = []
                for char_index in char_indices:
                    box = text_page.get_charbox(char_index)
                    if box is not None:
                        boxes.append(box)

                if not boxes:
                    continue

                covered_chars += len(boxes)

                # PDFium character boxes use bottom-left PDF coordinates:
                # (left, bottom, right, top). Convert to top-left pixels.
                x0 = min(box[0] for box in boxes) * scale
                y0 = (height_pt - max(box[3] for box in boxes)) * scale
                x1 = max(box[2] for box in boxes) * scale
                y1 = (height_pt - min(box[1] for box in boxes)) * scale

                x0 = max(0.0, min(float(page_ref.original_width_px), x0))
                x1 = max(0.0, min(float(page_ref.original_width_px), x1))
                y0 = max(0.0, min(float(page_ref.original_height_px), y0))
                y1 = max(0.0, min(float(page_ref.original_height_px), y1))

                if x1 <= x0 or y1 <= y0:
                    continue

                bbox_original = BoundingBox(
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                )

                # Native PDF coordinates are in original-page space. Map to
                # preprocessed dimensions for the existing layout contract.
                sx = page_ref.preprocessed_width_px / page_ref.original_width_px
                sy = page_ref.preprocessed_height_px / page_ref.original_height_px

                bbox_preprocessed = BoundingBox(
                    x0=x0 * sx,
                    y0=y0 * sy,
                    x1=x1 * sx,
                    y1=y1 * sy,
                )

                element_type = (
                    LayoutElementType.HEADER
                    if clean_text.isupper() and len(clean_text) > 4
                    else LayoutElementType.PARAGRAPH
                )

                blocks.append(
                    LayoutBlock(
                        block_id=f"pdf_p{loaded_page.page_number}_{line_index:04d}",
                        element_type=element_type,
                        text=clean_text,
                        # Native PDF text has no optical OCR confidence; explicitly unavailable.
                        ocr_confidence=None,
                        confidence_provenance="native_text",
                        bbox_preprocessed=bbox_preprocessed,
                        bbox_original=bbox_original,
                        reading_order_index=line_index,
                    )
                )
                line_index += 1

            if meaningful_chars and covered_chars / meaningful_chars < 0.80:
                raise ParserEngineError(
                    message="Too few embedded text characters have usable geometry.",
                    engine_name="PDFium-Native-Text",
                    code="NO_USABLE_EMBEDDED_TEXT",
                    details={"page_number": loaded_page.page_number},
                )

            # Preserve PDFium's native extraction order. Its text stream
            # retains logical grouping better than a global spatial sort on
            # multi-column brochure pages.
            ordered_blocks = blocks
            markdown = generate_page_markdown(ordered_blocks)

            valid_confs = [b.ocr_confidence for b in ordered_blocks if b.ocr_confidence is not None]
            mean_confidence = (
                round(sum(valid_confs) / len(valid_confs), 4)
                if valid_confs
                else None
            )

            return PageLayoutOutput(
                page_number=loaded_page.page_number,
                markdown_content=markdown,
                blocks=ordered_blocks,
                mean_ocr_confidence=mean_confidence,
                confidence_provenance="native_text",
            )

        except ParserEngineError:
            raise
        except Exception as exc:
            raise ParserEngineError(
                message=f"PDFium text extraction failed on page {loaded_page.page_number}.",
                engine_name="PDFium-Native-Text",
                code="PDFIUM_TEXT_EXTRACTION_FAILED",
                details={"page_number": loaded_page.page_number},
            ) from exc
        finally:
            if text_page is not None:
                text_page.close()
            if page is not None:
                page.close()
            if document is not None:
                document.close()


class HybridPdfParserAdapter(ParserEngine):
    """Prefer native PDF text; fall back to OCR for unsupported or textless pages."""

    def __init__(self, fallback_parser: ParserEngine) -> None:
        self.native_parser = PdfiumTextParserAdapter()
        self.fallback_parser = fallback_parser

    def get_metadata(self) -> ParserEngineMetadata:
        fallback = self.fallback_parser.get_metadata()
        return ParserEngineMetadata(
            engine_name="PDFium+OCR-Hybrid",
            engine_version="1.0.0",
            is_operational=self.is_operational(),
            supports_optical_confidence=fallback.supports_optical_confidence,
            description=(
                "Native PDF text is preferred per page; configured OCR handles "
                "pages without usable embedded text or with unsupported geometry."
            ),
        )

    def is_operational(self) -> bool:
        return self.native_parser.is_operational() or self.fallback_parser.is_operational()

    def parse_page(self, loaded_page: LoadedPageImage) -> PageLayoutOutput:
        if loaded_page.source_pdf_path is None:
            return self.fallback_parser.parse_page(loaded_page)

        try:
            return self.native_parser.parse_page(loaded_page)
        except ParserEngineError as exc:
            fallback_codes = {
                "NO_USABLE_EMBEDDED_TEXT",
                "UNSUPPORTED_PDF_GEOMETRY",
            }
            if exc.code not in fallback_codes:
                raise

            return self.fallback_parser.parse_page(loaded_page)
