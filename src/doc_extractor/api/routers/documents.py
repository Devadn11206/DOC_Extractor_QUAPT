"""
Documents REST API Router (Module M5 / Phase 2 Milestone 2D).

Endpoints:
- GET /documents/{document_id}/pages/{page_number} (API-003)
"""

from fastapi import APIRouter, Depends, Path, Response

from doc_extractor.api.contracts.documents import SCOPE_DOCUMENTS_READ
from doc_extractor.api.contracts.errors import ErrorEnvelope
from doc_extractor.api.dependencies import get_document_read_service, require_scopes
from doc_extractor.api.services.document_read_service import DocumentReadService

router = APIRouter(tags=["Documents"])


@router.get(
    "/documents/{document_id}/pages/{page_number}",
    dependencies=[Depends(require_scopes(SCOPE_DOCUMENTS_READ))],
    response_class=Response,
    summary="Retrieve rendered document page image",
    description="Securely returns the rendered document page as PNG image bytes.",
    responses={
        200: {
            "content": {"image/png": {}},
            "description": "Rendered document page image bytes (PNG).",
        },
        401: {"model": ErrorEnvelope, "description": "Authentication required"},
        403: {"model": ErrorEnvelope, "description": "Insufficient scope (requires documents:read)"},
        404: {"model": ErrorEnvelope, "description": "Document or page not found"},
        422: {"model": ErrorEnvelope, "description": "Validation error"},
    },
)
def get_document_page(
    document_id: str = Path(..., description="Unique document identifier"),
    page_number: int = Path(..., ge=1, description="1-based page sequence number"),
    read_service: DocumentReadService = Depends(get_document_read_service),
) -> Response:
    """
    Retrieves rendered document page image as PNG bytes (API-003).

    Conceals missing documents, malformed IDs, foreign-tenant documents,
    and out-of-range pages with an identical 404 DOCUMENT_NOT_FOUND.
    """
    image_bytes, media_type = read_service.get_page_image(
        document_id=document_id,
        page_number=page_number,
    )
    headers = {
        "Content-Type": media_type,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Disposition": f'inline; filename="page-{page_number}.png"',
    }
    return Response(
        content=image_bytes,
        media_type=media_type,
        headers=headers,
    )
