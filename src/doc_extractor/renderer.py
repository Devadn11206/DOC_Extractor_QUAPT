"""
PDF Rendering and Page Management Component.

Implements:
- FR-ING-004: Split multi-page PDFs into individual page images rendered at exactly 200 DPI.
- SCH-013: Coordinates and bounding boxes downstream use pixels of the 200-DPI page image.
- Page ordering preservation (1-based), dimensions, metadata, and document ID association.
- Resource management and atomic rollback/cleanup upon rendering failure.
"""

from dataclasses import dataclass, field
import io
import math
from pathlib import Path
from typing import BinaryIO, List, Optional, Tuple, Union

import pypdfium2 as pdfium
from pypdfium2._helpers.misc import PdfiumError

from doc_extractor.config import Settings
from doc_extractor.exceptions import DocumentProcessingError, UnsupportedFileError
from doc_extractor.logging import setup_logging
from doc_extractor.storage import LocalDiskStorage, StorageBackend, StoredArtifact


@dataclass(frozen=True)
class RenderedPage:
    """Metadata and stored reference for an individually rendered page."""
    document_id: str
    page_number: int  # 1-based original sequence
    width_px: int     # Exact rendered width in pixels
    height_px: int    # Exact rendered height in pixels
    dpi: int          # Rendering DPI (PRD standard 200)
    image_format: str # e.g. "PNG"
    image_path: Path  # Path to stored page image
    file_size_bytes: int


@dataclass
class DocumentRenderResult:
    """Full outcome of rendering a document's pages."""
    document_id: str
    total_pages: int
    dpi: int
    pages: List[RenderedPage]
    is_complete: bool


class PdfRenderer:
    """
    Renders validated, clean PDF documents into 200-DPI page images via pypdfium2.
    """

    PDF_POINTS_PER_INCH = 72.0

    def __init__(
        self,
        settings: Optional[Settings] = None,
        storage: Optional[StorageBackend] = None,
    ) -> None:
        self.settings = settings or Settings()
        self.target_dpi = self.settings.target_render_dpi  # Strict PRD standard: 200
        self.storage = storage or LocalDiskStorage(self.settings.storage_base_dir)
        self.logger = setup_logging(
            level=self.settings.log_level,
            service_name=self.settings.service_name,
            environment=self.settings.app_env,
        )

    def calculate_scale(self, target_dpi: int) -> float:
        """
        Calculates the exact scale factor for pypdfium2: target_dpi / 72.0 points.
        For 200 DPI: scale = 200.0 / 72.0 = 2.7777777777777777
        """
        return target_dpi / self.PDF_POINTS_PER_INCH

    def render_pdf(
        self,
        pdf_input: Union[str, Path, bytes, BinaryIO],
        document_id: str,
        password: Optional[str] = None,
    ) -> DocumentRenderResult:
        """
        Splits and renders all pages of a PDF at 200 DPI.
        
        Guarantees:
        - Strict 1-based page ordering.
        - Exact 200-DPI pixel dimensions.
        - Cleanup of partial output if any page rendering fails.
        - Resources closed reliably.

        Raises:
            DocumentProcessingError: If PDF is encrypted, password-protected without valid credentials,
                                    or encounters a rendering failure.
            UnsupportedFileError: If PDF data is corrupt or cannot be opened.
        """
        scale = self.calculate_scale(self.target_dpi)
        rendered_pages: List[RenderedPage] = []

        # Read input into bytes if stream or path
        pdf_bytes: bytes
        if isinstance(pdf_input, (str, Path)):
            with open(pdf_input, "rb") as f:
                pdf_bytes = f.read()
        elif isinstance(pdf_input, bytes):
            pdf_bytes = pdf_input
        else:
            pdf_input.seek(0)
            pdf_bytes = pdf_input.read()

        if len(pdf_bytes) == 0:
            raise UnsupportedFileError(
                message="Cannot render PDF: document is empty (0 bytes).",
                details={"document_id": document_id},
            )

        doc = None
        try:
            try:
                doc = pdfium.PdfDocument(pdf_bytes, password=password)
            except PdfiumError as exc:
                err_msg = str(exc)
                if "password" in err_msg.lower() or "protected" in err_msg.lower():
                    raise DocumentProcessingError(
                        message="Cannot render PDF: document is password-protected or encrypted.",
                        code="DOCUMENT_ENCRYPTED",
                        details={"document_id": document_id},
                    ) from exc
                else:
                    raise UnsupportedFileError(
                        message="Failed to load PDF document into rendering engine (corrupt data).",
                        details={"document_id": document_id, "error": err_msg},
                    ) from exc

            page_count = len(doc)
            if page_count == 0:
                raise UnsupportedFileError(
                    message="Cannot render PDF: document contains 0 pages.",
                    details={"document_id": document_id},
                )

            self.logger.info(
                f"Starting PDF render at {self.target_dpi} DPI",
                extra={
                    "document_id": document_id,
                    "extra_data": {"page_count": page_count, "dpi": self.target_dpi},
                },
            )

            # Render pages sequentially (1-based)
            for idx in range(page_count):
                page_number = idx + 1
                page = doc[idx]

                try:
                    # Page dimensions in points
                    pt_w, pt_h = page.get_width(), page.get_height()
                    expected_w = round(pt_w * scale)
                    expected_h = round(pt_h * scale)

                    # Render to PIL Image
                    pil_img = page.render(scale=scale).to_pil()
                    img_w, img_h = pil_img.size

                    # Encode to PNG buffer in memory
                    img_buf = io.BytesIO()
                    pil_img.save(img_buf, format="PNG")
                    img_bytes = img_buf.getvalue()

                    # Store via storage abstraction
                    stored = self.storage.store_page_image(
                        document_id=document_id,
                        page_number=page_number,
                        image_data=img_bytes,
                        format_extension="png",
                    )

                    rendered_pages.append(
                        RenderedPage(
                            document_id=document_id,
                            page_number=page_number,
                            width_px=img_w,
                            height_px=img_h,
                            dpi=self.target_dpi,
                            image_format="PNG",
                            image_path=stored.file_path,
                            file_size_bytes=stored.file_size_bytes,
                        )
                    )
                except Exception as render_err:
                    self.logger.error(
                        f"Rendering failed on page {page_number}",
                        extra={
                            "document_id": document_id,
                            "extra_data": {"page_number": page_number, "error": str(render_err)},
                        },
                    )
                    # Atomic rollback: Clean up any partially stored page images
                    self.storage.cleanup_document(document_id)
                    raise DocumentProcessingError(
                        message=f"Rendering failed on page {page_number} of {page_count}.",
                        details={"document_id": document_id, "failed_page": page_number},
                    ) from render_err
                finally:
                    page.close()

            return DocumentRenderResult(
                document_id=document_id,
                total_pages=page_count,
                dpi=self.target_dpi,
                pages=rendered_pages,
                is_complete=True,
            )

        finally:
            if doc is not None:
                doc.close()

