"""
Tenant-scoped repository base for MongoDB (Team Member 3, Phase 2A-2).

Every concrete repository (review tasks: ``review_tasks.py``; API keys: ``api_keys.py``; audit: ``audit.py``, which composes it instead) derives from ``TenantScopedRepository`` and
reaches the collection ONLY through it. The base makes the tenant boundary (SEC-006) structural instead of a convention:

- EVERY operation takes a ``tenant_id``, validated with the same rule as the rest of the application
  (``require_path_safe_tenant_id``), and ANDs ``{"tenant_id": tenant_id}`` into the filter at the top level. A caller cannot
  omit it and cannot override it: a filter or update that names ``tenant_id`` is refused.
- Filters are built from equality and a short allow-list of comparison operators over plain values. Operators that execute
  code or arbitrary expressions (``$where``, ``$expr``, ``$function``, ``$accumulator``) and ``$regex`` are not permitted, and
  a value that is a mapping or a list is refused unless the operator expects one. This closes operator injection through
  attacker-controlled values.
- A document to insert must carry the scope's tenant (it is added when absent, refused when different). It must NOT carry a
  root ``_id``: the server generates it. ``_id`` is unique across the whole collection, not per tenant, so a caller-chosen one
  would let tenants collide with, and probe for, each other's records.
- Updates use an allow-list of operators and may not touch ``tenant_id`` or ``_id``. Update TARGETS (dotted paths such as
  ``metadata.count`` are allowed) are validated separately from the VALUES, which go through the value serializer.
- Reads, like writes, accept the transaction ``session`` of ``MongoConnection.run_in_transaction``; the tenant filter is
  applied exactly the same way with or without one.
- ONE narrow exception to the tenant rule exists, and it is opt-in per field: ``find_one_by_global_unique_key``. A repository
  whose collection has a globally unique key that identifies the tenant (the API-key credential hash: authentication has only
  the hash and must learn the tenant FROM the record) declares that field at construction. Only a declared field can be looked
  up this way, only by equality on a plain string, and only for one document; the record's own tenant is then read from the
  record. Every other operation, including every write, stays tenant-scoped.
- Driver failures become typed ``PersistenceError`` subclasses with fixed messages (duplicate keys never echo the key).
- Reads return plain dicts WITHOUT the internal ``_id``; models are built by ``serialization.from_document``.
- Page sizes are bounded.

This class adds no policy about any collection: unique constraints live in ``indexes.py`` and business rules in the
repositories of later slices.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from doc_extractor.infrastructure.mongo.connection import MongoConnection
from doc_extractor.infrastructure.mongo.errors import (
    PersistenceDataError,
    PersistenceError,
    TenantScopeError,
    UnsafeQueryError,
    translate_driver_error,
)
from doc_extractor.infrastructure.mongo.serialization import to_document, to_value
from doc_extractor.tenancy import require_path_safe_tenant_id
from doc_extractor.exceptions import InvalidTenantIdentifierError

MAX_PAGE_SIZE = 1_000
TENANT_FIELD = "tenant_id"

_COMPARISON_OPERATORS = frozenset({"$eq", "$ne", "$gt", "$gte", "$lt", "$lte", "$in", "$nin", "$exists"})
_LIST_OPERATORS = frozenset({"$in", "$nin"})
_LOGICAL_OPERATORS = frozenset({"$and", "$or", "$nor"})
_UPDATE_OPERATORS = frozenset({"$set", "$unset", "$inc", "$push", "$pull", "$addToSet", "$setOnInsert", "$currentDate"})
_EACH_OPERATORS = frozenset({"$push", "$addToSet"})
_PROTECTED_FIELDS = frozenset({TENANT_FIELD, "_id"})
_MAX_FILTER_DEPTH = 6
_MAX_IN_LIST = 1_000
_MAX_GLOBAL_KEY_LENGTH = 256

SortSpec = Sequence[Tuple[str, int]]


def validate_tenant(tenant_id: Any) -> str:
    """The tenant, validated with the application-wide rule. Raises ``TenantScopeError`` (never echoes the value)."""
    try:
        checked = require_path_safe_tenant_id(tenant_id)
    except InvalidTenantIdentifierError:
        raise TenantScopeError("invalid_tenant_identifier") from None
    if not checked.strip():                  # the container refuses a blank tenant too; the shared path rule alone does not
        raise TenantScopeError("invalid_tenant_identifier")
    return checked


def _scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str, bytes, datetime))


def _check_condition(field: str, condition: Any) -> None:
    if _scalar(condition):
        return
    if not isinstance(condition, Mapping) or not condition:
        raise UnsafeQueryError("filter_value_is_not_a_plain_value")
    for operator, operand in condition.items():
        if operator not in _COMPARISON_OPERATORS:
            raise UnsafeQueryError("filter_operator_is_not_permitted")
        if operator in _LIST_OPERATORS:
            if not isinstance(operand, (list, tuple)) or len(operand) > _MAX_IN_LIST or not all(_scalar(v) for v in operand):
                raise UnsafeQueryError("filter_list_operand_is_not_a_short_list_of_plain_values")
        elif operator == "$exists":
            if not isinstance(operand, bool):
                raise UnsafeQueryError("filter_exists_operand_is_not_a_boolean")
        elif not _scalar(operand):
            raise UnsafeQueryError("filter_operand_is_not_a_plain_value")


def validate_filter(filter_: Optional[Mapping[str, Any]], *, _depth: int = 0) -> Dict[str, Any]:
    """A copy of ``filter_`` that passed the allow-list. Raises ``UnsafeQueryError`` (reason only)."""
    if filter_ is None:
        return {}
    if not isinstance(filter_, Mapping):
        raise UnsafeQueryError("filter_is_not_a_mapping")
    if _depth > _MAX_FILTER_DEPTH:
        raise UnsafeQueryError("filter_is_nested_too_deeply")
    checked: Dict[str, Any] = {}
    for key, value in filter_.items():
        if not isinstance(key, str) or not key:
            raise UnsafeQueryError("filter_field_is_not_a_non_empty_string")
        if key in _LOGICAL_OPERATORS:
            if not isinstance(value, (list, tuple)) or not value or len(value) > 20:
                raise UnsafeQueryError("filter_logical_operand_is_not_a_short_list")
            checked[key] = [validate_filter(item, _depth=_depth + 1) for item in value]
            continue
        if key.startswith("$"):
            raise UnsafeQueryError("filter_operator_is_not_permitted")
        if key == TENANT_FIELD:
            raise TenantScopeError("filter_must_not_name_the_tenant")
        _check_condition(key, value)
        checked[key] = value
    return checked


def _check_update_target(field: Any) -> None:
    """An update target (a field name or dotted path). It is NOT a document key: ``metadata.count`` is a legitimate target."""
    if not isinstance(field, str) or not field:
        raise UnsafeQueryError("update_field_is_not_a_non_empty_string")
    segments = field.split(".")
    if segments[0] in _PROTECTED_FIELDS:
        raise TenantScopeError("update_must_not_change_the_tenant_or_id")
    if "\x00" in field or any(not segment or segment.startswith("$") for segment in segments):
        raise UnsafeQueryError("update_field_uses_reserved_characters")


def _update_value(operator: str, value: Any) -> Any:
    """The serialized operand of one update target. ``$push`` and ``$addToSet`` accept exactly ``{"$each": [...]}`` as a modifier."""
    if operator in _EACH_OPERATORS and isinstance(value, Mapping) and "$each" in value:
        items = value["$each"]
        if set(value) != {"$each"} or not isinstance(items, (list, tuple)) or len(items) > _MAX_IN_LIST:
            raise UnsafeQueryError("update_each_operand_is_not_a_short_list")
        return {"$each": [to_value(item) for item in items]}
    return to_value(value)


def validate_update(update: Mapping[str, Any]) -> Dict[str, Any]:
    """
    A copy of ``update`` that uses only allowed operators, does not touch the tenant or ``_id``, and whose values are
    serialized. Targets are checked as targets (dotted paths allowed); values as values (no operator or path keys smuggled in).
    """
    if not isinstance(update, Mapping) or not update:
        raise UnsafeQueryError("update_is_not_an_operator_document")
    checked: Dict[str, Any] = {}
    for operator, fields in update.items():
        if operator not in _UPDATE_OPERATORS or not isinstance(fields, Mapping) or not fields:
            raise UnsafeQueryError("update_operator_is_not_permitted")
        targets: Dict[str, Any] = {}
        for field, value in fields.items():
            _check_update_target(field)
            targets[field] = _update_value(operator, value)
        checked[operator] = targets
    return checked


def _strip_id(document: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if document is None:
        return None
    return {key: value for key, value in document.items() if key != "_id"}


class TenantScopedRepository:
    """Base of every MongoDB repository. See the module docstring."""

    def __init__(
        self, connection: MongoConnection, collection_name: str, *, global_unique_fields: Iterable[str] = ()
    ) -> None:
        if not collection_name or "$" in collection_name or collection_name.startswith("system."):
            raise ValueError("A repository needs a plain collection name.")
        fields = frozenset(global_unique_fields)
        for field in fields:
            if not isinstance(field, str) or not field or field in _PROTECTED_FIELDS or "$" in field or "." in field:
                raise ValueError("A global unique field must be a plain field name other than the tenant and the id.")
        self._connection = connection
        self._collection_name = collection_name
        self._global_unique_fields = fields

    @property
    def collection_name(self) -> str:
        return self._collection_name

    @property
    def _collection(self) -> Any:
        return self._connection.collection(self._collection_name)

    def scoped_filter(self, tenant_id: str, filter_: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """``filter_`` (validated) with the tenant ANDed in. The tenant key is written last: nothing can shadow it."""
        scoped = validate_filter(filter_)
        scoped[TENANT_FIELD] = validate_tenant(tenant_id)
        return scoped

    async def _run(self, operation: Any) -> Any:
        try:
            return await operation
        except PersistenceError:
            raise
        except Exception as exc:                                            # noqa: BLE001 - mapped to a typed error
            raise translate_driver_error(exc, collection=self._collection_name) from None

    # -- operations ------------------------------------------------------------------------------------------------------

    async def find_one(
        self,
        tenant_id: str,
        filter_: Optional[Mapping[str, Any]] = None,
        *,
        sort: Optional[SortSpec] = None,
        session: Any = None,
    ) -> Optional[Dict[str, Any]]:
        query = self.scoped_filter(tenant_id, filter_)
        kwargs: Dict[str, Any] = {}
        if sort is not None:
            kwargs["sort"] = list(sort)
        if session is not None:
            kwargs["session"] = session
        return _strip_id(await self._run(self._collection.find_one(query, **kwargs)))

    async def find_many(
        self,
        tenant_id: str,
        filter_: Optional[Mapping[str, Any]] = None,
        *,
        sort: Optional[SortSpec] = None,
        skip: int = 0,
        limit: int = 100,
        session: Any = None,
    ) -> List[Dict[str, Any]]:
        if not isinstance(limit, int) or not 1 <= limit <= MAX_PAGE_SIZE or not isinstance(skip, int) or skip < 0:
            raise UnsafeQueryError("page_bounds_are_not_valid")
        query = self.scoped_filter(tenant_id, filter_)
        cursor = self._collection.find(query, **({"session": session} if session is not None else {}))
        if sort is not None:
            cursor = cursor.sort(list(sort))
        cursor = cursor.skip(skip).limit(limit)
        documents = await self._run(cursor.to_list(length=limit))
        return [_strip_id(document) for document in documents]

    async def find_one_by_global_unique_key(self, field: str, value: str) -> Optional[Dict[str, Any]]:
        """
        The ONE tenant-exempt read: the document whose globally unique ``field`` equals ``value`` (``None`` when there is none).

        Only a field declared as ``global_unique_fields`` at construction is accepted (anything else raises
        ``TenantScopeError``), the value must be a plain non-empty string (no operator can be injected), and a unique index on
        the field must make the answer a single document. The caller learns the owning tenant from the returned document and
        must not assume one beforehand. Never use this for a field that is not unique across tenants.
        """
        if field not in self._global_unique_fields:
            raise TenantScopeError("field_is_not_a_declared_global_key")
        if not isinstance(value, str) or not value or len(value) > _MAX_GLOBAL_KEY_LENGTH:
            raise UnsafeQueryError("global_key_is_not_a_plain_string")
        return _strip_id(await self._run(self._collection.find_one({field: value})))

    async def find_window(
        self,
        tenant_id: str,
        filter_: Optional[Mapping[str, Any]] = None,
        *,
        sort: Optional[SortSpec] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Up to ``limit`` documents from ``skip`` (tenant-scoped), fetched in chunks of at most ``MAX_PAGE_SIZE``: ``limit`` itself may exceed it."""
        if not isinstance(limit, int) or limit < 0 or not isinstance(skip, int) or skip < 0:
            raise UnsafeQueryError("page_bounds_are_not_valid")
        documents: List[Dict[str, Any]] = []
        while len(documents) < limit:
            wanted = min(MAX_PAGE_SIZE, limit - len(documents))
            chunk = await self.find_many(tenant_id, filter_, sort=sort, skip=skip + len(documents), limit=wanted)
            documents.extend(chunk)
            if len(chunk) < wanted:                          # the collection ran out
                break
        return documents

    async def count(self, tenant_id: str, filter_: Optional[Mapping[str, Any]] = None, *, session: Any = None) -> int:
        query = self.scoped_filter(tenant_id, filter_)
        kwargs = {"session": session} if session is not None else {}
        return int(await self._run(self._collection.count_documents(query, **kwargs)))

    async def insert_one(self, tenant_id: str, document: Mapping[str, Any], *, session: Any = None) -> None:
        """
        Insert ``document`` for ``tenant_id``; the server generates ``_id``. A document with a root ``_id`` raises
        ``PersistenceDataError``. Raises ``DuplicateKeyPersistenceError`` when a unique index refuses it.
        """
        scope = validate_tenant(tenant_id)
        data = to_document(document)
        if "_id" in data:
            raise PersistenceDataError("document_must_not_set_the_id")
        if data.get(TENANT_FIELD, scope) != scope:
            raise TenantScopeError("document_names_another_tenant")
        data[TENANT_FIELD] = scope
        kwargs = {"session": session} if session is not None else {}
        await self._run(self._collection.insert_one(data, **kwargs))

    async def update_one(
        self,
        tenant_id: str,
        filter_: Optional[Mapping[str, Any]],
        update: Mapping[str, Any],
        *,
        session: Any = None,
    ) -> int:
        """Apply ``update`` to the first match inside the tenant. Returns the number of documents MATCHED (0 or 1)."""
        query = self.scoped_filter(tenant_id, filter_)
        kwargs = {"session": session} if session is not None else {}
        result = await self._run(self._collection.update_one(query, validate_update(update), **kwargs))
        return int(result.matched_count)

    async def find_one_and_update(
        self,
        tenant_id: str,
        filter_: Optional[Mapping[str, Any]],
        update: Mapping[str, Any],
        *,
        return_after: bool = True,
        sort: Optional[SortSpec] = None,
        session: Any = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Atomically match and update one document inside the tenant (a single conditional update: the building block for the
        atomic claim, release and complete of ``review_tasks.py``). Returns the document, or ``None`` when nothing matched.
        """
        query = self.scoped_filter(tenant_id, filter_)
        kwargs: Dict[str, Any] = {"return_document": bool(return_after)}
        if sort is not None:
            kwargs["sort"] = list(sort)
        if session is not None:
            kwargs["session"] = session
        document = await self._run(self._collection.find_one_and_update(query, validate_update(update), **kwargs))
        return _strip_id(document)

    async def delete_one(self, tenant_id: str, filter_: Optional[Mapping[str, Any]], *, session: Any = None) -> int:
        query = self.scoped_filter(tenant_id, filter_)
        kwargs = {"session": session} if session is not None else {}
        result = await self._run(self._collection.delete_one(query, **kwargs))
        return int(result.deleted_count)
