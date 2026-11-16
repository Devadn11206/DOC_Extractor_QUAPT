"""
Settings-driven selection of the Module B parser engine (ARC-002, FR-PRS-001, FR-PRS-005).

This module only *composes* existing parser implementations; it adds no OCR logic:

- ``tesseract``     -> LocalOCRParserAdapter.with_tesseract (existing Tesseract integration)
- ``pdfium_hybrid`` -> HybridPdfParserAdapter(fallback=Tesseract adapter) (existing PDFium adapter)
- ``pdfium``        -> PdfiumTextParserAdapter (native PDF text only, no OCR)
- ``none``          -> NoParserEngine, an explicit and clearly non-operational parser

Backends that need a runtime (Tesseract) are verified when the parser is built, so a
missing executable, package or language pack fails immediately with an actionable error
instead of producing empty pages from a parser that claims to be operational.
"""

from typing import Optional

from doc_extractor.config import Settings
from doc_extractor.contracts_b import PageLayoutOutput
from doc_extractor.exceptions import ConfigurationError
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import (
    ParserEngine,
    ParserEngineError,
    ParserEngineMetadata,
)
from doc_extractor.parsing.pdfium import HybridPdfParserAdapter, PdfiumTextParserAdapter

SUPPORTED_OCR_BACKENDS = ("tesseract", "pdfium_hybrid", "pdfium", "none")


class NoParserEngine(ParserEngine):
    """Explicit no-parser mode (``ocr_backend='none'``): never operational, never parses."""

    engine_name = "NoParser"

    def get_metadata(self) -> ParserEngineMetadata:
        return ParserEngineMetadata(
            engine_name=self.engine_name,
            engine_version="n/a",
            is_operational=False,
            supports_optical_confidence=False,
            description="No parser configured (ocr_backend='none'); Module B cannot parse pages.",
        )

    def is_operational(self) -> bool:
        return False

    def parse_page(self, loaded_page: LoadedPageImage) -> PageLayoutOutput:
        raise ParserEngineError(
            message=(
                "No parser is configured (ocr_backend='none'). "
                "Set Settings.ocr_backend to 'tesseract', 'pdfium_hybrid' or 'pdfium' to enable parsing."
            ),
            engine_name=self.engine_name,
            code="PARSER_NOT_CONFIGURED",
            details={"page_number": loaded_page.page_number},
        )


def _build_tesseract_adapter(settings: Settings) -> LocalOCRParserAdapter:
    return LocalOCRParserAdapter.with_tesseract(
        lang=settings.ocr_tesseract_lang,
        config=settings.ocr_tesseract_config,
        tesseract_cmd=settings.ocr_tesseract_cmd,
        verify=True,
    )


def build_parser_engine(settings: Optional[Settings] = None) -> ParserEngine:
    """
    Builds the Module B parser selected by ``settings.ocr_backend`` (default: Tesseract).

    Raises:
        ConfigurationError: if ``ocr_backend`` is not a supported backend.
        ParserEngineError: if the selected backend's runtime is unavailable
            (codes OCR_DEPENDENCY_MISSING, OCR_BINARY_MISSING, OCR_BINARY_UNUSABLE,
            OCR_LANGUAGE_MISSING).
    """
    settings = settings if settings is not None else Settings()
    backend = settings.ocr_backend

    if backend == "tesseract":
        return _build_tesseract_adapter(settings)
    if backend == "pdfium_hybrid":
        return HybridPdfParserAdapter(fallback_parser=_build_tesseract_adapter(settings))
    if backend == "pdfium":
        return PdfiumTextParserAdapter()
    if backend == "none":
        return NoParserEngine()

    raise ConfigurationError(
        message=(
            f"Unsupported ocr_backend '{backend}'. "
            f"Supported backends: {', '.join(SUPPORTED_OCR_BACKENDS)}."
        ),
        details={"ocr_backend": backend, "supported": list(SUPPORTED_OCR_BACKENDS)},
    )
