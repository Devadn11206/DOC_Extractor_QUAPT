"""
Documents REST API Router (Module M5 / Phase 2 Milestone 2D).

Endpoints:
- POST /documents (API-001, upload only: Module A ingestion, rendering and preprocessing; no extraction)
- GET /documents (API-015, persisted metadata only; see DocumentListResponse for the limitations)
- GET /documents/{document_id} (API-002, persisted metadata only; see DocumentDetailResponse for the limitations)
- GET /documents/{document_id}/pages/{page_number} (API-003)
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, Path, Query, Request, Response, UploadFile

from doc_extractor.api.contracts.documents import (
    SCOPE_DOCUMENTS_READ,
    SCOPE_DOCUMENTS_WRITE,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentUploadResponse,
    parse_list_date_param,
    reject_unexpected_upload_input,
    reject_unknown_list_parameters,
    reject_unsupported_list_filter,
    reject_unsupported_upload_parameters,
    validate_idempotency_key,
    validate_list_date_range,
)
from doc_extractor.api.contracts.errors import ErrorEnvelope
from doc_extractor.api.dependencies import get_document_read_service, get_document_upload_service, require_scopes
from doc_extractor.api.services.document_listing import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DocumentListQuery,
    DocumentSortField,
    SortOrder,
    build_document_listing,
)
from doc_extractor.api.services.document_read_service import DocumentReadService
from doc_extractor.api.services.document_upload_service import DocumentUploadService
from doc_extractor.models import DocumentStatus

router = APIRouter(tags=["Documents"])


async def _reject_unexpected_upload_input(request: Request) -> None:
    """
    Upload input checks on the PARSED form: no query parameters, only the documented form fields, no duplicate
    fields, exactly one ``file`` part, and no unsupported parameters (``document_type_hint``, ``schema_id``,
    ``callback_url``). A dependency, so it runs after authentication and scope. ``request.form()`` returns the form
    FastAPI has already parsed (it is cached).
    """
    from collections import Counter

    form = await request.form()
    counts = Counter(k for k, _ in form.multi_items())
    reject_unexpected_upload_input(request.query_params.keys(), form.keys(), counts)
    reject_unsupported_upload_parameters(counts.keys())


@router.post(
    "/documents",
    status_code=202,
    dependencies=[Depends(require_scopes(SCOPE_DOCUMENTS_WRITE)), Depends(_reject_unexpected_upload_input)],
    response_model=DocumentUploadResponse,
    summary="Upload a document",
    description=(
        "Accepts one file (multipart) and runs Module A only: validation, malware scan, hashing, idempotency, the document "
        "record, page rendering and preprocessing. No extraction runs. Returns `202` with the `document_id` and nothing "
        "else; read the document through `GET /documents/{id}`. PROVISIONAL: requires the provisional scope "
        "`documents:write` (not owner-approved); `document_type_hint`, `schema_id` and `callback_url` are not supported "
        "yet and are rejected with 422; unknown form fields and any query parameter are rejected with 422; the body is "
        "limited to the configured maximum file size plus 1 MiB of multipart headroom and is refused with 413 before it is "
        "parsed; the body must arrive within the idle and total body timeouts (408); rendering runs inside the request and "
        "nothing continues in the background; an upload rejected as unsupported or password-protected (400), as malware "
        "(400) or by a scanner failure (503) leaves nothing stored (a late rejection is rolled back and verified; if the "
        "removal cannot be confirmed the answer is 500, not 400), while an operational failure (500) can leave a "
        "`processing` record; admission control limits "
        "concurrent uploads; supported only as one worker process per shared storage."
    ),
    responses={
        400: {"model": ErrorEnvelope, "description": "UNSUPPORTED_FILE or MALWARE_DETECTED"},
        401: {"model": ErrorEnvelope, "description": "Authentication required"},
        403: {"model": ErrorEnvelope, "description": "Insufficient scope (requires documents:write)"},
        409: {"model": ErrorEnvelope, "description": "Idempotency-Key reused with a different request"},
        413: {"model": ErrorEnvelope, "description": "FILE_TOO_LARGE (byte limit or page limit)"},
        408: {"model": ErrorEnvelope, "description": "REQUEST_TIMEOUT: the body did not arrive in time"},
        422: {"model": ErrorEnvelope, "description": "Invalid, unknown or unsupported parameter"},
        429: {"model": ErrorEnvelope, "description": "RATE_LIMITED: too many concurrent uploads"},
        500: {"model": ErrorEnvelope, "description": "Upload processing failed"},
        503: {"model": ErrorEnvelope, "description": "Malware scanning or service unavailable"},
    },
)
def upload_document(
    request: Request,
    response: Response,
    file: UploadFile = File(..., description="The document to upload (PDF, JPEG, PNG or TIFF)"),
    document_type_hint: Optional[str] = Form(None, description="NOT SUPPORTED yet: any value is rejected with 422"),
    schema_id: Optional[str] = Form(None, description="NOT SUPPORTED yet: any value is rejected with 422"),
    callback_url: Optional[str] = Form(None, description="NOT SUPPORTED yet: any value is rejected with 422"),
    idempotency_key: Optional[str] = Header(
        None, alias="Idempotency-Key", description="Optional key making a retry of the same upload idempotent"
    ),
    upload_service: DocumentUploadService = Depends(get_document_upload_service),
) -> DocumentUploadResponse:
    """
    Uploads one document (API-001). Synchronous on purpose: Module A's rendering and preprocessing run in the threadpool.

    The tenant comes only from the authenticated principal (through the upload-service dependency). The filename and the
    client's media type are untrusted: Module A decides the format from the file's magic bytes, and the client's content
    type is never read. Parameters are validated before Module A runs.
    """
    # document_type_hint, schema_id and callback_url are declared (so they appear in the contract) but are refused by presence before this body.
    key = validate_idempotency_key(idempotency_key)

    document_id = upload_service.upload(
        file.file,
        file.filename or "",
        idempotency_key=key,
        request_id=getattr(request.state, "request_id", None),
    )
    response.headers["Cache-Control"] = "private, no-store"
    return DocumentUploadResponse(document_id=document_id)


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
