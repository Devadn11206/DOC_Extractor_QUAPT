"""
File signature (magic bytes), structural validation, and boundary enforcement.

Implements:
- FR-ING-001: Accept PDF, JPG, PNG, and TIFF formats.
- FR-ING-002: Verify file signatures, size limits, page limits; reject unsupported files
  with code UNSUPPORTED_FILE and store nothing.
- GR-001: Decompression-bomb limits, malformed file fuzz resilience (0 crashes, 100% rejection).

Security & Performance Considerations:
- Stream/chunk-based inspection to avoid loading unbounded files into memory.
- Safe path handling to prevent path traversal.
- Binary structure sanity checking without heavy full rendering/decoding.
"""

from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import struct
from typing import BinaryIO, Optional, Tuple, Union

from doc_extractor.config import Settings
from doc_extractor.exceptions import UnsupportedFileError


class SupportedFormat(str, Enum):
    PDF = "application/pdf"
    JPEG = "image/jpeg"
    PNG = "image/png"
    TIFF = "image/tiff"


# Magic byte definitions according to binary specifications
MAGIC_SIGNATURES = {
    # PDF: %PDF- (25 50 44 46 2D)
    b"%PDF-": SupportedFormat.PDF,
    # JPEG: FF D8 FF
    b"\xFF\xD8\xFF": SupportedFormat.JPEG,
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    b"\x89PNG\r\n\x1a\n": SupportedFormat.PNG,
    # TIFF: Little-Endian (49 49 2A 00)
    b"II*\x00": SupportedFormat.TIFF,
    # TIFF: Big-Endian (4D 4D 00 2A)
    b"MM\x00*": SupportedFormat.TIFF,
}

EXTENSION_TO_FORMAT = {
    ".pdf": SupportedFormat.PDF,
    ".jpg": SupportedFormat.JPEG,
    ".jpeg": SupportedFormat.JPEG,
    ".png": SupportedFormat.PNG,
    ".tif": SupportedFormat.TIFF,
    ".tiff": SupportedFormat.TIFF,
}


@dataclass(frozen=True)
class ValidationResult:
    """Detailed result of file signature and structural validation."""
    is_valid: bool
    format: SupportedFormat
    file_size_bytes: int
    page_count: int
    detected_mime: str
    filename: str


def detect_magic_format(header_bytes: bytes) -> Optional[SupportedFormat]:
    """Sniffs the binary header to detect supported format signatures."""
    if len(header_bytes) < 4:
        return None

    if header_bytes.startswith(b"%PDF-"):
        return SupportedFormat.PDF
    if header_bytes.startswith(b"\xFF\xD8\xFF"):
        return SupportedFormat.JPEG
    if header_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return SupportedFormat.PNG
    if header_bytes.startswith(b"II*\x00") or header_bytes.startswith(b"MM\x00*"):
        return SupportedFormat.TIFF

    return None


def _validate_png_structure(stream: BinaryIO) -> None:
    """
    Validates PNG chunk structure and basic dimensions to prevent decompression bombs.
    PNG signature is 8 bytes, followed by IHDR chunk (4 bytes length, 4 bytes 'IHDR', 13 bytes data).
    """
    stream.seek(8)
    ihdr_len_bytes = stream.read(4)
    ihdr_tag = stream.read(4)
    if len(ihdr_len_bytes) < 4 or ihdr_tag != b"IHDR":
        raise UnsupportedFileError(
            message="Corrupted PNG: missing valid IHDR chunk.",
            details={"reason": "INVALID_PNG_IHDR"},
        )

    ihdr_data = stream.read(13)
    if len(ihdr_data) < 13:
        raise UnsupportedFileError(
            message="Corrupted PNG: truncated IHDR header.",
            details={"reason": "TRUNCATED_PNG"},
        )

    width, height = struct.unpack(">II", ihdr_data[0:8])
    # Decompression bomb guard: dimension limit (e.g. 20,000 x 20,000 px)
    if width <= 0 or height <= 0:
        raise UnsupportedFileError(
            message="Corrupted PNG: invalid dimensions.",
            details={"reason": "INVALID_DIMENSIONS", "width": width, "height": height},
        )
    if width > 20000 or height > 20000:
        raise UnsupportedFileError(
            message="File rejected: image dimensions exceed safe decompression bounds.",
            details={"reason": "DECOMPRESSION_BOMB_LIMIT", "width": width, "height": height},
        )


