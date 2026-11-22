"""
Explicit pass-through Module B parser: no OCR, page images preserved (PRD v1.5 Module B scope).

PRD v1.5 describes Module B as "document classification, schema binding, and passing image paths":
the vision model reads the page image itself, so an OCR step is not required to
run the pipeline. ``PassThroughParserEngine`` is that mode, made explicit:

- every page is parsed into an EMPTY layout (no blocks, no tables, no checkboxes),
- no optical confidence exists and none is invented (``mean_ocr_confidence`` is None),
- the page image stays addressable (``PageLayoutOutput.page_image_path`` is set by Module B),
- the rule classifier has no content evidence, so classification is UNKNOWN and the
  document requires review - it never guesses a type.

It is NOT a fallback: it only runs when it is the configured backend (the default), it never
substitutes for a failing OCR engine, and its metadata says plainly that it performs no OCR.
Configure ``ocr_backend='pdfium'`` to read the native text layer of digital PDFs for grounded evidence and
rule-based classification.
"""

from typing import List

import numpy as np

from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.base import ParserEngineMetadata, RawOCRToken


def _read_no_text(_image: np.ndarray) -> List[RawOCRToken]:
    """The pass-through 'engine': reads nothing, by design."""
    return []


class PassThroughParserEngine(LocalOCRParserAdapter):
    """Parser that performs no OCR and preserves the page image for the vision model."""

    ENGINE_NAME = "PassThrough-NoOCR"

    def __init__(self) -> None:
        super().__init__(
            engine_name=self.ENGINE_NAME,
            engine_version="1.0.0",
            raw_extractor=_read_no_text,
            is_operational=True,
        )

    def get_metadata(self) -> ParserEngineMetadata:
        return ParserEngineMetadata(
            engine_name=self.engine_name,
            engine_version=self.engine_version,
            is_operational=True,
            supports_optical_confidence=False,
            description=(
                "Explicit pass-through (no OCR): empty layouts, page images preserved, "
                "classification UNKNOWN and review required unless another backend is configured."
            ),
        )
