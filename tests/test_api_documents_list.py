"""
Tests for API-015: GET /api/v1/documents (Milestone 2D, persisted metadata only, PROVISIONAL).

Three layers are verified here (see docs/m5-open-decisions.md, M5-D06 / M2D-D01 / M2D-D02 / M2D-D04):

1. ``document_listing``: the PURE filter / sort / paginate / statistics logic over validated records (no filesystem).
2. ``DocumentReadService.list_documents``: the hardened descriptor-relative enumeration of ONE tenant directory
   (fail closed on corruption, symlinks and special files; skip non-documents; the two scan ceilings; nothing is created).
3. The real route against the real storage mapping and records written through ``LocalDiskStorage``.

PROVISIONAL limits that these tests pin down so they cannot drift silently: ``stats`` counts PERSISTED statuses only
(Module A writes ``processing`` once and no terminal status exists), one corrupt record fails the whole list,
the list is a full scan bounded by two ceilings (10,000 candidate document directories and 20,000 directory entries
examined), unknown query parameter names are rejected, and no snapshot is kept between page requests.
"""

import ast
import inspect
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from doc_extractor.api.app import create_app
from doc_extractor.api.composition import ApplicationContainer
from doc_extractor.api.config import ApiSettings
from doc_extractor.api.contracts.documents import (
    SCOPE_DOCUMENTS_READ,
    LIST_QUERY_PARAMETERS,
    DocumentListResponse,
    DocumentStats,
    PagingInfo,
    parse_list_date_param,
    public_document_resource,
    validate_list_date_range,
)
from doc_extractor.api.exceptions import ApiValidationError
from doc_extractor.api.ports.authentication import ApiPrincipal
from doc_extractor.api.routers.documents import get_document_list
from doc_extractor.api.services import document_listing
from doc_extractor.api.services import document_read_service as read_service_module
from doc_extractor.api.services.document_listing import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    DocumentListQuery,
    DocumentSortField,
    SortOrder,
    build_document_listing,
    compute_document_stats,
)
from doc_extractor.api.services.document_read_service import (
    DocumentListingLimitExceededError,
    DocumentReadService,
)
from doc_extractor.exceptions import (
    ComponentConfigurationError,
    StorageError,
    TenantStorageIsolationError,
)
from doc_extractor.models import DocumentRecord, DocumentStatus, IngestionState
from doc_extractor.storage import LocalDiskStorage
from tests.test_api_documents_get import (
    PUBLIC_KEYS,
    _assert_generic_500,
    _make_decoy_document,
    _record,
    _rewrite_record,
    _store,
)
from tests.test_api_documents_page_image import (
    ALPHA_HEADERS,
    MockAuthProvider,
    _assert_descriptors_clean,
    _call_with_timeout,
    _install_proxy,
    _LogCapture,
    _once,
    _open_fd_count,
    _swap_for_fifo,
    _swap_for_symlink,
)

ROW_KEYS = PUBLIC_KEYS - {"result"}          # a list row is the sanctioned DocumentResource: no reserved ``result`` key
BETA_HEADERS = {"Authorization": "Bearer token-beta-reader"}
WRITER_ONLY_HEADERS = {"Authorization": "Bearer token-alpha-writer-only"}
FRESH_HEADERS = {"Authorization": "Bearer token-fresh-tenant"}
LIST_URL = "/api/v1/documents"
BASE_TIME = datetime(2026, 11, 1, 8, 0, 0, tzinfo=timezone.utc)
UTC = timezone.utc


@pytest.fixture
def api(tmp_path):
    settings = ApiSettings(app_env="development", storage_base_dir=str(tmp_path))
    container = ApplicationContainer(api_settings=settings, auth_provider=MockAuthProvider())
    app = create_app(settings=settings, container=container)
    # Default TestClient: an untyped exception escaping the route would fail the test instead of becoming a 500.
    return {"app": app, "client": TestClient(app), "container": container, "tmp_path": tmp_path}


@pytest.fixture
def captured_logs():
    handler = _LogCapture()
    attached = []
    for name in ("doc_extractor.api.read_service", "doc_extractor.api.errors"):
        target = logging.getLogger(name)
        attached.append((target, target.level))
        target.addHandler(handler)
        target.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        for target, level in attached:
            target.removeHandler(handler)
            target.setLevel(level)


def _stamp(minutes: int) -> str:
    return (BASE_TIME + timedelta(minutes=minutes)).isoformat()


def _doc(doc_id: str, tenant_id: str = "tenant_alpha", *, minutes: int = 0, **overrides) -> DocumentRecord:
    values = dict(created_at=_stamp(minutes), updated_at=_stamp(minutes), idempotency_key=None)
    values.update(overrides)
    return _record(doc_id, tenant_id, **values)


def _store_many(api, count: int, tenant_id: str = "tenant_alpha", prefix: str = "doc_", **overrides):
    """``count`` records, document ``i`` created ``i`` minutes after BASE_TIME (so a higher index is newer)."""
    for i in range(count):
        _store(api, _doc(f"{prefix}{i:04d}", tenant_id, minutes=i, **overrides))


def _list(api, headers=ALPHA_HEADERS, **params):
    return api["client"].get(LIST_URL, params=params, headers=headers)


def _ids(response) -> list:
    return [row["document_id"] for row in response.json()["content"]]


def _service(api, tenant_id: str = "tenant_alpha") -> DocumentReadService:
    return DocumentReadService(LocalDiskStorage(api["tmp_path"], tenant_id=tenant_id), tenant_id)


