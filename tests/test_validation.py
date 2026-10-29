"""
Automated unit and boundary tests for DocumentValidator (FR-ING-001, FR-ING-002, GR-001).

Tests:
A. Valid PDF, JPEG, PNG and TIFF signatures.
B. Unsupported extensions and formats.
C. Renamed executable / arbitrary binary presented as a PDF.
D. Extension/signature mismatches.
E. Empty and truncated files.
F. Corrupt or malformed PDFs and images.
G. File-size and page-count limits, including boundary cases.
K. Adversarial / fuzz malformed inputs.
"""

import io
import pytest

from doc_extractor.config import Settings
from doc_extractor.exceptions import UnsupportedFileError
from doc_extractor.validation import (
    DocumentValidator,
    SupportedFormat,
    detect_magic_format,
)
from tests.conftest import (
    create_minimal_jpeg,
    create_minimal_pdf,
    create_minimal_png,
    create_minimal_tiff,
)


@pytest.fixture
def validator():
    settings = Settings(
        max_file_size_bytes=1024 * 1024,  # 1 MB for testing
        max_page_count=10,                 # 10 pages max for testing
    )
    return DocumentValidator(settings)


# --- Category A: Valid Signatures & Formats ---

@pytest.mark.unit
def test_valid_pdf_validation(validator, sample_pdf_bytes):
    res = validator.validate_file(io.BytesIO(sample_pdf_bytes), "document.pdf")
    assert res.is_valid is True
    assert res.format == SupportedFormat.PDF
    assert res.page_count == 2
    assert res.detected_mime == "application/pdf"


@pytest.mark.unit
def test_valid_png_validation(validator, sample_png_bytes):
    res = validator.validate_file(io.BytesIO(sample_png_bytes), "scan.png")
    assert res.is_valid is True
    assert res.format == SupportedFormat.PNG
    assert res.page_count == 1
    assert res.detected_mime == "image/png"


@pytest.mark.unit
def test_valid_jpeg_validation(validator, sample_jpeg_bytes):
    res = validator.validate_file(io.BytesIO(sample_jpeg_bytes), "invoice.jpg")
    assert res.is_valid is True
    assert res.format == SupportedFormat.JPEG
    assert res.page_count == 1
    assert res.detected_mime == "image/jpeg"


@pytest.mark.unit
def test_valid_tiff_little_and_big_endian(validator):
    # Little-endian
    le_tiff = create_minimal_tiff(pages=2, little_endian=True)
    res_le = validator.validate_file(io.BytesIO(le_tiff), "contract.tiff")
    assert res_le.is_valid is True
    assert res_le.format == SupportedFormat.TIFF
    assert res_le.page_count == 2

    # Big-endian
    be_tiff = create_minimal_tiff(pages=1, little_endian=False)
    res_be = validator.validate_file(io.BytesIO(be_tiff), "record.tif")
    assert res_be.is_valid is True
    assert res_be.format == SupportedFormat.TIFF
    assert res_be.page_count == 1


# --- Category B: Unsupported Extensions & Formats ---

@pytest.mark.unit
def test_unsupported_format_rejection(validator):
    # Zip / DOCX / text magic bytes
    zip_bytes = b"PK\x03\x04\x14\x00\x00\x00"
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(zip_bytes), "archive.zip")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert "MAGIC_BYTE_MISMATCH" in str(exc_info.value.details)


# --- Category C & D: Extension Mismatches & Executables ---

@pytest.mark.security
def test_renamed_executable_as_pdf(validator):
    """PRD FR-ING-002 Acceptance Test: Given a renamed .exe as .pdf, reject UNSUPPORTED_FILE."""
    exe_header = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff"
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(exe_header), "malicious_tool.pdf")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "MAGIC_BYTE_MISMATCH"


@pytest.mark.unit
def test_extension_mismatch_png_with_jpg_extension(validator, sample_png_bytes):
    """Claimed .jpg but file is actually a PNG."""
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(sample_png_bytes), "photo.jpg")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "EXTENSION_MISMATCH"


# --- Category E: Empty & Truncated Files ---

