"""
Tesseract OCR Engine Integration for Module B.

Implements:
- Word-level token extraction using pytesseract.image_to_data (FR-PRS-001, FR-PRS-005).
- Normalization of optical confidence from Tesseract scale (0..100) to standard [0.0, 1.0].
- Safe handling of undetermined/negative/invalid confidence scores.
- Bounding-box preservation in 200-DPI preprocessed image pixel coordinates.
- Fail-closed error propagation on OCR engine execution failure (FR-PRS-005).
"""

from dataclasses import dataclass
import math
import shutil
from typing import Any, Dict, List, Optional
import cv2
import numpy as np

try:
    import pytesseract
except ImportError:
    pytesseract = None

from doc_extractor.parsing.base import ParserEngineError, RawOCRToken


def normalize_tesseract_confidence(raw_conf: Any) -> Optional[float]:
    """
    Normalizes a Tesseract confidence score (reported on a 0..100 scale) to [0.0, 1.0].

    Returns:
        float in [0.0, 1.0] if valid and >= 0.0.
        None if raw_conf is -1 (Tesseract convention for undetermined/unavailable confidence),
        None, NaN, infinite, or cannot be parsed as a float.
    """
    if raw_conf is None:
        return None
    try:
        val = float(raw_conf)
        if math.isnan(val) or math.isinf(val):
            return None
        # Tesseract convention: -1 indicates undetermined / unavailable confidence
        if val < 0.0:
            return None
        # Normalize 0..100 -> 0.0..1.0 and clamp to [0.0, 1.0]
        return round(max(0.0, min(1.0, val / 100.0)), 4)
    except (ValueError, TypeError):
        return None


_INSTALL_HINT = (
    "Install the Tesseract OCR binary (macOS: 'brew install tesseract'; "
    "Debian/Ubuntu: 'apt-get install tesseract-ocr'; Windows: UB-Mannheim installer) "
    "or set Settings.ocr_tesseract_cmd to the full path of the tesseract executable."
)


def check_tesseract_available(
    lang: str = "eng",
    tesseract_cmd: Optional[str] = None,
) -> str:
    """
    Verifies that the Tesseract runtime can actually be used (fail-closed, FR-PRS-005).

    Checks, in order: the 'pytesseract' Python package, the Tesseract executable
    (PATH lookup or the explicitly configured ``tesseract_cmd``), and that every
    requested language pack (e.g. 'eng' or 'eng+hin') is installed.

    Args:
        lang: Tesseract language string; '+' joins multiple languages.
        tesseract_cmd: Optional explicit path/name of the tesseract executable.

    Returns:
        The detected Tesseract version string.

    Raises:
        ParserEngineError: with code OCR_DEPENDENCY_MISSING, OCR_BINARY_MISSING,
            OCR_BINARY_UNUSABLE or OCR_LANGUAGE_MISSING and an actionable message.
    """
    if pytesseract is None:
        raise ParserEngineError(
            message=(
                "The 'pytesseract' Python package is not installed. "
                "It is an optional dependency: install it with 'pip install -e \".[ocr]\"' or 'pip install pytesseract' "
                "(plus the Tesseract binary), or keep the default ocr_backend='passthrough'."
            ),
            engine_name="Tesseract",
            code="OCR_DEPENDENCY_MISSING",
        )

    if tesseract_cmd:
        resolved = shutil.which(tesseract_cmd)
        if resolved is None:
            raise ParserEngineError(
                message=(
                    f"Configured Tesseract executable '{tesseract_cmd}' was not found or is not executable. "
                    "Fix Settings.ocr_tesseract_cmd or unset it to use 'tesseract' from PATH."
                ),
                engine_name="Tesseract",
                code="OCR_BINARY_MISSING",
                details={"tesseract_cmd": tesseract_cmd},
            )
        pytesseract.pytesseract.tesseract_cmd = resolved

    try:
        version = str(pytesseract.get_tesseract_version())
    except pytesseract.TesseractNotFoundError as exc:
        raise ParserEngineError(
            message=f"Tesseract executable was not found. {_INSTALL_HINT}",
            engine_name="Tesseract",
            code="OCR_BINARY_MISSING",
            details={"cause": str(exc)},
        ) from exc
    except Exception as exc:
        raise ParserEngineError(
            message=f"Tesseract executable could not be run: {exc}. {_INSTALL_HINT}",
            engine_name="Tesseract",
            code="OCR_BINARY_UNUSABLE",
            details={"cause": str(exc)},
        ) from exc

    try:
        installed = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        raise ParserEngineError(
            message=f"Could not list installed Tesseract language packs: {exc}",
            engine_name="Tesseract",
            code="OCR_BINARY_UNUSABLE",
            details={"cause": str(exc)},
        ) from exc

    requested = [part for part in lang.split("+") if part]
    missing = [part for part in requested if part not in installed]
    if not requested or missing:
        raise ParserEngineError(
            message=(
                f"Tesseract language pack(s) not installed: {', '.join(missing) or lang!r}. "
                f"Installed: {', '.join(sorted(installed)) or 'none'}. "
                "Install the matching tessdata package or change Settings.ocr_tesseract_lang."
            ),
            engine_name="Tesseract",
            code="OCR_LANGUAGE_MISSING",
            details={"requested": requested, "installed": sorted(installed)},
        )

    return version