def _tree(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def _assert_validation_error(response, parameter: str, error_type: str = None, *leaks: str) -> dict:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    locations = [e["location"] for e in error["details"]["validation_errors"]]
    assert ["query", parameter] in locations, locations
    if error_type is not None:
        assert error_type in [e["type"] for e in error["details"]["validation_errors"]]
    for leak in leaks:
        assert leak not in response.text, f"{leak!r} was echoed in a validation error"
    return error


def _assert_unknown_parameter(response, *leaks: str) -> dict:
    """The fixed, non-reflective 422 for an unrecognized query parameter name."""
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    (detail,) = error["details"]["validation_errors"]
    assert detail["location"] == ["query"] and detail["type"] == "unknown_parameter"
    assert detail["message"].startswith("Unknown query parameter. Allowed parameters:")
    assert "content" not in response.json() and "stats" not in response.json()
    for leak in leaks:
        assert leak not in response.text, f"{leak!r} was reflected in the unknown-parameter error"
    return {k: v for k, v in error.items() if k != "request_id"}


# ============================================================================
# 1. Pure listing logic (no filesystem)
# ============================================================================

def _pure(doc_id: str, *, minutes: int = 0, **overrides) -> DocumentRecord:
    return _doc(doc_id, minutes=minutes, **overrides)


def test_pure_module_imports_no_filesystem_database_or_storage():
    tree = ast.parse(inspect.getsource(document_listing))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    forbidden = {"os", "pathlib", "io", "json", "shutil", "sqlite3", "socket", "doc_extractor.storage",
                 "doc_extractor.api.services.document_read_service"}
    assert not (imported & forbidden), imported & forbidden


def test_pure_listing_does_not_mutate_its_input_and_is_repeatable():
    records = [_pure(f"doc_{i}", minutes=i % 3) for i in range(7)]
    snapshot = list(records)
    query = DocumentListQuery(size=3, page=2)
    first = build_document_listing(records, query)
    assert records == snapshot
    assert build_document_listing(records, query) == first


_SORT_FIXTURE = [
    _doc("doc_a", minutes=60, page_count=3, file_size_bytes=300, status=DocumentStatus.PROCESSING),
    _doc("doc_b", minutes=60, page_count=1, file_size_bytes=100, status=DocumentStatus.FAILED),
    _doc("doc_c", minutes=0, page_count=3, file_size_bytes=200, status=DocumentStatus.NEEDS_REVIEW),
    _doc("doc_d", minutes=120, page_count=2, file_size_bytes=400, status=DocumentStatus.AUTO_APPROVED),
]


@pytest.mark.parametrize(
    "field, order, expected",
    [
        ("created_at", "desc", ["doc_d", "doc_a", "doc_b", "doc_c"]),     # tie at +60 min: document_id ASCENDING
        ("created_at", "asc", ["doc_c", "doc_a", "doc_b", "doc_d"]),      # tie-break stays ascending in both directions
        ("document_id", "asc", ["doc_a", "doc_b", "doc_c", "doc_d"]),
        ("document_id", "desc", ["doc_d", "doc_c", "doc_b", "doc_a"]),
        ("page_count", "desc", ["doc_a", "doc_c", "doc_d", "doc_b"]),     # 3, 3 (tie: a before c), 2, 1
        ("page_count", "asc", ["doc_b", "doc_d", "doc_a", "doc_c"]),
        ("file_size_bytes", "desc", ["doc_d", "doc_a", "doc_c", "doc_b"]),
        ("file_size_bytes", "asc", ["doc_b", "doc_c", "doc_a", "doc_d"]),
        ("status", "asc", ["doc_d", "doc_b", "doc_c", "doc_a"]),          # alphabetical by stored value
        ("status", "desc", ["doc_a", "doc_c", "doc_b", "doc_d"]),
    ],
)
def test_pure_every_sort_field_and_direction_with_the_documented_tie_break(field, order, expected):
    query = DocumentListQuery(sort=DocumentSortField(field), order=SortOrder(order))
    listing = build_document_listing(_SORT_FIXTURE, query)
    assert [r.document_id for r in listing.records] == expected


@pytest.mark.parametrize("field", [f.value for f in DocumentSortField])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_pure_result_does_not_depend_on_the_order_of_the_input(field, order):
    """The documented total order holds for ANY input order, so tied keys can never reshuffle between requests."""
    import itertools
    import random
    records = [
        _pure(f"doc_{i:02d}", minutes=i % 3, page_count=1 + i % 2, file_size_bytes=100 * (i % 2),
              status=(DocumentStatus.PROCESSING, DocumentStatus.FAILED)[i % 2])
        for i in range(8)                                                   # many ties on every sortable key
    ]
    query = DocumentListQuery(sort=DocumentSortField(field), order=SortOrder(order), size=100)
    expected = [r.document_id for r in build_document_listing(records, query).records]
    candidates = [list(reversed(records)), list(itertools.islice(itertools.permutations(records), 0, 1))[0]]
    rng = random.Random(15)
    for _ in range(25):
        shuffled = list(records)
        rng.shuffle(shuffled)
        candidates.append(shuffled)
    for candidate in candidates:
        assert [r.document_id for r in build_document_listing(candidate, query).records] == expected


def test_pure_default_query_is_newest_first_size_ten():
    query = DocumentListQuery()
    assert (query.sort, query.order, query.page, query.size) == (DocumentSortField.CREATED_AT, SortOrder.DESC, 1, 10)
    assert DEFAULT_PAGE_SIZE == 10 and MAX_PAGE_SIZE == 100


@pytest.mark.parametrize("count", [0, 1, 9, 10, 11, 20, 21])
def test_pure_page_counts_at_the_page_size_boundaries(count):
    records = [_pure(f"doc_{i:03d}", minutes=i) for i in range(count)]
    first = build_document_listing(records, DocumentListQuery())
    assert first.total_items == count
    assert first.total_pages == -(-count // 10)
    assert len(first.records) == min(count, 10)
    seen = []
    for page in range(1, first.total_pages + 1):
        seen += [r.document_id for r in build_document_listing(records, DocumentListQuery(page=page)).records]
    assert seen == [r.document_id for r in sorted(records, key=lambda r: r.created_at, reverse=True)]
    assert len(set(seen)) == count                                       # pages are disjoint


def test_pure_out_of_range_page_is_empty_with_the_true_totals():
    records = [_pure(f"doc_{i}", minutes=i) for i in range(12)]
    listing = build_document_listing(records, DocumentListQuery(page=3, size=5))
    assert listing.total_pages == 3 and len(listing.records) == 2
    beyond = build_document_listing(records, DocumentListQuery(page=4, size=5))
    assert beyond.records == () and (beyond.total_items, beyond.total_pages, beyond.page) == (12, 3, 4)
    assert beyond.stats["total"] == 12
    huge = build_document_listing(records, DocumentListQuery(page=10 ** 30, size=5))
    assert huge.records == () and huge.total_items == 12
    empty = build_document_listing([], DocumentListQuery(page=1))
    assert (empty.records, empty.total_items, empty.total_pages) == ((), 0, 0)


@pytest.mark.parametrize("page, size", [(0, 10), (-1, 10), (1, 0), (1, -5)])
def test_pure_invalid_page_or_size_is_refused(page, size):
    with pytest.raises(ValueError):
        build_document_listing([], DocumentListQuery(page=page, size=size))


def test_pure_stats_count_persisted_statuses_over_all_records_before_any_filter():
    records = [
        _pure("doc_1", status=DocumentStatus.PROCESSING), _pure("doc_2", status=DocumentStatus.PROCESSING),
        _pure("doc_3", status=DocumentStatus.NEEDS_REVIEW), _pure("doc_4", status=DocumentStatus.AUTO_APPROVED),
        _pure("doc_5", status=DocumentStatus.FAILED), _pure("doc_6", status=DocumentStatus.REVIEWED),
        _pure("doc_7", status=DocumentStatus.REJECTED),
    ]
    expected = {"total": 7, "processing": 2, "needs_review": 1, "auto_approved": 1, "failed": 1}
    assert compute_document_stats(records) == expected
    assert sum(v for k, v in expected.items() if k != "total") == 5 != expected["total"]     # categories need not add up
    filtered = build_document_listing(records, DocumentListQuery(status=DocumentStatus.FAILED, size=1))
    assert filtered.total_items == 1 and filtered.stats == expected
    nothing = build_document_listing(records, DocumentListQuery(id_prefix="zzz"))
    assert nothing.total_items == 0 and nothing.stats == expected


def test_pure_status_filter_is_equality_on_the_persisted_status():
    records = [_pure("doc_1"), _pure("doc_2", status=DocumentStatus.FAILED)]
    only = build_document_listing(records, DocumentListQuery(status=DocumentStatus.FAILED))
    assert [r.document_id for r in only.records] == ["doc_2"]
    assert build_document_listing(records, DocumentListQuery(status=DocumentStatus.REVIEWED)).total_items == 0


def test_pure_id_prefix_is_case_sensitive_and_a_prefix_not_a_substring():
    records = [_pure("doc_Abc"), _pure("doc_abc"), _pure("xdoc_abc"), _pure("doc_abd")]
    def ids(prefix):
        return sorted(r.document_id for r in build_document_listing(records, DocumentListQuery(id_prefix=prefix)).records)
    assert ids("doc_a") == ["doc_abc", "doc_abd"]
    assert ids("doc_A") == ["doc_Abc"]
    assert ids("DOC_") == []
    assert ids("abc") == []                                     # substring is not a prefix
    assert ids("doc_abc") == ["doc_abc"]


_INSTANT_FIXTURE = [
    _doc("doc_r1", created_at="2026-11-19T10:00:00+00:00"),
    _doc("doc_r2", created_at="2026-11-19T12:00:00+02:00"),               # same instant as r1
    _doc("doc_r3", created_at="2026-11-19T10:00:00"),                     # naive: read as UTC, same instant again
    _doc("doc_r4", created_at="2026-11-19T09:59:59.999999+00:00"),
    _doc("doc_r5", created_at="2026-11-19T10:00:00.000001+00:00"),
    _doc("doc_r6", created_at="2026-11-20T00:00:00+00:00"),
    _doc("doc_r7", created_at="2026-11-19T23:59:59.999999+00:00"),
    _doc("doc_r8", created_at="2026-11-19T23:30:00-05:00"),               # = 2026-11-20T04:30Z
]


def _between(created_from, created_to):
    lower = parse_list_date_param(created_from, "from", end_of_range=False) if created_from else None
    upper = parse_list_date_param(created_to, "to", end_of_range=True) if created_to else None
    listing = build_document_listing(_INSTANT_FIXTURE, DocumentListQuery(created_from=lower, created_to=upper, size=100))
    return sorted(r.document_id for r in listing.records)


def test_pure_date_range_bounds_are_inclusive_and_compare_instants():
    assert _between("2026-11-19T10:00:00Z", "2026-11-19T10:00:00Z") == ["doc_r1", "doc_r2", "doc_r3"]
    assert _between("2026-11-19T10:00:00.000001Z", "2026-11-19T10:00:00.000001Z") == ["doc_r5"]
    assert _between("2026-11-19T10:00:00+00:00", "2026-11-19T10:00:00+00:00") == ["doc_r1", "doc_r2", "doc_r3"]
    assert _between("2026-11-19T12:00:00+02:00", "2026-11-19T10:00:00Z") == ["doc_r1", "doc_r2", "doc_r3"]   # offsets compare as instants
    assert _between("2026-11-19T10:00:00", "2026-11-19T10:00:00") == ["doc_r1", "doc_r2", "doc_r3"]          # naive query = UTC


def test_pure_date_only_to_includes_the_whole_utc_day_and_from_starts_it():
    whole_day = _between("2026-11-19", "2026-11-19")
    assert whole_day == ["doc_r1", "doc_r2", "doc_r3", "doc_r4", "doc_r5", "doc_r7"]
    assert "doc_r6" not in whole_day and "doc_r8" not in whole_day
    assert _between(None, "2026-11-19") == whole_day                                   # to: through 23:59:59.999999
    assert _between("2026-11-20", None) == ["doc_r6", "doc_r8"]                        # from: starting 00:00:00
    assert _between("2026-11-20", "2026-11-20") == ["doc_r6", "doc_r8"]                # r8 is 04:30Z on the 20th


def test_pure_sorting_by_created_at_orders_mixed_offsets_by_instant():
    listing = build_document_listing(_INSTANT_FIXTURE, DocumentListQuery(size=100))
    ids = [r.document_id for r in listing.records]
    assert ids[:2] == ["doc_r8", "doc_r6"]                                  # 04:30Z on the 20th, then 00:00Z on the 20th
    assert ids[2] == "doc_r7" and ids[3] == "doc_r5"
    assert ids[4:7] == ["doc_r1", "doc_r2", "doc_r3"]                       # the same instant: document_id ascending
    assert ids[7] == "doc_r4"


@pytest.mark.parametrize("value, expected", [
    ("2026-11-19", datetime(2026, 11, 19, 0, 0, tzinfo=UTC)),
    ("2026-11-19T10:00", datetime(2026, 11, 19, 10, 0, tzinfo=UTC)),
    ("2026-11-19T10:00:00", datetime(2026, 11, 19, 10, 0, tzinfo=UTC)),
    ("2026-11-19T10:00:00Z", datetime(2026, 11, 19, 10, 0, tzinfo=UTC)),
    ("2026-11-19T10:00:00+00:00", datetime(2026, 11, 19, 10, 0, tzinfo=UTC)),
    ("2026-11-19T12:00:00+02:00", datetime(2026, 11, 19, 10, 0, tzinfo=UTC)),
    ("2026-11-19T10:00:00.123456Z", datetime(2026, 11, 19, 10, 0, 0, 123456, tzinfo=UTC)),
])
def test_pure_query_date_forms_that_are_accepted(value, expected):
    assert parse_list_date_param(value, "from", end_of_range=False) == expected


def test_pure_date_only_end_of_range_is_the_last_microsecond_and_never_overflows():
    assert parse_list_date_param("2026-11-19", "to", end_of_range=True) == datetime(2026, 11, 19, 23, 59, 59, 999999, tzinfo=UTC)
    assert parse_list_date_param("9999-12-31", "to", end_of_range=True).year == 9999
    assert parse_list_date_param("2026-11-19T10:00:00Z", "to", end_of_range=True) == datetime(2026, 11, 19, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize("value", [
    "", " ", "garbage", "1700000000", "0", "-1", "20261119", "20261119T100000Z", "2026-W47-3", "2026-13-01", "2026-02-30",
    "0000-01-01", "2026-11-19T24:00:00", "2026-11-19T10", "2026-11-19T10:00:00+25:00", "2026-11-19T10:00:00 +00:00",
    " 2026-11-19", "2026-11-19 ", "2026-11-19\n", "2026-11-19T10:00:00\n", "2026-11-19 10:00:00",
    "２０２６-11-19", "2026-11-19T10:00:00.1234567Z", "2026-11-19T10:00:00+0000", "2026-11-19t10:00:00z", "inf", "nan",
])
def test_pure_query_date_forms_that_are_rejected(value):
    with pytest.raises(ApiValidationError) as excinfo:
        parse_list_date_param(value, "to", end_of_range=True)
    assert excinfo.value.code == "VALIDATION_ERROR" and excinfo.value.status_code == 422
    assert excinfo.value.details["validation_errors"][0]["location"] == ["query", "to"]


def test_pure_reversed_range_is_refused_and_an_equal_or_open_range_is_not():
    early, late = datetime(2026, 11, 1, tzinfo=UTC), datetime(2026, 11, 2, tzinfo=UTC)
    with pytest.raises(ApiValidationError):
        validate_list_date_range(late, early)
    validate_list_date_range(early, early)
    validate_list_date_range(early, late)
    validate_list_date_range(None, early)
    validate_list_date_range(late, None)
    validate_list_date_range(None, None)


def test_pure_models_are_closed_and_hold_the_five_prd_stat_keys():
    assert set(DocumentStats.model_fields) == {"total", "processing", "needs_review", "auto_approved", "failed"}
    assert set(PagingInfo.model_fields) == {"page", "size", "total_items", "total_pages"}
    assert set(DocumentListResponse.model_fields) == {"content", "stats", "paging"}
    with pytest.raises(Exception):
        DocumentStats(total=1, processing=0, needs_review=0, auto_approved=0, failed=0, extra=1)


def test_public_document_resource_is_the_api002_allow_list_unit():
    resource = public_document_resource(_record(error={"code": "UNSUPPORTED_FILE", "message": "/srv/leak", "details": {"p": 1}}))
    assert (resource.error_code, resource.error_message) == ("UNSUPPORTED_FILE", "This file type isn't supported.")
    unknown = public_document_resource(_record(error={"code": "SECRET-CODE", "message": "SECRET-MSG"}))
    assert unknown.error_code == "INTERNAL_ERROR" and "SECRET" not in unknown.model_dump_json()
    for malformed in ("boom", {"code": 5}, {"message": "m"}, {"code": "X", "message": 7}):
        with pytest.raises(StorageError) as excinfo:
            public_document_resource(_record(error=malformed))
        assert excinfo.value.details == {"reason": "record_error_invalid"}


# ============================================================================
# 2. DocumentReadService.list_documents
# ============================================================================

def test_service_lists_only_the_tenants_own_documents_with_interleaved_tenants(api):
    for i in range(6):
        _store(api, _doc(f"doc_alpha_{i}", "tenant_alpha", minutes=i))
        _store(api, _doc(f"doc_beta_{i}", "tenant_beta", minutes=i))
        _store(api, _doc(f"doc_shared_{i}", "tenant_alpha" if i % 2 else "tenant_beta", minutes=i))
    alpha = {r.document_id for r in _service(api, "tenant_alpha").list_documents()}
    beta = {r.document_id for r in _service(api, "tenant_beta").list_documents()}
    assert alpha == {f"doc_alpha_{i}" for i in range(6)} | {f"doc_shared_{i}" for i in (1, 3, 5)}
    assert beta == {f"doc_beta_{i}" for i in range(6)} | {f"doc_shared_{i}" for i in (0, 2, 4)}
    assert alpha.isdisjoint(beta)
    assert all(r.tenant_id == "tenant_alpha" for r in _service(api, "tenant_alpha").list_documents())


def test_service_never_opens_another_tenants_directory_or_documents(api, monkeypatch):
    _store(api, _doc("doc_mine", "tenant_alpha"))
    _store(api, _doc("doc_theirs", "tenant_beta"))
    opened = []
    _install_proxy(monkeypatch, before_open=lambda path, dir_fd: opened.append(str(path)))
    records = _service(api, "tenant_alpha").list_documents()
    assert [r.document_id for r in records] == ["doc_mine"]
    assert "tenant_beta" not in opened and "doc_theirs" not in opened
    assert "tenant_alpha" in opened and "doc_mine" in opened


def test_service_hashed_and_case_distinct_tenant_identities_stay_apart(api):
    hashed = "Tenant.Mixed@Case"                     # not [a-z0-9_-] only: stored under an h+<hash> directory
    shouting = "TENANT_ALPHA"                        # differs from tenant_alpha only by case
    _store(api, _doc("doc_plain", "tenant_alpha"))
    _store(api, _doc("doc_hashed", hashed))
    _store(api, _doc("doc_shouting", shouting))
    assert [r.document_id for r in _service(api, hashed).list_documents()] == ["doc_hashed"]
    assert [r.document_id for r in _service(api, shouting).list_documents()] == ["doc_shouting"]
    assert [r.document_id for r in _service(api, "tenant_alpha").list_documents()] == ["doc_plain"]
    assert any(p.name.startswith("h+") for p in api["tmp_path"].iterdir()), "the hashed directory layout was not exercised"


def test_service_empty_or_missing_tenant_is_an_empty_list_and_creates_nothing(api):
    tmp_path = api["tmp_path"]
    _store(api, _doc("doc_other", "tenant_beta"))
    before = _tree(tmp_path)
    assert _service(api, "tenant_never_provisioned").list_documents() == []
    assert _service(api, "Hashed.Never@Provisioned").list_documents() == []
    missing_root = tmp_path / "no_such_storage_root"
    assert DocumentReadService(LocalDiskStorage(missing_root, tenant_id="tenant_alpha"), "tenant_alpha").list_documents() == []
    assert _tree(tmp_path) == before and not missing_root.exists()


def test_service_skips_non_documents_and_counts_only_real_records(api):
    tenant_dir = _store(api, _doc("doc_real_1")).parent
    _store(api, _doc("doc_real_2"))
    (tenant_dir / "idempotency").mkdir()
    (tenant_dir / "idempotency" / "abc123.json").write_text("{not even json", encoding="utf-8")
    (tenant_dir / "doc_without_record").mkdir()                              # a directory with no document_record.json
    (tenant_dir / "doc_only_tmp").mkdir()
    (tenant_dir / "doc_only_tmp" / ".tmp_rec_deadbeef").write_text("partial", encoding="utf-8")
    (tenant_dir / "not a valid id").mkdir()                                  # invalid document id (space)
    (tenant_dir / "..hidden").mkdir()
    (tenant_dir / ".DS_Store").write_text("x", encoding="utf-8")
    (tenant_dir / "notes.txt").write_text("x", encoding="utf-8")
    assert (tenant_dir / ".tenant-identity.json").exists(), "the identity marker must be present in a real tenant directory"
    records = _service(api).list_documents()
    assert [r.document_id for r in records] == ["doc_real_1", "doc_real_2"]


def test_service_every_listed_id_is_readable_through_get_document(api):
    _store_many(api, 7)
    service = _service(api)
    for record in service.list_documents():
        assert service.get_document(record.document_id) == record


def _write_text(doc_dir, text):
    (doc_dir / "document_record.json").write_text(text, encoding="utf-8")


def _write_bytes(doc_dir, data):
    (doc_dir / "document_record.json").write_bytes(data)


def _set(key, value):
    def mutate(data):
        data[key] = value
    return mutate


def _drop(key):
    def mutate(data):
        del data[key]
    return mutate


# (id, corrupter, fixed reason). Every value that identifies the corruption carries SECRET so a leak is detectable.
_CORRUPTIONS = [
    pytest.param(lambda d: _write_text(d, "{not json SECRET-LIST"), "record_json_decode_error", id="invalid-json"),
    pytest.param(lambda d: _write_text(d, ""), "record_json_decode_error", id="empty-file"),
    pytest.param(lambda d: _write_bytes(d, b"\xff\xfe SECRET-LIST \xc3\x28"), "record_unreadable", id="invalid-utf8"),
    pytest.param(lambda d: _write_text(d, json.dumps(["SECRET-LIST"])), "record_not_an_object", id="json-array"),
    pytest.param(lambda d: _write_text(d, json.dumps("SECRET-LIST")), "record_not_an_object", id="json-string"),
    pytest.param(lambda d: _rewrite_record(d, _drop("page_count")), "record_field_missing", id="missing-key"),
    pytest.param(lambda d: _rewrite_record(d, _set("file_size_bytes", "SECRET-LIST")), "record_field_type_invalid", id="wrong-type-size"),
    pytest.param(lambda d: _rewrite_record(d, _set("page_count", True)), "record_field_type_invalid", id="bool-for-int"),
    pytest.param(lambda d: _rewrite_record(d, _set("claimed_filename", 12345)), "record_field_type_invalid", id="wrong-type-filename"),
    pytest.param(lambda d: _rewrite_record(d, _set("status", "SECRET-LIST")), "record_status_invalid", id="status-not-in-enum"),
    pytest.param(lambda d: _rewrite_record(d, _set("internal_state", "SECRET-LIST")), "record_internal_state_invalid", id="state-not-in-enum"),
    pytest.param(lambda d: _rewrite_record(d, _set("file_hash_sha256", "SECRET-LIST")), "record_hash_invalid", id="bad-hash"),
    pytest.param(lambda d: _rewrite_record(d, _set("created_at", "SECRET-LIST")), "record_timestamp_invalid", id="bad-created-at"),
    pytest.param(lambda d: _rewrite_record(d, _set("updated_at", "20261119T100000Z")), "record_timestamp_invalid", id="bad-updated-at"),
    pytest.param(lambda d: _rewrite_record(d, _set("error", "SECRET-LIST boom")), "record_error_invalid", id="malformed-error-string"),
    pytest.param(lambda d: _rewrite_record(d, _set("error", {"code": 5, "message": "SECRET-LIST"})), "record_error_invalid", id="malformed-error-code"),
    pytest.param(lambda d: _rewrite_record(d, _set("error", {"message": "SECRET-LIST"})), "record_error_invalid", id="malformed-error-no-code"),
    pytest.param(lambda d: _rewrite_record(d, _set("tenant_id", "tenant_beta")), "record_tenant_mismatch", id="record-claims-another-tenant"),
    pytest.param(lambda d: _rewrite_record(d, _set("tenant_id", None)), "record_tenant_mismatch", id="record-without-tenant"),
    pytest.param(lambda d: _rewrite_record(d, _set("document_id", "doc_somebody_else")), "record_doc_id_mismatch", id="record-claims-another-document"),
]


@pytest.mark.parametrize("corrupt, reason", _CORRUPTIONS)
def test_service_one_corrupt_record_fails_the_whole_listing_with_a_fixed_reason(api, corrupt, reason):
    _store_many(api, 3)
    bad_dir = _store(api, _doc("doc_corrupt", minutes=50))
    corrupt(bad_dir)
    with pytest.raises(StorageError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details == {"reason": reason}
    assert "SECRET" not in str(excinfo.value) and "SECRET" not in json.dumps(excinfo.value.details)
    assert str(api["tmp_path"]) not in str(excinfo.value)


def test_service_tenant_mismatch_is_an_isolation_fault_not_a_concealed_not_found(api):
    bad_dir = _store(api, _doc("doc_foreign"))
    _rewrite_record(bad_dir, _set("tenant_id", "tenant_beta"))
    with pytest.raises(TenantStorageIsolationError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details == {"reason": "record_tenant_mismatch"}


def test_service_symlinked_document_directory_is_refused_not_followed(api):
    tmp_path = api["tmp_path"]
    tenant_dir = _store(api, _doc("doc_seed")).parent
    decoy = _make_decoy_document(tmp_path.parent / (tmp_path.name + "_decoy_list_dir"), "tenant_alpha", "doc_decoy_dir")
    (tenant_dir / "doc_decoy_dir").symlink_to(decoy)
    with pytest.raises(TenantStorageIsolationError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details["reason"] == "document_directory_is_a_symlink"


def test_service_symlinked_record_file_is_refused_not_followed(api):
    tmp_path = api["tmp_path"]
    doc_dir = _store(api, _doc("doc_link_record"))
    decoy = _make_decoy_document(tmp_path.parent / (tmp_path.name + "_decoy_list_rec"), "tenant_alpha", "doc_link_record")
    (doc_dir / "document_record.json").unlink()
    (doc_dir / "document_record.json").symlink_to(decoy / "document_record.json")
    with pytest.raises(TenantStorageIsolationError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details["reason"] == "record_file_is_symlink"


def test_service_dangling_symlinks_are_refused_not_skipped(api):
    tenant_dir = _store(api, _doc("doc_seed")).parent
    (tenant_dir / "doc_dangling").symlink_to(tenant_dir / "no_such_target")
    with pytest.raises(TenantStorageIsolationError):
        _service(api).list_documents()


def test_service_a_file_that_looks_like_a_document_directory_is_refused(api):
    tenant_dir = _store(api, _doc("doc_seed")).parent
    (tenant_dir / "doc_is_a_file").write_text("not a directory", encoding="utf-8")
    with pytest.raises(TenantStorageIsolationError):
        _service(api).list_documents()


def test_service_symlinked_tenant_directory_is_refused_by_the_descriptor_walk_alone(api, monkeypatch):
    tmp_path = api["tmp_path"]
    outside = tmp_path.parent / (tmp_path.name + "_decoy_list_tenant")
    _make_decoy_document(outside, "tenant_walk_attack", "doc_victim")
    (tmp_path / "tenant_walk_attack").symlink_to(outside / "tenant_walk_attack")
    monkeypatch.setattr(LocalDiskStorage, "_tenant_dir", lambda self, create=False: self.base_dir / self.tenant_id)
    with pytest.raises(TenantStorageIsolationError) as excinfo:
        _service(api, "tenant_walk_attack").list_documents()
    assert excinfo.value.details["reason"] == "tenant_directory_is_a_symlink"


@pytest.mark.parametrize("kind", ["fifo", "directory", "socket"])
def test_service_special_file_at_a_record_path_is_refused_without_blocking(api, kind):
    _store(api, _doc("doc_fine"))
    doc_dir = _store(api, _doc("doc_special"))
    target = doc_dir / "document_record.json"
    target.unlink()
    held = None
    if kind == "fifo":
        os.mkfifo(str(target))
    elif kind == "directory":
        target.mkdir()
    else:
        held = socket.socket(socket.AF_UNIX)
        cwd = os.getcwd()
        os.chdir(doc_dir)                                  # AF_UNIX paths are short: bind by relative name
        try:
            held.bind("document_record.json")
        finally:
            os.chdir(cwd)
    try:
        with pytest.raises(StorageError) as excinfo:
            _call_with_timeout(lambda: _service(api).list_documents(), fifo_paths=[target] if kind == "fifo" else ())
    finally:
        if held is not None:
            held.close()
    # A FIFO or directory is refused by the fstat check; opening a socket is refused by the OS itself (ENXIO on Linux,
    # EOPNOTSUPP on macOS, which the service reports as a failed open). Either way it fails closed, never reads, never blocks.
    assert excinfo.value.details["reason"] in {"not_a_regular_file", "file_open_failed"}
    if kind != "socket":
        assert excinfo.value.details["reason"] == "not_a_regular_file"


def test_service_directory_swapped_for_a_symlink_at_open_during_the_scan_is_refused(api, monkeypatch):
    tmp_path = api["tmp_path"]
    _store(api, _doc("doc_before", minutes=1))
    doc_dir = _store(api, _doc("doc_raced", minutes=2))
    decoy = _make_decoy_document(tmp_path.parent / (tmp_path.name + "_decoy_race_dir"), "tenant_alpha", "doc_raced")
    proxy = _install_proxy(monkeypatch, before_open=_once("doc_raced", lambda: _swap_for_symlink(doc_dir, decoy)))
    baseline = _open_fd_count()
    try:
        with pytest.raises(TenantStorageIsolationError) as excinfo:
            _service(api).list_documents()
    finally:
        monkeypatch.undo()
    assert excinfo.value.details["reason"] == "document_directory_is_a_symlink"
    _assert_descriptors_clean(proxy, baseline)


def test_service_record_swapped_for_a_fifo_at_open_during_the_scan_is_refused_without_blocking(api, monkeypatch):
    _store(api, _doc("doc_before", minutes=1))
    doc_dir = _store(api, _doc("doc_raced", minutes=2))
    record_path = doc_dir / "document_record.json"
    leaf_opens = {"count": 0}

    def swap_second_record_open(path, dir_fd):
        if dir_fd is not None and path == "document_record.json":
            leaf_opens["count"] += 1
            if leaf_opens["count"] == 2:                           # doc_before is read first, doc_raced second
                _swap_for_fifo(record_path)

    proxy = _install_proxy(monkeypatch, before_open=swap_second_record_open)
    baseline = _open_fd_count()
    try:
        with pytest.raises(TenantStorageIsolationError) as excinfo:
            _call_with_timeout(lambda: _service(api).list_documents(), fifo_paths=[record_path])
    finally:
        monkeypatch.undo()
    assert excinfo.value.details["reason"] == "not_a_regular_file"
    assert leaf_opens["count"] == 2
    _assert_descriptors_clean(proxy, baseline)


def test_service_scan_ceiling_refuses_instead_of_truncating(api, monkeypatch):
    assert read_service_module._MAX_LISTED_ENTRIES == 10_000, "the provisional ceiling documented for API-015 is 10,000"
    monkeypatch.setattr(read_service_module, "_MAX_LISTED_ENTRIES", 5)
    _store_many(api, 5)
    assert len(_service(api).list_documents()) == 5                      # exactly at the ceiling is fine
    _store(api, _doc("doc_0005", minutes=5))
    with pytest.raises(DocumentListingLimitExceededError) as excinfo:
        _service(api).list_documents()                                   # one over: refused, never 5 of 6
    assert excinfo.value.details == {"reason": "document_listing_limit_exceeded"}
    assert isinstance(excinfo.value, StorageError)


def test_service_scan_ceiling_counts_only_candidate_documents_and_is_checked_before_reading(api, monkeypatch):
    monkeypatch.setattr(read_service_module, "_MAX_LISTED_ENTRIES", 3)
    tenant_dir = _store(api, _doc("doc_0001")).parent
    _store(api, _doc("doc_0002"))
    _store(api, _doc("doc_0003"))
    (tenant_dir / "idempotency").mkdir()
    for junk in ("a b", ".x1", ".x2", "n o"):
        (tenant_dir / junk).write_text("x", encoding="utf-8")
    assert len(_service(api).list_documents()) == 3                      # marker, idempotency and junk do not count
    _store(api, _doc("doc_0004"))
    proxy = _install_proxy(monkeypatch)
    with pytest.raises(DocumentListingLimitExceededError):
        _service(api).list_documents()
    leaf_opens = [e for e in proxy.events if e[0] == "transfer"]
    assert leaf_opens == [], "no record may be read once the ceiling is known to be exceeded"


class _CountingScandir:
    """Wraps a real ``os.scandir`` iterator and counts how many entries the service actually pulled from it."""

    def __init__(self, real) -> None:
        self.real = real
        self.yielded = 0

    def __enter__(self):
        self.real.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self.real.__exit__(*exc_info)

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.real)
        self.yielded += 1
        return entry


def _make_junk(directory: Path, count: int, prefix: str = ".junk_") -> None:
    """``count`` unrelated entries (empty files with names that are not valid document ids), created quickly."""
    for i in range(count):
        os.close(os.open(str(directory / f"{prefix}{i}"), os.O_CREAT | os.O_WRONLY))


def _count_scandir(monkeypatch):
    proxy = _install_proxy(monkeypatch)
    counters = []

    def counting_scandir(fd):
        counters.append(_CountingScandir(os.scandir(fd)))
        return counters[-1]

    proxy.scandir = counting_scandir
    return proxy, counters


def test_service_entry_limit_is_a_named_provisional_constant_at_least_the_candidate_limit_plus_margin():
    assert read_service_module._MAX_EXAMINED_DIRECTORY_ENTRIES == 20_000
    assert read_service_module._MAX_LISTED_ENTRIES == 10_000
    assert read_service_module._MAX_EXAMINED_DIRECTORY_ENTRIES >= read_service_module._MAX_LISTED_ENTRIES + 100
    source = inspect.getsource(read_service_module)
    constants_start = source.index("_MAX_EXAMINED_DIRECTORY_ENTRIES = 20_000")
    assert "PROVISIONAL" in source[max(0, constants_start - 1800):constants_start], "the limits must be documented as provisional"
    assert "CANDIDATE" in source[max(0, constants_start - 1800):constants_start] and "ALL directory entries" in source[max(0, constants_start - 1800):constants_start]
    assert "_MAX_EXAMINED_DIRECTORY_ENTRIES" in source.split("def _candidate_document_names")[1].split("def _read_listed_record")[0]


def test_service_enumeration_itself_is_bounded_and_stops_streaming_at_the_entry_limit(api, monkeypatch):
    monkeypatch.setattr(read_service_module, "_MAX_EXAMINED_DIRECTORY_ENTRIES", 100)
    tenant_dir = _store(api, _doc("doc_only")).parent
    _make_junk(tenant_dir, 3000)                                         # 30x the limit, none of it a document
    baseline = _open_fd_count()
    proxy, counters = _count_scandir(monkeypatch)
    with pytest.raises(DocumentListingLimitExceededError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details == {"reason": "directory_entry_limit_exceeded"}
    assert isinstance(excinfo.value, StorageError)
    (counter,) = counters
    assert counter.yielded == 101, f"enumeration pulled {counter.yielded} entries instead of stopping at limit + 1"
    assert [e for e in proxy.events if e[0] == "transfer"] == [], "no record may be read after the limit is exceeded"
    _assert_descriptors_clean(proxy, baseline)


def test_service_entry_limit_boundary_is_exact_and_counts_every_kind_of_entry(api, monkeypatch):
    tenant_dir = _store(api, _doc("doc_0001", minutes=1)).parent
    _store(api, _doc("doc_0002", minutes=2))
    (tenant_dir / "idempotency").mkdir()
    _make_junk(tenant_dir, 7)
    total = len(os.listdir(tenant_dir))                                   # documents + marker + idempotency + junk
    assert total == 2 + 1 + 1 + 7
    monkeypatch.setattr(read_service_module, "_MAX_EXAMINED_DIRECTORY_ENTRIES", total)
    assert [r.document_id for r in _service(api).list_documents()] == ["doc_0001", "doc_0002"]   # exactly at the limit
    monkeypatch.setattr(read_service_module, "_MAX_EXAMINED_DIRECTORY_ENTRIES", total - 1)
    with pytest.raises(DocumentListingLimitExceededError) as excinfo:
        _service(api).list_documents()                                    # one over: refused, never a partial list
    assert excinfo.value.details == {"reason": "directory_entry_limit_exceeded"}


def test_service_the_two_limits_are_independent_and_report_distinct_fixed_reasons(api, monkeypatch):
    tenant_dir = _store(api, _doc("doc_0001")).parent
    _store(api, _doc("doc_0002"))
    _store(api, _doc("doc_0003"))
    (tenant_dir / "idempotency").mkdir()
    _make_junk(tenant_dir, 5)                                             # 3 candidates, 10 entries (with the marker)
    monkeypatch.setattr(read_service_module, "_MAX_LISTED_ENTRIES", 3)
    monkeypatch.setattr(read_service_module, "_MAX_EXAMINED_DIRECTORY_ENTRIES", 10)
    assert len(_service(api).list_documents()) == 3                       # both limits met exactly
    _make_junk(tenant_dir, 1, prefix=".more_")                            # entries 11 > 10, candidates still 3
    with pytest.raises(DocumentListingLimitExceededError) as entries:
        _service(api).list_documents()
    assert entries.value.details == {"reason": "directory_entry_limit_exceeded"}
    os.unlink(tenant_dir / ".more_0")
    _store(api, _doc("doc_0004"))                                         # 4 candidates > 3, entries 11 > 10 again
    monkeypatch.setattr(read_service_module, "_MAX_EXAMINED_DIRECTORY_ENTRIES", 1000)
    with pytest.raises(DocumentListingLimitExceededError) as candidates:
        _service(api).list_documents()
    assert candidates.value.details == {"reason": "document_listing_limit_exceeded"}


def test_service_entry_limit_with_the_real_constant_refuses_instead_of_listing_a_huge_directory(api):
    limit = read_service_module._MAX_EXAMINED_DIRECTORY_ENTRIES
    tenant_dir = _store(api, _doc("doc_only")).parent
    (tenant_dir / "idempotency").mkdir()
    existing = len(os.listdir(tenant_dir))                                # the document, the marker, idempotency
    _make_junk(tenant_dir, limit - existing)
    assert len(os.listdir(tenant_dir)) == limit
    assert [r.document_id for r in _service(api).list_documents()] == ["doc_only"]      # exactly 20,000 entries: fine
    _make_junk(tenant_dir, 1, prefix=".one_too_many_")
    with pytest.raises(DocumentListingLimitExceededError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details == {"reason": "directory_entry_limit_exceeded"}


def test_route_entry_limit_is_a_generic_500_with_a_fixed_reason_and_no_partial_body(api, monkeypatch, captured_logs):
    monkeypatch.setattr(read_service_module, "_MAX_EXAMINED_DIRECTORY_ENTRIES", 20)
    tenant_dir = _store(api, _doc("doc_visible")).parent
    _make_junk(tenant_dir, 50)
    response = _list(api)
    _assert_generic_500(response, api["tmp_path"], "doc_visible", ".junk_", "tenant_alpha")
    assert set(response.json()) == {"error"}
    joined = "\n".join(captured_logs.messages)
    assert "reason=directory_entry_limit_exceeded" in joined
    for leak in ("doc_visible", ".junk_", "tenant_alpha", str(api["tmp_path"])):
        assert leak not in joined


def test_service_descriptors_are_clean_on_success_failure_empty_and_ceiling(api, monkeypatch):
    _store_many(api, 4)
    api["client"].get(LIST_URL, headers=ALPHA_HEADERS)                    # create lazily built long-lived resources first
    baseline = _open_fd_count()

    proxy = _install_proxy(monkeypatch)
    assert len(_service(api).list_documents()) == 4
    assert len([e for e in proxy.events if e[0] == "transfer"]) == 4, "each record descriptor must go to a file object"
    _assert_descriptors_clean(proxy, baseline)

    proxy = _install_proxy(monkeypatch)
    assert _service(api, "tenant_never_provisioned").list_documents() == []
    _assert_descriptors_clean(proxy, baseline)

    bad_dir = _store(api, _doc("doc_bad", minutes=90))
    _write_bytes(bad_dir, b"\xff\xfe\xfd")
    proxy = _install_proxy(monkeypatch)
    with pytest.raises(StorageError):
        _service(api).list_documents()
    _assert_descriptors_clean(proxy, baseline)

    _write_text(bad_dir, "{}")
    proxy = _install_proxy(monkeypatch)
    with pytest.raises(StorageError):
        _service(api).list_documents()                                    # decodes, then fails validation after the close
    _assert_descriptors_clean(proxy, baseline)

    monkeypatch.setattr(read_service_module, "_MAX_LISTED_ENTRIES", 2)
    proxy = _install_proxy(monkeypatch)
    with pytest.raises(DocumentListingLimitExceededError):
        _service(api).list_documents()
    _assert_descriptors_clean(proxy, baseline)


@pytest.mark.parametrize("hidden", [("O_NOFOLLOW",), ("O_DIRECTORY",), ("O_NONBLOCK",), ("supports_dir_fd",)])
def test_service_platform_without_the_required_capabilities_fails_closed_before_any_access(api, monkeypatch, hidden):
    _store(api, _doc("doc_fine"))
    proxy = _install_proxy(monkeypatch, hidden=hidden)
    with pytest.raises(ComponentConfigurationError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details == {"reason": "platform_unsupported"}
    assert proxy.open_calls == 0


def test_service_read_failure_after_the_descriptor_transfer_is_a_typed_fault_without_a_double_close(api, monkeypatch):
    _store_many(api, 2)
    api["client"].get(LIST_URL, headers=ALPHA_HEADERS)
    baseline = _open_fd_count()
    proxy = _install_proxy(monkeypatch, fail_page_reads=False)

    class _FailingText:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            self.inner.close()
            return False

        def read(self):
            raise OSError(5, "simulated read failure")

        def close(self):
            self.inner.close()

        @property
        def closed(self):
            return self.inner.closed

    original = proxy._fdopen
    proxy.fdopen = lambda fd, *a, **k: _FailingText(original(fd, *a, **k))
    with pytest.raises(StorageError) as excinfo:
        _service(api).list_documents()
    assert excinfo.value.details == {"reason": "record_unreadable"}
    problems, leaked, _ = proxy.ownership_report()
    assert problems == [] and leaked == []
    monkeypatch.undo()
    assert _open_fd_count() == baseline


def test_service_log_calls_added_for_listing_use_only_constant_messages():
    tree = ast.parse(inspect.getsource(read_service_module))
    listing_logs = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger" and isinstance(node.args[0], ast.Constant)
                and ("listing" in node.args[0].value.lower() or "Listed" in node.args[0].value)):
            listing_logs.append(node)
            assert len(node.args) <= 2, "a listing log call may carry at most an integer errno"
    assert len(listing_logs) >= 3


# ============================================================================
# 3. The route: contract, filters, pagination, tenant isolation
# ============================================================================

def test_route_is_synchronous_and_the_endpoint_is_documented_in_openapi(api):
    assert not inspect.iscoroutinefunction(get_document_list)
    schema = api["client"].get("/openapi.json").json()
    operation = schema["paths"]["/api/v1/documents"]["get"]
    names = {p["name"] for p in operation["parameters"]}
    assert {"page", "size", "status", "from", "to", "q", "sort", "order", "documentType", "confidenceBelow"} <= names
    assert "tenant_id" not in names and "X-Tenant-ID" not in names
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("/DocumentListResponse")
    assert {"401", "403", "422", "500"} <= set(operation["responses"])
    assert "/api/v1/documents/{document_id}" in schema["paths"]            # no route conflict with API-002
    assert "/api/v1/documents/{document_id}/pages/{page_number}" in schema["paths"]


def test_route_requires_authentication_and_the_documents_read_scope(api):
    _store_many(api, 2)
    assert SCOPE_DOCUMENTS_READ == "documents:read"
    missing = api["client"].get(LIST_URL)
    assert missing.status_code == 401 and missing.json()["error"]["code"] in ("AUTHENTICATION_REQUIRED", "UNAUTHENTICATED")
    invalid = api["client"].get(LIST_URL, headers={"Authorization": "Bearer not-a-token"})
    assert invalid.status_code == 401
    wrong_scope = api["client"].get(LIST_URL, headers=WRITER_ONLY_HEADERS)
    assert wrong_scope.status_code == 403 and wrong_scope.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    assert "doc_0000" not in wrong_scope.text
    # authentication and scope are decided before the parameters are judged
    assert api["client"].get(LIST_URL, params={"page": "0", "documentType": "x"}).status_code == 401
    assert api["client"].get(LIST_URL, params={"page": "0"}, headers=WRITER_ONLY_HEADERS).status_code == 403
    assert api["client"].get(LIST_URL, headers=ALPHA_HEADERS).status_code == 200


def test_route_default_response_shape_headers_and_row_projection(api):
    _store(api, _doc("doc_one", minutes=5, claimed_filename="invoice.pdf", page_count=4, file_size_bytes=999))
    response = _list(api)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["X-Request-ID"] and response.headers["Content-Type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"content", "stats", "paging"}
    assert body["paging"] == {"page": 1, "size": 10, "total_items": 1, "total_pages": 1}
    assert body["stats"] == {"total": 1, "processing": 1, "needs_review": 0, "auto_approved": 0, "failed": 0}
    (row,) = body["content"]
    assert set(row) == ROW_KEYS
    assert row["document_id"] == "doc_one" and row["filename"] == "invoice.pdf"
    assert row["page_count"] == 4 and row["file_size_bytes"] == 999 and row["status"] == "processing"
    assert row["sha256"] == "ab" * 32 and row["error_code"] is None and row["error_message"] is None
    for forbidden in ("tenant_id", "internal_state", "idempotency_key", "result", "document_type", "confidence", "storage_uri"):
        assert forbidden not in row
    assert "tenant_alpha" not in response.text and str(api["tmp_path"]) not in response.text


def test_route_empty_tenant_is_200_with_empty_content_zero_stats_and_creates_no_directory(api):
    _store(api, _doc("doc_other", "tenant_beta"))
    before = _tree(api["tmp_path"])
    response = _list(api, FRESH_HEADERS)
    assert response.status_code == 200
    assert response.json() == {
        "content": [],
        "stats": {"total": 0, "processing": 0, "needs_review": 0, "auto_approved": 0, "failed": 0},
        "paging": {"page": 1, "size": 10, "total_items": 0, "total_pages": 0},
    }
    assert _tree(api["tmp_path"]) == before, "a read must never create the tenant directory"
    still_empty = _list(api, ALPHA_HEADERS)                    # alpha has nothing either, and sees nothing of beta
    assert still_empty.json()["content"] == [] and _tree(api["tmp_path"]) == before


def test_route_tenant_isolation_with_interleaved_documents(api):
    for i in range(8):
        _store(api, _doc(f"doc_alpha_{i}", "tenant_alpha", minutes=2 * i, claimed_filename=f"ALPHA-{i}.pdf"))
        _store(api, _doc(f"doc_beta_{i}", "tenant_beta", minutes=2 * i + 1, claimed_filename=f"BETA-{i}.pdf"))
    alpha = _list(api, ALPHA_HEADERS, size=100)
    beta = _list(api, BETA_HEADERS, size=100)
    assert sorted(_ids(alpha)) == sorted(f"doc_alpha_{i}" for i in range(8))
    assert sorted(_ids(beta)) == sorted(f"doc_beta_{i}" for i in range(8))
    assert "BETA" not in alpha.text and "doc_beta" not in alpha.text
    assert "ALPHA" not in beta.text and "doc_alpha" not in beta.text
    assert alpha.json()["stats"]["total"] == 8 and beta.json()["stats"]["total"] == 8


def test_route_ignores_tenant_headers_and_rejects_tenant_query_parameters(api):
    _store_many(api, 3, "tenant_alpha")
    _store_many(api, 4, "tenant_beta", prefix="doc_b")
    baseline = _list(api, ALPHA_HEADERS, size=100).json()
    for headers in (
        {"X-Tenant-ID": "tenant_beta"},
        {"X-Tenant": "tenant_beta", "Tenant-Id": "tenant_beta"},
    ):
        response = api["client"].get(LIST_URL, params={"size": 100}, headers={**ALPHA_HEADERS, **headers})
        assert response.status_code == 200
        assert response.json() == baseline, headers                   # a tenant header is never read
    for name in ("tenant_id", "tenantId", "tenant", "TENANT_ID"):
        response = api["client"].get(LIST_URL, params={"size": 100, name: "tenant_beta"}, headers=ALPHA_HEADERS)
        _assert_unknown_parameter(response)                           # a tenant query parameter is refused, never honored
        assert "tenant_beta" not in response.text and "doc_b0000" not in response.text
    assert baseline["stats"]["total"] == 3 and all(r["document_id"].startswith("doc_0") for r in baseline["content"])


def test_route_hashed_tenant_identity_lists_only_its_own_documents(api):
    hashed = "Tenant.Mixed@Case"
    api["app"].state.auth_provider.principals["token-hashed"] = ApiPrincipal(
        principal_id="hashed_user", tenant_id=hashed, scopes={SCOPE_DOCUMENTS_READ}, is_active=True
    )
    _store_many(api, 3, hashed, prefix="doc_h")
    _store_many(api, 2, "tenant_alpha", prefix="doc_a")
    response = _list(api, {"Authorization": "Bearer token-hashed"}, size=100)
    assert response.status_code == 200
    assert sorted(_ids(response)) == ["doc_h0000", "doc_h0001", "doc_h0002"]
    assert "h+" not in response.text and hashed not in response.text and str(api["tmp_path"]) not in response.text


def test_route_default_page_size_is_ten_and_pages_are_disjoint_and_complete(api):
    _store_many(api, 25)
    first = _list(api).json()
    assert len(first["content"]) == 10 and first["paging"] == {"page": 1, "size": 10, "total_items": 25, "total_pages": 3}
    seen = []
    for page in (1, 2, 3):
        body = _list(api, page=page).json()
        assert body["paging"]["total_items"] == 25 and body["paging"]["page"] == page
        seen += [r["document_id"] for r in body["content"]]
    assert seen == [f"doc_{i:04d}" for i in range(24, -1, -1)]               # newest first, every record exactly once
    assert len(set(seen)) == 25
    assert [len(_list(api, page=p).json()["content"]) for p in (1, 2, 3)] == [10, 10, 5]


@pytest.mark.parametrize("count, size, pages", [(0, 5, 0), (1, 5, 1), (5, 5, 1), (6, 5, 2), (10, 5, 2), (11, 5, 3)])
def test_route_totals_at_zero_one_exact_and_page_size_plus_one(api, count, size, pages):
    _store_many(api, count)
    body = _list(api, size=size).json()
    assert body["paging"] == {"page": 1, "size": size, "total_items": count, "total_pages": pages}
    assert len(body["content"]) == min(count, size)
    assert body["stats"]["total"] == count


def test_route_out_of_range_page_is_200_empty_with_the_true_totals(api):
    _store_many(api, 12)
    for page in (4, 99, 10 ** 12, 10 ** 30):
        response = _list(api, page=page, size=5)
        assert response.status_code == 200
        body = response.json()
        assert body["content"] == []
        assert body["paging"] == {"page": page, "size": 5, "total_items": 12, "total_pages": 3}
        assert body["stats"]["total"] == 12


@pytest.mark.parametrize("params, parameter", [
    ({"page": "0"}, "page"), ({"page": "-1"}, "page"), ({"page": "abc"}, "page"), ({"page": "1.5"}, "page"), ({"page": ""}, "page"),
    ({"size": "0"}, "size"), ({"size": "-3"}, "size"), ({"size": "101"}, "size"), ({"size": "x"}, "size"), ({"size": ""}, "size"),
])
def test_route_invalid_page_and_size_are_422_validation_error(api, params, parameter):
    _store_many(api, 2)
    _assert_validation_error(_list(api, **params), parameter)


def test_route_size_bounds_are_accepted_at_one_and_the_maximum(api):
    _store_many(api, 3)
    assert _list(api, size=1).json()["paging"]["total_pages"] == 3
    assert _list(api, size=100).json()["paging"]["size"] == 100


def test_route_ordering_is_deterministic_for_tied_timestamps(api):
    for doc_id in ("doc_d", "doc_b", "doc_a", "doc_c"):
        _store(api, _doc(doc_id, minutes=7))                       # one identical created_at for all four
    _store(api, _doc("doc_newer", minutes=9))
    _store(api, _doc("doc_older", minutes=1))
    descending = _ids(_list(api))
    assert descending == ["doc_newer", "doc_a", "doc_b", "doc_c", "doc_d", "doc_older"]
    assert _ids(_list(api, order="asc")) == ["doc_older", "doc_a", "doc_b", "doc_c", "doc_d", "doc_newer"]
    pieces = []
    for page in (1, 2, 3):
        pieces += _ids(_list(api, size=2, page=page))
    assert pieces == descending                                     # paging walks the same total order
    assert _ids(_list(api)) == descending                           # repeatable


@pytest.mark.parametrize("field, order, expected", [
    ("created_at", "desc", ["doc_d", "doc_a", "doc_b", "doc_c"]),
    ("document_id", "desc", ["doc_d", "doc_c", "doc_b", "doc_a"]),
    ("page_count", "asc", ["doc_b", "doc_d", "doc_a", "doc_c"]),
    ("file_size_bytes", "desc", ["doc_d", "doc_a", "doc_c", "doc_b"]),
    ("status", "asc", ["doc_d", "doc_b", "doc_c", "doc_a"]),
])
def test_route_sort_fields_and_directions(api, field, order, expected):
    for record in _SORT_FIXTURE:
        _store(api, record)
    assert _ids(_list(api, sort=field, order=order)) == expected


@pytest.mark.parametrize("sort", ["updated_at", "filename", "sha256", "documentType", "confidence", "mime_type", "-created_at", "", "CREATED_AT"])
def test_route_unsupported_sort_field_is_422(api, sort):
    _store_many(api, 2)
    _assert_validation_error(_list(api, sort=sort), "sort")


@pytest.mark.parametrize("order", ["ascending", "up", "", "DESC", "1"])
def test_route_invalid_order_is_422(api, order):
    _assert_validation_error(_list(api, order=order), "order")


def test_route_new_document_between_requests_can_shift_a_page_boundary_there_is_no_snapshot(api):
    """Documented limitation: offset paging keeps no snapshot between requests."""
    _store_many(api, 12)
    page_one = _list(api, size=5, page=1).json()
    assert [r["document_id"] for r in page_one["content"]] == ["doc_0011", "doc_0010", "doc_0009", "doc_0008", "doc_0007"]
    _store(api, _doc("doc_0099", minutes=500))                       # a newer document arrives between the two requests
    page_two = _list(api, size=5, page=2).json()
    ids_two = [r["document_id"] for r in page_two["content"]]
    assert ids_two[0] == "doc_0007", "the record that ended page one now starts page two (shifted boundary)"
    assert "doc_0099" not in ids_two                                  # and the new document was never on page two
    assert page_two["paging"]["total_items"] == 13 and page_two["paging"]["total_pages"] == 3
    assert page_one["paging"]["total_items"] == 12
    fresh_one = _list(api, size=5, page=1).json()
    assert fresh_one["content"][0]["document_id"] == "doc_0099"
    assert fresh_one["stats"]["total"] == 13


# ---- filters ---------------------------------------------------------------------------------------------------------

def test_route_status_filter_validates_the_enum_and_matches_the_persisted_status(api):
    _store(api, _doc("doc_p1", minutes=1))
    _store(api, _doc("doc_p2", minutes=2))
    _store(api, _doc("doc_f1", minutes=3, status=DocumentStatus.FAILED, internal_state=IngestionState.FAILED))
    processing = _list(api, status="processing").json()
    assert sorted(r["document_id"] for r in processing["content"]) == ["doc_p1", "doc_p2"]
    assert processing["paging"]["total_items"] == 2
    failed = _list(api, status="failed").json()
    assert [r["document_id"] for r in failed["content"]] == ["doc_f1"]
    for valid_but_absent in ("needs_review", "auto_approved", "reviewed", "rejected"):
        body = _list(api, status=valid_but_absent).json()
        assert body["content"] == [] and body["paging"] == {"page": 1, "size": 10, "total_items": 0, "total_pages": 0}
        assert body["stats"]["total"] == 3
    for invalid in ("queued", "rejected_malware", "PROCESSING", "done", "", "processing,failed"):
        _assert_validation_error(_list(api, status=invalid), "status")


def test_route_stats_are_whole_tenant_persisted_status_counts_ignoring_every_filter(api):
    _store(api, _doc("doc_p1", minutes=1))
    _store(api, _doc("doc_p2", minutes=2))
    _store(api, _doc("doc_n1", minutes=3, status=DocumentStatus.NEEDS_REVIEW))
    _store(api, _doc("doc_a1", minutes=4, status=DocumentStatus.AUTO_APPROVED))
    _store(api, _doc("doc_f1", minutes=5, status=DocumentStatus.FAILED, internal_state=IngestionState.FAILED))
    _store(api, _doc("doc_r1", minutes=6, status=DocumentStatus.REVIEWED))
    _store(api, _doc("doc_x1", minutes=7, status=DocumentStatus.REJECTED, internal_state=IngestionState.REJECTED_UNSUPPORTED))
    _store(api, _doc("doc_beta", "tenant_beta", minutes=8))
    expected = {"total": 7, "processing": 2, "needs_review": 1, "auto_approved": 1, "failed": 1}
    for params in ({}, {"status": "failed"}, {"q": "doc_p"}, {"from": "2030-01-01"}, {"page": 5, "size": 2},
                   {"status": "needs_review", "q": "zzz"}, {"sort": "status", "order": "asc"}):
        body = _list(api, **params).json()
        assert body["stats"] == expected, params
    assert sum(v for k, v in expected.items() if k != "total") != expected["total"]


def test_route_current_module_a_records_give_processing_only_statistics(api):
    _store_many(api, 4)
    stats = _list(api).json()["stats"]
    assert stats == {"total": 4, "processing": 4, "needs_review": 0, "auto_approved": 0, "failed": 0}


def test_route_date_range_is_inclusive_on_created_at_and_compares_instants(api):
    for record in _INSTANT_FIXTURE:
        _store(api, record)
    def ids(**params):
        return sorted(_ids(_list(api, size=100, **params)))
    assert ids(**{"from": "2026-11-19T10:00:00Z", "to": "2026-11-19T10:00:00Z"}) == ["doc_r1", "doc_r2", "doc_r3"]
    assert ids(**{"from": "2026-11-19T10:00:00+00:00", "to": "2026-11-19T10:00:00+00:00"}) == ["doc_r1", "doc_r2", "doc_r3"]
    assert ids(**{"from": "2026-11-19T12:00:00+02:00", "to": "2026-11-19T12:00:00+02:00"}) == ["doc_r1", "doc_r2", "doc_r3"]
    assert ids(**{"from": "2026-11-19T10:00:00", "to": "2026-11-19T10:00:00"}) == ["doc_r1", "doc_r2", "doc_r3"]    # naive = UTC
    assert ids(**{"from": "2026-11-19T10:00:00.000001Z", "to": "2026-11-19T10:00:00.000001Z"}) == ["doc_r5"]
    assert ids(**{"to": "2026-11-19T09:59:59.999999Z"}) == ["doc_r4"]
    assert ids(**{"from": "2026-11-19T23:59:59.999999Z", "to": "2026-11-20T00:00:00Z"}) == ["doc_r6", "doc_r7"]


def test_route_date_only_to_includes_that_whole_utc_day(api):
    for record in _INSTANT_FIXTURE:
        _store(api, record)
    def ids(**params):
        return sorted(_ids(_list(api, size=100, **params)))
    day = ids(**{"from": "2026-11-19", "to": "2026-11-19"})
    assert day == ["doc_r1", "doc_r2", "doc_r3", "doc_r4", "doc_r5", "doc_r7"]
    assert ids(**{"to": "2026-11-19"}) == day
    assert ids(**{"from": "2026-11-20"}) == ["doc_r6", "doc_r8"]
    assert ids(**{"from": "2026-11-19T10:00:00Z", "to": "2026-11-19"}) == ["doc_r1", "doc_r2", "doc_r3", "doc_r5", "doc_r7"]


def test_route_offset_plus_sign_must_be_percent_encoded_and_a_raw_plus_is_a_422_not_a_misparse(api):
    _store_many(api, 2)
    encoded = api["client"].get(LIST_URL + "?from=2026-11-01T08%3A00%3A00%2B00%3A00", headers=ALPHA_HEADERS)
    assert encoded.status_code == 200 and len(encoded.json()["content"]) == 2
    raw_plus = api["client"].get(LIST_URL + "?from=2026-11-01T08:00:00+00:00", headers=ALPHA_HEADERS)
    _assert_validation_error(raw_plus, "from")


@pytest.mark.parametrize("name", ["from", "to"])
@pytest.mark.parametrize("value", ["garbage", "1700000000", "2026-13-01", "2026-02-30", "20261119", "2026-11-19T24:00:00",
                                   "2026-11-19T10:00:00+25:00", "SECRET-DATE-VALUE", " 2026-11-19", ""])
def test_route_invalid_dates_are_422_and_the_value_is_never_echoed(api, name, value):
    _store_many(api, 2)
    response = _list(api, **{name: value})
    _assert_validation_error(response, name, None, "SECRET-DATE-VALUE", "garbage", "1700000000")


@pytest.mark.parametrize("params", [
    {"from": "2026-11-20", "to": "2026-11-19"},
    {"from": "2026-11-19T10:00:01Z", "to": "2026-11-19T10:00:00Z"},
    {"from": "2026-11-19T12:00:00+00:00", "to": "2026-11-19T12:00:00+02:00"},     # to is two hours EARLIER as an instant
])
def test_route_reversed_date_range_is_422(api, params):
    _store_many(api, 2)
    _assert_validation_error(_list(api, **params), "from", "invalid_range")


def test_route_equal_from_and_to_is_not_reversed(api):
    _store_many(api, 3)
    exact = _stamp(1)
    body = _list(api, **{"from": exact, "to": exact}).json()
    assert [r["document_id"] for r in body["content"]] == ["doc_0001"]


def test_route_q_is_a_case_sensitive_document_id_prefix(api):
    # (ids that differ only by case would be ONE directory on a case-insensitive filesystem, so none are stored here)
    for doc_id in ("doc_Abc", "doc_abd", "doc_abe", "xdoc_abd", "other_1"):
        _store(api, _doc(doc_id))
    assert sorted(_ids(_list(api, q="doc_a"))) == ["doc_abd", "doc_abe"]
    assert _ids(_list(api, q="doc_A")) == ["doc_Abc"]
    assert _ids(_list(api, q="DOC_")) == []
    assert _ids(_list(api, q="abd")) == []                            # a substring is not a prefix
    assert _ids(_list(api, q="doc_abd")) == ["doc_abd"]
    nothing = _list(api, q="doc_abdz").json()
    assert nothing["content"] == [] and nothing["paging"]["total_items"] == 0 and nothing["stats"]["total"] == 5


def test_route_q_does_not_search_filenames_or_hashes(api):
    _store(api, _doc("doc_x1", claimed_filename="needle-report.pdf"))
    assert _ids(_list(api, q="needle")) == []
    assert _ids(_list(api, q="ab")) == []                              # the shared test hash starts with "ab"


@pytest.mark.parametrize("q", ["", "x" * 129])
def test_route_q_length_bounds_are_422(api, q):
    _assert_validation_error(_list(api, q=q), "q")


@pytest.mark.parametrize("q", ["../etc", "%2e%2e", "a/b", "doc\x00x", "' OR 1=1 --", "doc_*", "x" * 128])
def test_route_q_with_unusual_characters_is_just_a_prefix_that_matches_nothing(api, q):
    _store_many(api, 2)
    response = _list(api, q=q)
    assert response.status_code == 200 and response.json()["content"] == []
    assert str(api["tmp_path"]) not in response.text


@pytest.mark.parametrize("name", ["documentType", "confidenceBelow"])
@pytest.mark.parametrize("value", ["INVOICE", "0.8", "", "SECRET-UNSUPPORTED", "0"])
def test_route_unsupported_filters_are_rejected_not_silently_ignored(api, name, value):
    _store_many(api, 3)
    response = _list(api, **{name: value})
    _assert_validation_error(response, name, "unsupported_filter", "SECRET-UNSUPPORTED", "INVOICE")
    assert response.json()["error"]["details"]["validation_errors"][0]["message"].startswith("This filter is not supported")
    assert "content" not in response.json()


def test_route_unsupported_filters_are_rejected_even_next_to_valid_ones_and_before_storage_is_read(api, monkeypatch):
    _store_many(api, 3)
    calls = []
    monkeypatch.setattr(DocumentReadService, "list_documents", lambda self: calls.append(1) or [])
    _assert_validation_error(_list(api, status="processing", q="doc", documentType="X"), "documentType")
    _assert_validation_error(_list(api, confidenceBelow="0.5", size=5), "confidenceBelow")
    _assert_validation_error(_list(api, **{"from": "2026-11-20", "to": "2026-11-19"}), "from")
    _assert_validation_error(_list(api, **{"from": "garbage"}), "from")
    assert calls == [], "invalid parameters must be refused before any storage access"
    assert _list(api).status_code == 200 and calls == [1]


_UNKNOWN_NAMES = [
    "stauts", "Status", "PAGE", "Size", "FROM", "To", "Q", "Sort", "ORDER", "limit", "offset", "tenant_id", "tenantId",
    "sort_by", "order_by", "search", "document_type", "documenttype", "confidence_below", "confidencebelow",
    "from_date", "created_from", "_", "a", "x" * 200, "státus", "%00", "a b", "a.b", "a[0]", "a[]", "-", "status ",
    " status", "page\n",
]


@pytest.mark.parametrize("name", _UNKNOWN_NAMES)
def test_route_unknown_query_parameter_name_is_422_and_nothing_is_reflected(api, name):
    _store_many(api, 3)
    response = api["client"].get(LIST_URL, params=[(name, "SECRET-PARAM-VALUE")], headers=ALPHA_HEADERS)
    _assert_unknown_parameter(response, "SECRET-PARAM-VALUE")


def test_route_unknown_parameter_error_is_identical_for_every_name_and_value(api):
    """Byte-for-byte the same body (apart from the request id), so no user-supplied text can be reflected."""
    _store_many(api, 2)
    bodies = set()
    for name, value in [(n, "SECRET" if i % 2 else "processing") for i, n in enumerate(_UNKNOWN_NAMES)] + [("%00", "<script>")]:
        response = api["client"].get(LIST_URL, params=[(name, value)], headers=ALPHA_HEADERS)
        bodies.add(json.dumps(_assert_unknown_parameter(response), sort_keys=True))
    assert len(bodies) == 1, "the error body varies with what the client sent, so something is reflected"


@pytest.mark.parametrize("query", [
    "?stauts=", "?stauts", "?=x", "?=", "?&", "?stauts=&status=processing", "?status=processing&stauts=", "?page=1&size=5&foo",
    "?status=processing&stauts=processing", "?stauts=1&stauts=2", "?q=doc&unknown=", "?sort=created_at&order=desc&x=",
])
def test_route_unknown_names_with_empty_or_missing_values_are_422(api, query):
    _store_many(api, 3)
    response = api["client"].get(LIST_URL + query, headers=ALPHA_HEADERS)
    if query in ("?&",):                       # no parameter name at all: nothing to reject
        assert response.status_code == 200
        return
    _assert_unknown_parameter(response)


def test_route_unknown_names_are_refused_before_any_storage_access(api, monkeypatch):
    _store_many(api, 3)
    calls = []
    monkeypatch.setattr(DocumentReadService, "list_documents", lambda self: calls.append(1) or [])
    for query in ("?stauts=processing", "?status=processing&limit=5", "?documentType=X&stauts=1"):
        assert api["client"].get(LIST_URL + query, headers=ALPHA_HEADERS).status_code == 422
    assert calls == []
    assert api["client"].get(LIST_URL + "?status=processing", headers=ALPHA_HEADERS).status_code == 200 and calls == [1]


def test_route_unknown_parameter_does_not_change_authentication_and_authorization_ordering(api):
    _store_many(api, 2)
    unknown = [("stauts", "SECRET-PARAM-VALUE")]
    assert api["client"].get(LIST_URL, params=unknown).status_code == 401
    assert api["client"].get(LIST_URL, params=unknown, headers={"Authorization": "Bearer not-a-token"}).status_code == 401
    forbidden = api["client"].get(LIST_URL, params=unknown, headers=WRITER_ONLY_HEADERS)
    assert forbidden.status_code == 403 and forbidden.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    assert "SECRET-PARAM-VALUE" not in forbidden.text
    assert api["client"].get(LIST_URL, params=unknown, headers=ALPHA_HEADERS).status_code == 422
    assert api["client"].get(LIST_URL, headers=ALPHA_HEADERS).status_code == 200


def test_route_unknown_parameter_never_reaches_the_logs(api, captured_logs):
    _store_many(api, 2)
    _assert_unknown_parameter(api["client"].get(LIST_URL, params=[("stauts-LOGNAME", "SECRET-PARAM-VALUE")], headers=ALPHA_HEADERS))
    joined = "\n".join(captured_logs.messages)
    assert "SECRET-PARAM-VALUE" not in joined and "stauts-LOGNAME" not in joined


def test_route_recognized_names_are_exactly_the_documented_set_and_each_is_accepted(api):
    _store_many(api, 3)
    assert LIST_QUERY_PARAMETERS == {"page", "size", "status", "from", "to", "q", "sort", "order", "documentType", "confidenceBelow"}
    operation = api["client"].get("/openapi.json").json()["paths"]["/api/v1/documents"]["get"]
    query_names = {p["name"] for p in operation["parameters"] if p["in"] == "query"}
    assert query_names == set(LIST_QUERY_PARAMETERS), "OpenAPI and the recognized set drifted"
    accepted = {
        "page": "1", "size": "5", "status": "processing", "from": "2026-01-01", "to": "2026-12-31", "q": "doc",
        "sort": "document_id", "order": "asc",
    }
    for name, value in accepted.items():
        response = api["client"].get(LIST_URL, params=[(name, value)], headers=ALPHA_HEADERS)
        assert response.status_code == 200, name
    everything = api["client"].get(LIST_URL, params=accepted, headers=ALPHA_HEADERS)
    assert everything.status_code == 200 and everything.json()["paging"]["total_items"] == 3


@pytest.mark.parametrize("name", ["documentType", "confidenceBelow"])
@pytest.mark.parametrize("query_value", ["", "x", "SECRET-UNSUPPORTED"])
def test_route_the_two_unsupported_filters_stay_explicitly_rejected_as_unsupported_not_unknown(api, name, query_value):
    _store_many(api, 2)
    response = api["client"].get(LIST_URL, params=[(name, query_value)], headers=ALPHA_HEADERS)
    _assert_validation_error(response, name, "unsupported_filter", "SECRET-UNSUPPORTED")
    mixed = api["client"].get(LIST_URL, params=[(name, query_value), ("stauts", "x")], headers=ALPHA_HEADERS)
    assert mixed.status_code == 422 and "SECRET-UNSUPPORTED" not in mixed.text
    assert api["client"].get(LIST_URL + f"?{name}", headers=ALPHA_HEADERS).json()["error"]["details"]["validation_errors"][0]["type"] == "unsupported_filter"


def test_route_combined_filters_sort_and_paging(api):
    _store_many(api, 30, page_count=2)
    for i in range(30, 40):
        _store(api, _doc(f"doc_{i:04d}", minutes=i, page_count=9))
    response = _list(api, q="doc_003", sort="document_id", order="asc", size=4, page=2,
                     **{"from": _stamp(30), "to": _stamp(36)})
    body = response.json()
    assert [r["document_id"] for r in body["content"]] == ["doc_0034", "doc_0035", "doc_0036"]
    assert body["paging"] == {"page": 2, "size": 4, "total_items": 7, "total_pages": 2}
    assert body["stats"]["total"] == 40


# ---- cross-endpoint consistency and sanitization ------------------------------------------------------------------------

def test_route_every_listed_document_is_served_by_api002_with_identical_public_metadata(api):
    _store(api, _doc("doc_plain", minutes=1))
    _store(api, _doc("doc_ts_z", minutes=2, created_at="2026-11-19T10:00:00Z", updated_at="2026-11-19T10:00:00Z"))
    _store(api, _doc("doc_ts_naive", minutes=3, created_at="2026-11-19T10:00:00", updated_at="2026-11-19T11:30:00"))
    _store(api, _doc("doc_ts_offset", minutes=4, created_at="2026-11-19T12:00:00+02:00", updated_at="2026-11-19T12:00:00+02:00"))
    _store(api, _doc("doc_err_known", minutes=5, status=DocumentStatus.FAILED, internal_state=IngestionState.FAILED,
                     error={"code": "UNSUPPORTED_FILE", "message": "SECRET-LISTMSG /srv/x", "request_id": "SECRET-RID", "details": {"p": "SECRET-DET"}}))
    _store(api, _doc("doc_err_unknown", minutes=6, status=DocumentStatus.FAILED, internal_state=IngestionState.FAILED,
                     error={"code": "SECRET-CODE", "message": "open('/srv/data/x') failed SECRET-LISTMSG"}))
    _store(api, _doc("doc_err_empty", minutes=7, error={}))
    _store(api, _doc("doc_filename_odd", minutes=8, claimed_filename="Jänè Röe\n2026.pdf"))
    listing = _list(api, size=100)
    assert listing.status_code == 200
    rows = {r["document_id"]: r for r in listing.json()["content"]}
    assert len(rows) == 8
    for doc_id, row in rows.items():
        detail = api["client"].get(f"{LIST_URL}/{doc_id}", headers=ALPHA_HEADERS)
        assert detail.status_code == 200, doc_id
        expected = detail.json()
        assert expected.pop("result") is None
        assert row == expected, doc_id
    assert rows["doc_err_known"]["error_code"] == "UNSUPPORTED_FILE"
    assert rows["doc_err_unknown"]["error_code"] == "INTERNAL_ERROR"
    for leak in ("SECRET", "/srv/", "request_id", "details"):
        assert leak not in listing.text


def test_route_list_rows_never_expose_a_stored_error_message(api):
    _store(api, _doc("doc_leaky", error={"code": "STORAGE_FAILED", "message": "Traceback at /srv/x SECRET-STORED-MSG"}))
    response = _list(api)
    assert response.status_code == 200
    (row,) = response.json()["content"]
    assert row["error_code"] == "INTERNAL_ERROR" and "SECRET" not in response.text and "Traceback" not in response.text


# ---- record integrity through the route -------------------------------------------------------------------------------

@pytest.mark.parametrize("corrupt, reason", _CORRUPTIONS)
def test_route_one_corrupt_record_is_a_sanitized_500_and_nothing_partial_is_returned(api, captured_logs, corrupt, reason):
    _store_many(api, 3)
    corrupt(_store(api, _doc("doc_corrupt", minutes=50)))
    response = _list(api)
    _assert_generic_500(response, api["tmp_path"], "SECRET", "tenant_beta", "tenant_alpha", "doc_corrupt", "doc_0000")
    assert set(response.json()) == {"error"}, "a corrupt record must not produce a partial or success-shaped body"
    joined = "\n".join(captured_logs.messages)
    assert f"reason={reason}" in joined
    for leak in ("SECRET", "tenant_beta", "tenant_alpha", "doc_corrupt", str(api["tmp_path"]), "invoice_2026"):
        assert leak not in joined, f"{leak!r} reached a log record"
    assert all("\n" not in m and "\r" not in m for m in captured_logs.messages)


def test_route_foreign_record_in_the_tenant_directory_is_never_listed_or_leaked(api, captured_logs):
    _store_many(api, 2)
    foreign = _store(api, _doc("doc_foreign", minutes=9, claimed_filename="BETA-PATIENT-NAME.pdf"))
    _rewrite_record(foreign, _set("tenant_id", "tenant_beta"))
    response = _list(api)
    _assert_generic_500(response, api["tmp_path"], "BETA-PATIENT-NAME", "tenant_beta", "doc_foreign")
    assert "BETA-PATIENT-NAME" not in "\n".join(captured_logs.messages)


def test_route_symlinks_and_special_files_are_a_generic_500(api):
    tmp_path = api["tmp_path"]
    tenant_dir = _store(api, _doc("doc_seed")).parent
    decoy = _make_decoy_document(tmp_path.parent / (tmp_path.name + "_decoy_route_list"), "tenant_alpha", "doc_decoy")
    link = tenant_dir / "doc_decoy"
    link.symlink_to(decoy)
    _assert_generic_500(_list(api), tmp_path, "DECOY")
    link.unlink()
    record_dir = _store(api, _doc("doc_fifo"))
    (record_dir / "document_record.json").unlink()
    os.mkfifo(str(record_dir / "document_record.json"))
    response = _call_with_timeout(lambda: _list(api), fifo_paths=[record_dir / "document_record.json"])
    _assert_generic_500(response, tmp_path)


def test_route_symlinked_tenant_directory_is_a_generic_500(api):
    tmp_path = api["tmp_path"]
    outside = tmp_path.parent / (tmp_path.name + "_decoy_route_tenant")
    _make_decoy_document(outside, "tenant_symlink_attack", "doc_victim")
    (tmp_path / "tenant_symlink_attack").symlink_to(outside / "tenant_symlink_attack")
    api["app"].state.auth_provider.principals["token-attack"] = ApiPrincipal(
        principal_id="attacker", tenant_id="tenant_symlink_attack", scopes={SCOPE_DOCUMENTS_READ}, is_active=True
    )
    _assert_generic_500(_list(api, {"Authorization": "Bearer token-attack"}), tmp_path, "DECOY", "doc_victim")


def test_route_scan_ceiling_is_a_generic_500_with_a_fixed_reason_and_never_a_truncated_list(api, monkeypatch, captured_logs):
    monkeypatch.setattr(read_service_module, "_MAX_LISTED_ENTRIES", 4)
    _store_many(api, 4)
    assert _list(api).json()["paging"]["total_items"] == 4
    _store(api, _doc("doc_0004", minutes=4))
    response = _list(api)
    _assert_generic_500(response, api["tmp_path"], "doc_000")
    assert set(response.json()) == {"error"}
    assert "reason=document_listing_limit_exceeded" in "\n".join(captured_logs.messages)


def test_route_never_creates_or_changes_anything_on_disk(api):
    _store_many(api, 5)
    tenant_dir = api["tmp_path"] / "tenant_alpha"
    (tenant_dir / "idempotency").mkdir()
    before = _tree(api["tmp_path"])
    contents = {p: p.read_bytes() for p in api["tmp_path"].rglob("*") if p.is_file()}
    for params in ({}, {"status": "failed"}, {"page": 9}, {"q": "doc"}, {"size": 1, "page": 3}):
        assert _list(api, **params).status_code == 200
    assert _tree(api["tmp_path"]) == before
    assert {p: p.read_bytes() for p in api["tmp_path"].rglob("*") if p.is_file()} == contents


def test_route_does_not_disturb_api002_or_api003_routing(api):
    _store(api, _doc("doc_one"))
    assert api["client"].get(f"{LIST_URL}/doc_one", headers=ALPHA_HEADERS).status_code == 200
    assert api["client"].get(f"{LIST_URL}/doc_unknown", headers=ALPHA_HEADERS).status_code == 404
    page = api["client"].get(f"{LIST_URL}/doc_one/pages/1", headers=ALPHA_HEADERS)
    assert page.status_code == 404                              # no page was stored for this record: API-003 answers as before
