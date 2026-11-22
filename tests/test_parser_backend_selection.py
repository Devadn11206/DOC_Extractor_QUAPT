"""Module B parser backend selection after the Tesseract removal (PRD v1.5: no OCR engine in the core)."""
import pytest
from pydantic import ValidationError

from doc_extractor.config import Settings
from doc_extractor.parsing import PassThroughParserEngine, build_parser_engine
from doc_extractor.parsing.factory import SUPPORTED_OCR_BACKENDS, NoParserEngine
from doc_extractor.parsing.pdfium import PdfiumTextParserAdapter


def test_supported_backends_are_passthrough_pdfium_and_none():
    assert SUPPORTED_OCR_BACKENDS == ("passthrough", "pdfium", "none")


def test_default_settings_select_the_passthrough():
    assert Settings().ocr_backend == "passthrough"
    assert isinstance(build_parser_engine(), PassThroughParserEngine)


def test_pdfium_and_none_backends_build_their_engines():
    assert isinstance(build_parser_engine(Settings(ocr_backend="pdfium")), PdfiumTextParserAdapter)
    none = build_parser_engine(Settings(ocr_backend="none"))
    assert isinstance(none, NoParserEngine) and none.is_operational() is False


@pytest.mark.parametrize("backend", ["tesseract", "pdfium_hybrid", "paddle"])
def test_unsupported_backends_fail_validation(backend):
    with pytest.raises(ValidationError):
        Settings(ocr_backend=backend)


def test_no_tesseract_settings_remain():
    fields = set(Settings.model_fields)
    assert not {f for f in fields if "tesseract" in f}