def _validate_jpeg_structure(stream: BinaryIO) -> None:
    """
    Validates JPEG segment markers. JPEG must end with EOI marker (FF D9).
    Also parses SOF markers to enforce decompression bomb limits.
    """
    stream.seek(0, os.SEEK_END)
    file_size = stream.tell()
    if file_size < 4:
        raise UnsupportedFileError(
            message="Corrupted JPEG: truncated file.",
            details={"reason": "TRUNCATED_JPEG"},
        )

    # Check for End of Image (EOI) FF D9 in last 128 bytes
    stream.seek(max(0, file_size - 128))
    tail = stream.read()
    if b"\xFF\xD9" not in tail:
        raise UnsupportedFileError(
            message="Corrupted JPEG: missing End-of-Image (EOI) marker.",
            details={"reason": "MISSING_JPEG_EOI"},
        )

    # Quick parse for dimensions across markers
    stream.seek(2)
    while True:
        marker_head = stream.read(2)
        if len(marker_head) < 2 or marker_head[0] != 0xFF:
            break
        marker = marker_head[1]
        # Ignore restart markers or fill bytes
        if marker in (0xD8, 0xD9, 0x00, 0xFF) or (0xD0 <= marker <= 0xD7):
            continue

        len_bytes = stream.read(2)
        if len(len_bytes) < 2:
            break
        seg_len = struct.unpack(">H", len_bytes)[0]
        if seg_len < 2:
            raise UnsupportedFileError(
                message="Corrupted JPEG: invalid segment length.",
                details={"reason": "INVALID_JPEG_SEGMENT"},
            )

        # Baseline / Progressive SOF markers (SOF0 = 0xC0, SOF2 = 0xC2)
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            sof_data = stream.read(5)
            if len(sof_data) == 5:
                # precision (1), height (2), width (2)
                height, width = struct.unpack(">HH", sof_data[1:5])
                if width > 20000 or height > 20000:
                    raise UnsupportedFileError(
                        message="File rejected: image dimensions exceed safe decompression bounds.",
                        details={"reason": "DECOMPRESSION_BOMB_LIMIT", "width": width, "height": height},
                    )
            break
        else:
            # Skip segment data
            stream.seek(seg_len - 2, os.SEEK_CUR)


def _inspect_tiff_pages(stream: BinaryIO, max_pages: int) -> int:
    """
    Safely inspects TIFF Image File Directory (IFD) chain to determine page count
    without decoding pixel rasters. Detects cyclic or infinite IFD chains.
    """
    stream.seek(0)
    header = stream.read(8)
    if len(header) < 8:
        raise UnsupportedFileError(
            message="Corrupted TIFF: truncated header.",
            details={"reason": "TRUNCATED_TIFF"},
        )

    endian = "<" if header[:2] == b"II" else ">"
    version = struct.unpack(endian + "H", header[2:4])[0]
    if version != 42:
        raise UnsupportedFileError(
            message="Corrupted TIFF: invalid version magic.",
            details={"reason": "INVALID_TIFF_VERSION"},
        )

    first_ifd_offset = struct.unpack(endian + "I", header[4:8])[0]
    if first_ifd_offset == 0:
        raise UnsupportedFileError(
            message="Corrupted TIFF: no image directories found.",
            details={"reason": "EMPTY_TIFF_IFD"},
        )

    page_count = 0
    next_ifd = first_ifd_offset
    visited_offsets = set()

    stream.seek(0, os.SEEK_END)
    file_size = stream.tell()

    while next_ifd != 0:
        if next_ifd in visited_offsets:
            raise UnsupportedFileError(
                message="Corrupted TIFF: cyclic directory pointer detected.",
                details={"reason": "TIFF_CYCLE_DETECTED"},
            )
        visited_offsets.add(next_ifd)

        if next_ifd + 2 > file_size:
            raise UnsupportedFileError(
                message="Corrupted TIFF: IFD offset beyond end of file.",
                details={"reason": "TIFF_OFFSET_OUT_OF_BOUNDS"},
            )

        stream.seek(next_ifd)
        entry_count_bytes = stream.read(2)
        if len(entry_count_bytes) < 2:
            break
        num_entries = struct.unpack(endian + "H", entry_count_bytes)[0]

        # Each IFD entry is 12 bytes
        skip_bytes = num_entries * 12
        if next_ifd + 2 + skip_bytes + 4 > file_size:
            raise UnsupportedFileError(
                message="Corrupted TIFF: truncated directory entries.",
                details={"reason": "TIFF_TRUNCATED_ENTRIES"},
            )

        stream.seek(skip_bytes, os.SEEK_CUR)
        next_ifd_bytes = stream.read(4)
        if len(next_ifd_bytes) < 4:
            break
        next_ifd = struct.unpack(endian + "I", next_ifd_bytes)[0]
        page_count += 1

        if page_count > max_pages:
            raise UnsupportedFileError(
                message=f"Document exceeds maximum page limit of {max_pages} pages.",
                details={"reason": "PAGE_LIMIT_EXCEEDED", "page_count": page_count, "max_pages": max_pages},
            )

    if page_count == 0:
        raise UnsupportedFileError(
            message="Corrupted TIFF: contains 0 pages.",
            details={"reason": "EMPTY_TIFF"},
        )

    return page_count


