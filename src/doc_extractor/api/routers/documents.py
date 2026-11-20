"""
Documents REST API Router (Module M5 / Phase 2 Milestone 2D).

Endpoints:
- GET /documents (API-015, persisted metadata only; see DocumentListResponse for the limitations)
- GET /documents/{document_id} (API-002, persisted metadata only; see DocumentDetailResponse for the limitations)
- GET /documents/{document_id}/pages/{page_number} (API-003)
"""

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from doc_extractor.api.contracts.documents import (
    SCOPE_DOCUMENTS_READ,
    DocumentDetailResponse,
    DocumentListResponse,
    parse_list_date_param,
    reject_unknown_list_parameters,
    reject_unsupported_list_filter,
    validate_list_date_range,
)
from doc_extractor.api.contracts.errors import ErrorEnvelope
from doc_extractor.api.dependencies import get_document_read_service, require_scopes
from doc_extractor.api.services.document_listing import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DocumentListQuery,
    DocumentSortField,
    SortOrder,
    build_document_listing,
)
from doc_extractor.api.services.document_read_service import DocumentReadService
from doc_extractor.models import DocumentStatus

router = APIRouter(tags=["Documents"])


@router.get(
    "/documents",
    dependencies=[Depends(require_scopes(SCOPE_DOCUMENTS_READ))],
    response_model=DocumentListResponse,
    summary="List persisted document metadata",
    description=(
        "Lists the persisted metadata of the caller's tenant (`content`), with `stats` and `paging`. PROVISIONAL. "
        "`status` is the persisted value and is not authoritative (it is currently always `processing`), so `stats` "
        "counts persisted statuses only: `needs_review`, `auto_approved` and `failed` read 0 until a terminal status is "
        "persisted, and `stats` ignores every filter. Filters: `status`, `from` / `to` (inclusive, on `created_at`; a "
        "date-only `to` includes that whole UTC day; send `+` in an offset as `%2B`) and `q` (case-sensitive "
        "document-ID prefix). `documentType` and `confidenceBelow` are not supported and are rejected with 422, and so "
        "is any query parameter name that is not listed here (a misspelling is never silently ignored). "
        "Order is the chosen `sort` / `order` (default `created_at` descending), then `document_id` ascending. There is "
        "no snapshot between requests, so a document added meanwhile can move records across a page boundary. Every "
        "request reads all of the tenant's records. Two provisional ceilings apply: 10,000 candidate document "
        "directories and 20,000 directory entries of any kind examined; a tenant above either is refused, never "
        "truncated."
    ),
    responses={
        401: {"model": ErrorEnvelope, "description": "Authentication required"},
        403: {"model": ErrorEnvelope, "description": "Insufficient scope (requires documents:read)"},
        422: {"model": ErrorEnvelope, "description": "Invalid, unknown or unsupported query parameter"},
        500: {"model": ErrorEnvelope, "description": "A stored record is unreadable or invalid, or a provisional ceiling was exceeded (10,000 candidate document directories or 20,000 directory entries examined)"},
        503: {"model": ErrorEnvelope, "description": "Service unavailable"},
    },
)
def get_document_list(
    request: Request,
    response: Response,
    page: int = Query(1, ge=1, description="1-based page number"),
    size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Page size (provisional maximum 100)"),
    status: Optional[DocumentStatus] = Query(None, description="Persisted status (not authoritative)"),
    created_from: Optional[str] = Query(
        None, alias="from", description="Inclusive lower bound on created_at: ISO 8601 date or datetime"
    ),
    created_to: Optional[str] = Query(
        None, alias="to", description="Inclusive upper bound on created_at: ISO 8601 date or datetime"
    ),
    q: Optional[str] = Query(None, min_length=1, max_length=128, description="Case-sensitive document-ID prefix"),
    sort: DocumentSortField = Query(DocumentSortField.CREATED_AT, description="Sort field"),
    order: SortOrder = Query(SortOrder.DESC, description="Sort direction; document_id ascending breaks ties"),
    document_type: Optional[str] = Query(
        None, alias="documentType", description="NOT SUPPORTED (not persisted): any value is rejected with 422"
    ),
    confidence_below: Optional[str] = Query(
        None, alias="confidenceBelow", description="NOT SUPPORTED (not persisted): any value is rejected with 422"
    ),
    read_service: DocumentReadService = Depends(get_document_read_service),
) -> DocumentListResponse:
    """
    Lists the persisted metadata of the caller's tenant (API-015).

    Synchronous on purpose: the descriptor-relative reads run in FastAPI's threadpool. The tenant comes only from the
    authenticated principal; a tenant query parameter or header is never read. Parameters are validated before any
    storage access: an unknown parameter name and an unsupported filter are rejected rather than silently ignored.
    Authentication and scope are enforced by the route dependencies, before this body runs.
    """
    reject_unknown_list_parameters(request.query_params.keys())
    reject_unsupported_list_filter("documentType", document_type)
    reject_unsupported_list_filter("confidenceBelow", confidence_below)
    lower = parse_list_date_param(created_from, "from", end_of_range=False) if created_from is not None else None
    upper = parse_list_date_param(created_to, "to", end_of_range=True) if created_to is not None else None
    validate_list_date_range(lower, upper)

    query = DocumentListQuery(
        status=status, created_from=lower, created_to=upper, id_prefix=q, sort=sort, order=order, page=page, size=size
    )
    listing = build_document_listing(read_service.list_documents(), query)
    body = DocumentListResponse.from_listing(listing)
    response.headers["Cache-Control"] = "private, no-store"
    return body


@router.get(
    "/documents/{document_id}",
    dependencies=[Depends(require_scopes(SCOPE_DOCUMENTS_READ))],
    response_model=DocumentDetailResponse,
    summary="Retrieve persisted document metadata",
    description=(
        "Returns the persisted metadata of one document of the caller's tenant. PROVISIONAL: `status` is returned "
        "exactly as persisted (the stored status is currently always `processing` and is not authoritative for the "
        "extraction outcome), `created_at` and `updated_at` keep their persisted instants (the serialized text may be "
        "normalized), and `result` is always `null` because no canonical extraction envelope is persisted yet."
    ),
    responses={
        401: {"model": ErrorEnvelope, "description": "Authentication required"},
        403: {"model": ErrorEnvelope, "description": "Insufficient scope (requires documents:read)"},
        404: {"model": ErrorEnvelope, "description": "Document not found (also another tenant's document)"},
        500: {"model": ErrorEnvelope, "description": "Stored record unreadable or invalid"},
        503: {"model": ErrorEnvelope, "description": "Service unavailable"},
    },
)
def get_document_detail(
    response: Response,
    document_id: str = Path(..., description="Unique document identifier"),
    read_service: DocumentReadService = Depends(get_document_read_service),
) -> DocumentDetailResponse:
    """
    Retrieves the persisted metadata of one document (API-002).

    Synchronous on purpose: the descriptor-relative file reads run in FastAPI's threadpool, never on the event loop.
    The tenant comes only from the authenticated principal (via the read-service dependency). An unknown, malformed or
    foreign id, or a record that does not belong to the caller, is the concealed 404 DOCUMENT_NOT_FOUND.
    """
    record = read_service.get_document(document_id)
    response.headers["Cache-Control"] = "private, no-store"
    return DocumentDetailResponse.from_record(record)


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
