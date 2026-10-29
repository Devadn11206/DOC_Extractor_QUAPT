"""
Fixtures and synthetic generators for Phase 2 validation and ingestion tests.
All fixtures generate safe synthetic binary buffers in memory without relying
on external files or real confidential documents.
"""

import io
import struct
from typing import BinaryIO
import pytest

from doc_extractor.malware import EICAR_SIGNATURE


def create_minimal_pdf(page_count: int = 1) -> bytes:
    """Creates a structurally valid synthetic PDF in memory with specified page count."""
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    
    offsets = []
    # 1: Catalog
    offsets.append(buf.tell())
    buf.write(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")

    # Page object numbers: 3 to 2 + page_count
    page_objs = " ".join(f"{3 + i} 0 R" for i in range(page_count))
    offsets.append(buf.tell())
    buf.write(f"2 0 obj\n<< /Type /Pages /Kids [{page_objs}] /Count {page_count} >>\nendobj\n".encode("ascii"))

    for i in range(page_count):
        offsets.append(buf.tell())
        buf.write(f"{3 + i} 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n".encode("ascii"))

    xref_offset = buf.tell()
    buf.write(f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        buf.write(f"{off:010d} 00000 n \n".encode("ascii"))

    buf.write(
        f"trailer\n<< /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return buf.getvalue()


def create_minimal_png(width: int = 10, height: int = 10) -> bytes:
    """Creates a structurally valid PNG decodable by OpenCV and PIL."""
    import cv2
    import numpy as np

    # If requested width or height is larger (e.g. for decompression bomb test),
    # create a small valid PNG and patch the IHDR header bytes (bytes 16 to 24)
    # to simulate the header without allocating massive pixel arrays
    if width > 500 or height > 500:
        arr = np.full((10, 10, 3), 255, dtype=np.uint8)
        ok, buf = cv2.imencode(".png", arr)
        raw_png = buf.tobytes()
        import zlib
        ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        crc = struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data))
        patched = raw_png[:16] + ihdr_data + crc + raw_png[33:]
        return patched

    arr = np.full((height, width, 3), 255, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", arr)
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


def create_minimal_jpeg(width: int = 10, height: int = 10) -> bytes:
    """Creates a minimal valid JPEG with SOI, SOF0 (dimensions), and EOI markers."""
    buf = io.BytesIO()
    buf.write(b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    # SOF0 segment: len=11, precision=8, height, width, 1 component
    sof_len = 11
    sof_data = struct.pack(">BHHB", 8, height, width, 1) + b"\x01\x11\x00"
    buf.write(b"\xFF\xC0")
    buf.write(struct.pack(">H", sof_len))
    buf.write(sof_data)
    # EOI
    buf.write(b"\xFF\xD9")
    return buf.getvalue()


def create_minimal_tiff(pages: int = 1, little_endian: bool = True) -> bytes:
    """Creates a structurally valid TIFF with IFD chains representing multi-page TIFFs."""
    endian = "<" if little_endian else ">"
    buf = io.BytesIO()
    # Header: II* or MM* + offset to first IFD (8)
    magic = b"II*\x00" if little_endian else b"MM\x00*"
    buf.write(magic)
    buf.write(struct.pack(endian + "I", 8))

    current_offset = 8
    for p in range(pages):
        buf.seek(current_offset)
        # 1 entry: ImageWidth (tag 256, type 3 SHORT, count 1, value 10)
        num_entries = 1
        buf.write(struct.pack(endian + "H", num_entries))
        # Entry: Tag=256, Type=3 (SHORT), Count=1, Val=10
        buf.write(struct.pack(endian + "HHI", 256, 3, 1))
        buf.write(struct.pack(endian + "H", 10))
        buf.write(b"\x00\x00")  # padding in 4-byte value field
        
        # Next IFD pointer
        if p == pages - 1:
            buf.write(struct.pack(endian + "I", 0))  # End of IFD chain
        else:
            next_off = current_offset + 2 + (num_entries * 12) + 4
            buf.write(struct.pack(endian + "I", next_off))
            current_offset = next_off

    return buf.getvalue()


@pytest.fixture
def sample_pdf_bytes():
    return create_minimal_pdf(page_count=2)


@pytest.fixture
def sample_png_bytes():
    return create_minimal_png(width=100, height=100)


@pytest.fixture
def sample_jpeg_bytes():
    return create_minimal_jpeg(width=100, height=100)


@pytest.fixture
def sample_tiff_bytes():
    return create_minimal_tiff(pages=3, little_endian=True)


@pytest.fixture
def sample_eicar_bytes():
    """EICAR test file disguised as a PDF."""
    return b"%PDF-1.4\n" + EICAR_SIGNATURE + b"\n%%EOF"