def tesseract_raw_extractor(
    image: np.ndarray,
    lang: str = "eng",
    config: str = "",
) -> List[RawOCRToken]:
    """
    Tesseract-backed raw OCR token extractor callable for LocalOCRParserAdapter.

    Executes pytesseract.image_to_data on the input image, extracts word-level tokens,
    maps coordinates to preprocessed image coordinate space [x0, y0, x1, y1],
    normalizes optical confidence scores to [0.0, 1.0], and filters out empty entries.

    Args:
        image: Preprocessed page image as a numpy array (BGR from OpenCV or Grayscale).
        lang: Tesseract language string (default: "eng").
        config: Additional Tesseract CLI configuration arguments (e.g. "--psm 6").

    Returns:
        List of RawOCRToken instances in reading order.

    Raises:
        ParserEngineError: If pytesseract is not installed, the Tesseract binary is missing,
                           or OCR execution encounters an engine failure.
    """
    if pytesseract is None:
        raise ParserEngineError(
            message="pytesseract is not installed in the python environment.",
            engine_name="Tesseract",
            code="OCR_DEPENDENCY_MISSING",
        )

    if image is None or not isinstance(image, np.ndarray) or image.size == 0:
        raise ParserEngineError(
            message="Input image buffer is empty or invalid.",
            engine_name="Tesseract",
            code="EMPTY_IMAGE_BUFFER",
        )

    # Convert BGR (OpenCV standard) to RGB (expected by PIL/pytesseract)
    if len(image.shape) == 3 and image.shape[2] == 3:
        image_input = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    else:
        image_input = image

    try:
        data = pytesseract.image_to_data(
            image_input,
            output_type=pytesseract.Output.DICT,
            lang=lang,
            config=config,
        )
    except Exception as exc:
        # Explicitly do NOT swallow OCR failure (Requirement 9)
        raise ParserEngineError(
            message=f"Tesseract OCR execution failed: {str(exc)}",
            engine_name="Tesseract",
            code="OCR_EXTRACTION_FAILED",
            details={"lang": lang, "config": config, "cause": str(exc)},
        ) from exc

    required_keys = ("text", "left", "top", "width", "height", "conf")
    if not isinstance(data, dict) or not all(k in data for k in required_keys):
        raise ParserEngineError(
            message="Tesseract output is malformed: missing standard TSV/DICT keys.",
            engine_name="Tesseract",
            code="MALFORMED_OCR_OUTPUT",
        )

    texts = data["text"]
    lefts = data["left"]
    tops = data["top"]
    widths = data["width"]
    heights = data["height"]
    confs = data["conf"]
    levels = data.get("level")

    tokens: List[RawOCRToken] = []
    n_entries = len(texts)

    for i in range(n_entries):
        # 1. Level check: in standard Tesseract output, level 5 represents words
        if levels is not None and i < len(levels):
            try:
                if int(levels[i]) != 5:
                    continue
            except (ValueError, TypeError):
                pass

        # 2. Text check: ignore empty or whitespace-only text entries (Requirement 6)
        raw_text = texts[i]
        text_str = str(raw_text).strip() if raw_text is not None else ""
        if not text_str:
            continue

        # 3. Coordinate conversion: bounding box in preprocessed image space
        try:
            x0 = max(0.0, float(lefts[i]))
            y0 = max(0.0, float(tops[i]))
            w = max(0.0, float(widths[i]))
            h = max(0.0, float(heights[i]))
            x1 = x0 + w
            y1 = y0 + h
        except (ValueError, TypeError, IndexError):
            continue

        # Filter out zero-dimension degenerate boxes
        if w <= 0.0 or h <= 0.0:
            continue

        # 4. Confidence normalization to [0.0, 1.0] (Requirement 5 & 6)
        raw_c = confs[i] if i < len(confs) else None
        normalized_conf = normalize_tesseract_confidence(raw_c)

        tokens.append(
            RawOCRToken(
                text=text_str,
                bbox_preprocessed=(x0, y0, x1, y1),
                confidence=normalized_conf,
                reading_order_hint=len(tokens),
            )
        )

    return tokens


class TesseractExtractor:
    """
    Configurable, callable wrapper for Tesseract OCR extraction.
    Compatible with LocalOCRParserAdapter's raw_extractor parameter.
    """

    def __init__(self, lang: str = "eng", config: str = "") -> None:
        self.lang = lang
        self.config = config

    def __call__(self, image: np.ndarray) -> List[RawOCRToken]:
        return tesseract_raw_extractor(image, lang=self.lang, config=self.config)
