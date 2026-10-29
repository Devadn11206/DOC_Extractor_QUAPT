"""
Parsing module namespace exports for Module B.
"""

from doc_extractor.parsing.base import (
    ParserEngine,
    ParserEngineError,
    ParserEngineMetadata,
    RawOCRToken,
)
from doc_extractor.parsing.adapter import LocalOCRParserAdapter
from doc_extractor.parsing.markdown import generate_page_markdown, sort_blocks_reading_order
from doc_extractor.parsing.tables import (
    CandidateTableCell,
    CandidateTableRegion,
    TableReconstructionError,
    TableReconstructor,
)
from doc_extractor.parsing.checkboxes import (
    CandidateCheckboxRegion,
    CheckboxDetector,
    CheckboxDetectorError,
    CheckboxState,
)
from doc_extractor.parsing.character_grids import (
    CandidateCharacterGrid,
    CharacterBoxEvidence,
    CharacterGridError,
    CharacterGridReconstructor,
    RawCharacterObservation,
)

__all__ = [
    "ParserEngine",
    "ParserEngineError",
    "ParserEngineMetadata",
    "RawOCRToken",
    "LocalOCRParserAdapter",
    "generate_page_markdown",
    "sort_blocks_reading_order",
    "CandidateTableCell",
    "CandidateTableRegion",
    "TableReconstructionError",
    "TableReconstructor",
    "CandidateCheckboxRegion",
    "CheckboxDetector",
    "CheckboxDetectorError",
    "CheckboxState",
    "CandidateCharacterGrid",
    "CharacterBoxEvidence",
    "CharacterGridError",
    "CharacterGridReconstructor",
    "RawCharacterObservation",
]