def _inspect_pdf_pages(stream: BinaryIO, max_pages: int) -> int:
    """
    Safely inspects a PDF stream to count pages and verify basic PDF structure
    without rendering. Handles linear / non-linear dictionaries, detects EOF markers,
    and enforces page limits.
    """
    stream.seek(0, os.SEEK_END)
    file_size = stream.tell()
    if file_size < 32:
        raise UnsupportedFileError(
            message="Corrupted PDF: file is too small to be a valid PDF.",
            details={"reason": "TRUNCATED_PDF"},
        )

    # Check for EOF marker in last 2048 bytes
    search_tail_size = min(file_size, 2048)
    stream.seek(file_size - search_tail_size)
    tail = stream.read(search_tail_size)
    if b"%%EOF" not in tail:
        raise UnsupportedFileError(
            message="Corrupted PDF: missing %%EOF marker.",
            details={"reason": "MISSING_PDF_EOF"},
        )

    # Count /Type /Page (excluding /Pages) safely via chunked scan
    # Note: Stream scan provides robust protection against malformed or truncated page trees
    stream.seek(0)
    page_count = 0
    chunk_overlap = 64
    chunk_size = 64 * 1024

    prev_overlap = b""
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        data = prev_overlap + chunk
        
        # Count occurrences of /Type /Page (with arbitrary whitespace)
        # Using token scanning
        idx = 0
        while True:
            p_idx = data.find(b"/Page", idx)
            if p_idx == -1:
                break
            # Ensure it is /Page and not /Pages
            if not data[p_idx:].startswith(b"/Pages"):
                # Check preceding context: should belong to a dictionary object
                before = data[max(0, p_idx - 32):p_idx]
                if b"/Type" in before:
                    page_count += 1
            idx = p_idx + 5

        if len(chunk) < chunk_size:
            break
        prev_overlap = chunk[-chunk_overlap:]

    if page_count == 0:
        # Fallback: scan for /Count inside /Pages if /Type /Page was compacted/compressed
        stream.seek(0)
        content_sample = stream.read(min(file_size, 256 * 1024))
        count_idx = content_sample.find(b"/Count ")
        if count_idx != -1:
            try:
                sub = content_sample[count_idx + 7:count_idx + 20]
                digits = []
                for b in sub:
                    if ord(b"0") <= b <= ord(b"9"):
                        digits.append(chr(b))
                    elif digits:
                        break
                if digits:
                    page_count = int("".join(digits))
            except Exception:
                page_count = 1
        else:
            page_count = 1  # Minimum 1 if valid PDF structure

    if page_count > max_pages:
        raise UnsupportedFileError(
            message=f"Document exceeds maximum page limit of {max_pages} pages.",
            details={"reason": "PAGE_LIMIT_EXCEEDED", "page_count": page_count, "max_pages": max_pages},
        )

    return page_count


