"""
Documents API contracts and validation rules (Module M5 / Phase 2 Milestone 2D).

Defines:
- Provisional authorization scope constants for document operations (M2D-D04).
- Path-safe document ID validation rules compliant with PRD SEC-006.
"""

import re
from typing import Any

from doc_extractor.api.exceptions import TenantResourceNotFoundError

# Provisional authorization scope for document read operations (M2D-D04)
SCOPE_DOCUMENTS_READ: str = "documents:read"

# Safe document ID pattern: alphanumeric, hyphen, underscore, 1 to 128 characters.
# Strict path segment format prohibiting directory traversal (..), path separators (/ or \\),
# control characters, or whitespace.
SAFE_DOCUMENT_ID_REGEX = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def validate_document_id(document_id: Any) -> str:
    """
    Validates that document_id is a non-empty, path-safe identifier string.

    Accepts:
    - Standard document identifiers composed of [a-zA-Z0-9_-] up to 128 chars.
    - Existing engine identifier formats (e.g. doc_*, doc_rej_*).

    Rejects:
    - Non-string types, boolean values, empty strings, whitespace.
    - Path traversal attempts (.., /, \\).
    - Unsafe characters, null bytes, control characters.

    Fails closed:
    Raises TenantResourceNotFoundError (mapped to HTTP 404 DOCUMENT_NOT_FOUND)
    to conceal document existence and prevent structural probing per SEC-006.
    """
    if not isinstance(document_id, str) or isinstance(document_id, bool):
        raise TenantResourceNotFoundError()

    if not SAFE_DOCUMENT_ID_REGEX.fullmatch(document_id):
        raise TenantResourceNotFoundError()

    return document_id
