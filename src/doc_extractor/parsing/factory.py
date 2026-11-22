"""
Settings-driven selection of the Module B parser engine (ARC-002, FR-PRS-001, FR-PRS-005).

This module only *composes* existing parser implementations; it adds no OCR logic:

- ``passthrough``   -> PassThroughParserEngine (DEFAULT): no OCR, page images preserved, UNKNOWN
                       classification + review; needs no OCR runtime at all
- ``pdfium``        -> PdfiumTextParserAdapter (native PDF text only, no OCR)
- ``none``          -> NoParserEngine, an explicit and clearly non-operational parser

There is no automatic fallback between backends: the configured one is used or fails with an
actionable error. There is no OCR engine in the core: pages are read by the vision model (Qwen3-VL) or by
GLM-OCR through the routing table (PRD v1.5), not by Module B.
"""

from typing import Optional

from doc_extractor.config import Settings
from doc_extractor.contracts_b import PageLayoutOutput
from doc_extractor.exceptions import ConfigurationError
from doc_extractor.loader_b import LoadedPageImage
from doc_extractor.parsing.base import (
    ParserEngine,
    ParserEngineError,
    ParserEngineMetadata,
)
from doc_extractor.parsing.passthrough import PassThroughParserEngine
from doc_extractor.parsing.pdfium import PdfiumTextParserAdapter

SUPPORTED_OCR_BACKENDS = ("passthrough", "pdfium", "none")


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
                "Set Settings.ocr_backend to 'passthrough' or 'pdfium' to enable parsing."
            ),
            engine_name=self.engine_name,
            code="PARSER_NOT_CONFIGURED",
            details={"page_number": loaded_page.page_number},
        )


def build_parser_engine(settings: Optional[Settings] = None) -> ParserEngine:
    """
    Builds the Module B parser selected by ``settings.ocr_backend`` (default: explicit pass-through, no OCR).

    Raises:
        ConfigurationError: if ``ocr_backend`` is not a supported backend.
        ParserEngineError: if the selected backend's runtime is unavailable.
    """
    settings = settings if settings is not None else Settings()
    backend = settings.ocr_backend

    if backend == "passthrough":
        return PassThroughParserEngine()
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