class DocumentValidator:
    """
    Validates uploaded documents against magic bytes, claimed extension, size limits,
    page limits, and basic structural safety (FR-ING-001, FR-ING-002, GR-001).
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self.settings = settings or Settings()

    def validate_file(
        self,
        file_path_or_stream: Union[str, Path, BinaryIO],
        claimed_filename: str,
    ) -> ValidationResult:
        """
        Validates file signature, format, size limits, and structural sanity.

        Raises:
            UnsupportedFileError: If file is unsupported, corrupt, spoofed, or exceeds limits.
        """
        # Safe filename sanitization (prevent path traversal)
        safe_filename = Path(claimed_filename).name
        if not safe_filename or safe_filename in (".", ".."):
            raise UnsupportedFileError(
                message="Invalid or unsafe filename provided.",
                details={"reason": "INVALID_FILENAME"},
            )

        # Check claimed extension
        claimed_ext = Path(safe_filename).suffix.lower()
        expected_format = EXTENSION_TO_FORMAT.get(claimed_ext)

        # Check stream vs path
        is_opened_here = False
        if isinstance(file_path_or_stream, (str, Path)):
            path = Path(file_path_or_stream)
            if not path.exists() or not path.is_file():
                raise UnsupportedFileError(
                    message="Target file does not exist or is not a readable file.",
                    details={"reason": "FILE_NOT_FOUND"},
                )
            file_size = path.stat().st_size
            stream = open(path, "rb")
            is_opened_here = True
        else:
            stream = file_path_or_stream
            stream.seek(0, os.SEEK_END)
            file_size = stream.tell()
            stream.seek(0)

        try:
            # 1. Size bounds check (empty or exceeding max limit)
            if file_size == 0:
                raise UnsupportedFileError(
                    message="The uploaded file is empty (0 bytes).",
                    details={"reason": "EMPTY_FILE", "filename": safe_filename},
                )

            if file_size > self.settings.max_file_size_bytes:
                raise UnsupportedFileError(
                    message=f"File size ({file_size} bytes) exceeds limit of {self.settings.max_file_size_bytes} bytes.",
                    details={
                        "reason": "FILE_SIZE_LIMIT_EXCEEDED",
                        "size": file_size,
                        "limit": self.settings.max_file_size_bytes,
                    },
                )

            # 2. Magic bytes verification
            header_sample = stream.read(16)
            stream.seek(0)

            detected_format = detect_magic_format(header_sample)
            if not detected_format:
                raise UnsupportedFileError(
                    message="Unsupported file type: magic byte signature does not match any allowed format.",
                    details={
                        "reason": "MAGIC_BYTE_MISMATCH",
                        "detected_header": header_sample[:8].hex(),
                        "filename": safe_filename,
                    },
                )

            # 3. Claimed extension mismatch detection
            if expected_format is not None and expected_format != detected_format:
                raise UnsupportedFileError(
                    message=(
                        f"File extension '{claimed_ext}' does not match detected format "
                        f"'{detected_format.value}'."
                    ),
                    details={
                        "reason": "EXTENSION_MISMATCH",
                        "claimed_extension": claimed_ext,
                        "detected_mime": detected_format.value,
                    },
                )

            # 4. Check if MIME is allowed by settings
            if detected_format.value not in self.settings.allowed_mime_types:
                raise UnsupportedFileError(
                    message=f"MIME type '{detected_format.value}' is not permitted by configuration.",
                    details={"reason": "MIME_NOT_ALLOWED", "mime": detected_format.value},
                )

            # 5. Format-specific structural validation and page counting
            if detected_format == SupportedFormat.PDF:
                page_count = _inspect_pdf_pages(stream, self.settings.max_page_count)
            elif detected_format == SupportedFormat.TIFF:
                page_count = _inspect_tiff_pages(stream, self.settings.max_page_count)
            elif detected_format == SupportedFormat.PNG:
                _validate_png_structure(stream)
                page_count = 1
            elif detected_format == SupportedFormat.JPEG:
                _validate_jpeg_structure(stream)
                page_count = 1
            else:
                page_count = 1

            return ValidationResult(
                is_valid=True,
                format=detected_format,
                file_size_bytes=file_size,
                page_count=page_count,
                detected_mime=detected_format.value,
                filename=safe_filename,
            )

        finally:
            if is_opened_here:
                stream.close()

