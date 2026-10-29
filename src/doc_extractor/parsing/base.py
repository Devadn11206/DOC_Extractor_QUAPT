"""
Pluggable Visual Layout Parser Engine Interfaces.

Adheres to:
- ARC-002: Replaceable parser interface separate from extraction and classification.
- FR-PRS-001: Layout-faithful Markdown and typed text blocks with bounding boxes.
- FR-PRS-005: Optical confidence reported per text block (ocr_confidence in [0, 1]).
- SCH-013: 200-DPI top-left pixel space coordinates for downstream evidence.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import numpy as np

from doc_extractor.contracts_b import (
    BoundingBox,
    LayoutBlock,
    LayoutElementType,
    PageLayoutOutput,
)
from doc_extractor.exceptions import DocumentExtractorError, DocumentProcessingError
from doc_extractor.handoff import CoordinateTransform
from doc_extractor.loader_b import LoadedPageImage


class ParserEngineError(DocumentProcessingError):
    """Raised when layout parsing or OCR engine fails."""
    def __init__(
        self,
        message: str,
        engine_name: str,
        code: str = "PARSER_ENGINE_ERROR",
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        merged_details = dict(details or {})
        merged_details["engine_name"] = engine_name
        super().__init__(message=message, code=code, details=merged_details)


@dataclass(frozen=True)
class RawOCRToken:
    """Raw token detected by an OCR engine before layout reconstruction."""
    text: str
    bbox_preprocessed: Tuple[float, float, float, float]
    confidence: Optional[float]  # None if engine does not report optical confidence
    reading_order_hint: int = 0


@dataclass(frozen=True)
class ParserEngineMetadata:
    """Identity and capabilities metadata of a parser engine."""
    engine_name: str
    engine_version: str
    is_operational: bool
    supports_optical_confidence: bool
    description: str


class ParserEngine(ABC):
    """
    Abstract Base Class for visual layout parsing and OCR engines (ARC-002).
    
    Implementations must be pluggable and produce standard PageLayoutOutput
    without coupling to classification or extraction logic.
    """

    @abstractmethod
    def get_metadata(self) -> ParserEngineMetadata:
        """Returns engine identity, version, and capabilities."""
        pass

    @abstractmethod
    def is_operational(self) -> bool:
        """Returns True if local model/binary is installed and ready."""
        pass

    @abstractmethod
    def parse_page(self, loaded_page: LoadedPageImage) -> PageLayoutOutput:
        """
        Parses a loaded preprocessed page image into structured layout output.
        
        Guarantees:
        - Output adheres to PageLayoutOutput contract.
        - Text block bounding boxes are provided in preprocessed pixel space
          and inverted to original 200-DPI pixel space via CoordinateTransform.
        - OCR optical confidence is validated in [0, 1] or explicitly defaulted.
        - Reading order is preserved.
        """
        pass