@pytest.mark.unit
def test_reject_empty_file(validator):
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(b""), "empty.pdf")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "EMPTY_FILE"


@pytest.mark.unit
def test_reject_truncated_pdf(validator):
    truncated = b"%PDF-1.4\n1 0 obj << >>"
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(truncated), "truncated.pdf")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert "PDF" in exc_info.value.details.get("reason", "")


@pytest.mark.unit
def test_reject_truncated_jpeg(validator):
    truncated = b"\xFF\xD8\xFF\xE0\x00"
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(truncated), "truncated.jpg")
    assert exc_info.value.code == "UNSUPPORTED_FILE"


# --- Category F & K: Corrupt / Malformed / Decompression Bombs ---

@pytest.mark.unit
def test_reject_png_decompression_bomb_dimensions(validator):
    """PNG with dimensions exceeding 20,000x20,000 px."""
    huge_png = create_minimal_png(width=50000, height=50000)
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(huge_png), "bomb.png")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "DECOMPRESSION_BOMB_LIMIT"


@pytest.mark.unit
def test_reject_tiff_cyclic_ifd(validator):
    """Corrupted TIFF with cyclic IFD pointer causing potential infinite loop."""
    buf = io.BytesIO()
    buf.write(b"II*\x00")
    buf.write(b"\x08\x00\x00\x00")  # Offset 8
    # At offset 8: 1 entry, then next IFD offset points back to 8!
    buf.write(b"\x01\x00")
    buf.write(b"\x00" * 12)
    buf.write(b"\x08\x00\x00\x00")  # Cycle back to offset 8
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(buf, "cyclic.tiff")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "TIFF_CYCLE_DETECTED"


# --- Category G: Size Bounds & Page Count Limits ---

@pytest.mark.unit
def test_reject_file_size_exceeded(validator):
    large_pdf = b"%PDF-1.4\n" + (b"0" * (1024 * 1024 + 100)) + b"\n%%EOF"
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(large_pdf), "large.pdf")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "FILE_SIZE_LIMIT_EXCEEDED"


@pytest.mark.unit
def test_reject_pdf_page_limit_exceeded(validator):
    # Validator has max_page_count = 10; test with 12 pages
    pdf_12_pages = create_minimal_pdf(page_count=12)
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(pdf_12_pages), "multipage.pdf")
    assert exc_info.value.code == "UNSUPPORTED_FILE"
    assert exc_info.value.details.get("reason") == "PAGE_LIMIT_EXCEEDED"


@pytest.mark.unit
def test_accept_pdf_page_limit_boundary(validator):
    # Boundary: exactly 10 pages
    pdf_10_pages = create_minimal_pdf(page_count=10)
    res = validator.validate_file(io.BytesIO(pdf_10_pages), "boundary.pdf")
    assert res.is_valid is True
    assert res.page_count == 10


# --- Category K: Fuzz Corpus of Malformed Files (GR-001) ---

@pytest.mark.security
@pytest.mark.parametrize(
    "corrupt_input,claimed_name",
    [
        (b"", "zero.pdf"),
        (b"%PDF", "short.pdf"),
        (b"%PDF-1.4\x00\xff\xfe", "broken.pdf"),
        (b"\xFF\xD8\xFF", "short.jpg"),
        (b"\x89PNG\r\n\x1a\n", "short.png"),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\x01ABCD\x00", "bad_chunk.png"),
        (b"II*\x00\x99\x99\x99\x99", "out_of_bounds.tiff"),
        (b"RANDOM_JUNK_DATA_WITHOUT_MAGIC_BYTES_1234567890", "junk.png"),
        (b"..\x00\xff\\Windows\\System32\\cmd.exe", "traversal.pdf"),
    ],
)
def test_fuzz_malformed_inputs_zero_crashes(validator, corrupt_input, claimed_name):
    """PRD GR-001: 0 crashes, 100% rejected with standard error code."""
    with pytest.raises(UnsupportedFileError) as exc_info:
        validator.validate_file(io.BytesIO(corrupt_input), claimed_name)
    assert exc_info.value.code == "UNSUPPORTED_FILE"

