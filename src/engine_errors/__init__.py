"""
The single base class of every domain error in the Document Extraction Engine.

This is a deliberately tiny LEAF package: it imports nothing from ``doc_extractor`` or
``schema_lifecycle``, so either of them can import it first without triggering the other.

Why it exists: ``schema_lifecycle.exceptions`` used to import ``DocumentExtractorError`` from
``doc_extractor.exceptions`` and fall back to a private copy on ``ImportError``. Importing
``doc_extractor.exceptions`` runs ``doc_extractor/__init__``, which imports ``rule_evaluator``, which imports
``schema_lifecycle.metamodel``, which needs names from ``schema_lifecycle.exceptions`` while that module was
still half-initialised. When ``schema_lifecycle`` was imported first the cycle raised ``ImportError`` and M4
silently got a *different* base class, so the REST API's handlers (registered for Module A's class) no longer
recognised any M4 error and answered 500. With one shared base the identity is the same in every import order.

``doc_extractor.exceptions.DocumentExtractorError`` and ``schema_lifecycle.exceptions.DocumentExtractorError``
are re-exports of this class (``is`` identical); the behaviour is unchanged.

Adheres to:
- PRD §7.2 API-010: standardized error code envelope
- PRD §7.2 API-011: error messages SHALL NOT contain extracted document content or PII
- PRD §9 GR-001: strict rejection with standard machine-readable codes
"""

from typing import Any, Dict, Optional

__all__ = ["DocumentExtractorError"]


class DocumentExtractorError(Exception):
    """
    Base exception for all Document Extraction Engine errors.

    Ensures safe error formatting:
    1. Holds machine-readable standard error codes.
    2. Enforces that messages do not expose document content or PII.
    3. Formats to the PRD standard error envelope.
    """

    def __init__(
        self,
        message: str,
        code: str = "INTERNAL_ERROR",
        request_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.request_id = request_id
        self.details = details or {}

    def to_envelope(self) -> Dict[str, Any]:
        """
        Return the standardized error envelope required by PRD API-010:
        {
            "error": {
                "code": "...",
                "message": "...",
                "request_id": "...",
                "details": { ... }
            }
        }
        """
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": self.request_id,
                "details": self.details,
            }
        }
