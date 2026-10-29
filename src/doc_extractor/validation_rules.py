"""
Deterministic Normalization and Business Logic Validation Rules.
Strictly adheres to PRD §5.5 (FR-VAL-001 through FR-VAL-004) and PRD §6.3.
"""

from datetime import datetime
import re
from typing import Any, Dict, List, Optional, Tuple


# Regex patterns
PAN_REGEX = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
PHONE_DIGITS_REGEX = re.compile(r"\D")


# Verhoeff algorithm tables for Aadhaar checksum validation
VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def validate_aadhaar_checksum(aadhaar_str: str) -> bool:
    """Validate 12-digit Aadhaar number using Verhoeff algorithm."""
    clean_digits = re.sub(r"\s+", "", str(aadhaar_str))
    if not re.match(r"^\d{12}$", clean_digits):
        return False
    c = 0
    reversed_digits = [int(d) for d in reversed(clean_digits)]
    for i, item in enumerate(reversed_digits):
        c = VERHOEFF_D[c][VERHOEFF_P[i % 8][item]]
    return c == 0


def normalize_date_to_iso(date_str: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Normalizes a variety of date formats (DD/MM/YYYY, YYYY-MM-DD, DD-MM-YYYY, DD Month YYYY)
    to ISO 8601 (YYYY-MM-DD).
    Returns (normalized_date, error_message).
    """
    if not date_str or not isinstance(date_str, str):
        return None, None
    raw = date_str.strip()
    if not raw:
        return None, None

    date_formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y/%m/%d",
    ]
    for fmt in date_formats:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%Y-%m-%d"), None
        except ValueError:
            continue

    return raw, "INVALID_DATE_FORMAT"


def normalize_phone_number(phone_str: Optional[str], default_country_code: str = "+91") -> Tuple[Optional[str], Optional[str]]:
    """
    Normalizes phone number to E.164 standard (+91... for 10-digit Indian numbers).
    """
    if not phone_str or not isinstance(phone_str, str):
        return None, None
    raw = phone_str.strip()
    if not raw:
        return None, None

    digits = PHONE_DIGITS_REGEX.sub("", raw)
    if len(digits) == 10:
        return f"{default_country_code}{digits}", None
    elif len(digits) == 12 and digits.startswith("91"):
        return f"+{digits}", None
    elif raw.startswith("+") and len(digits) >= 10:
        return f"+{digits}", None
    return raw, "INVALID_PHONE_FORMAT"


def validate_pan(pan_value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    FR-VAL-002: PAN format validation: ^[A-Z]{5}[0-9]{4}[A-Z]$.
    """
    if not pan_value or not isinstance(pan_value, str):
        return None, None
    val = pan_value.strip().upper()
    if PAN_REGEX.match(val):
        return val, None
    return val, "INVALID_PAN_FORMAT"


def validate_chronology(
    admission_date_iso: Optional[str],
    discharge_date_iso: Optional[str],
) -> Optional[Dict[str, Any]]:
    """
    FR-VAL-004: Chronology check: admission_date <= discharge_date.
    Returns flag dict if violation occurs, else None.
    """
    if not admission_date_iso or not discharge_date_iso:
        return None

    try:
        dt_adm = datetime.strptime(admission_date_iso, "%Y-%m-%d")
        dt_dis = datetime.strptime(discharge_date_iso, "%Y-%m-%d")
        if dt_dis < dt_adm:
            return {
                "code": "CHRONOLOGY_MISMATCH",
                "message": f"Discharge date ({discharge_date_iso}) is prior to admission date ({admission_date_iso}).",
                "fields": ["admission_date", "discharge_date"],
            }
    except ValueError:
        pass
    return None


def validate_arithmetic(
    line_item_amounts: List[float],
    total_amount: Optional[float],
    tolerance: float = 0.01,
) -> Optional[Dict[str, Any]]:
    """
    FR-VAL-003: Arithmetic check: sum of line items equals declared total within tolerance.
    """
    if total_amount is None or not line_item_amounts:
        return None

    computed_sum = sum(float(x) for x in line_item_amounts if x is not None)
    diff = abs(computed_sum - float(total_amount))
    if diff > tolerance:
        return {
            "code": "ARITHMETIC_MISMATCH",
            "message": f"Computed sum {computed_sum:.2f} differs from declared total {total_amount:.2f} by {diff:.2f}.",
            "fields": ["line_items", "total_amount"],
        }
    return None
