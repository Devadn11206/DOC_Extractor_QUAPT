"""
In-memory FAKE of the slice of the asynchronous PyMongo API that the MongoDB foundation uses. TEST DOUBLE ONLY.

It exists so the foundation's logic (tenant scoping, filter checks, error mapping, index bootstrap, lifecycle) can be tested
without a server. It is NOT a MongoDB: it implements equality and a few comparison operators, plain unique indexes and no
transactions semantics beyond calling the callback. Results from these tests prove the foundation's own behaviour against
the driver's call shapes; they say nothing about real server behaviour (that is ``tests/test_mongo_integration.py``, which needs
a real replica set).
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


def _driver_errors():
    from pymongo import errors

    return errors


def _get(doc: Dict[str, Any], key: str) -> Any:
    return doc.get(key)


def _sort_key(value: Any):
    """Missing values last, and values of different types ordered by type name (a server orders by BSON type; never a TypeError)."""
    return (value is None, type(value).__name__, 0 if value is None else value)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _ordered(value: Any, operand: Any, compare) -> bool:
    """A range comparison with BSON type bracketing: a value of another type (a string against a number) simply does not match."""
    if value is None or not (_number(value) and _number(operand) or type(value) is type(operand)):
        return False
    return bool(compare(value, operand))


def matches(doc: Dict[str, Any], flt: Dict[str, Any]) -> bool:
    for key, cond in flt.items():
        if key == "$and":
            if not all(matches(doc, sub) for sub in cond):
                return False
            continue
        if key == "$or":
            if not any(matches(doc, sub) for sub in cond):
                return False
            continue
        if key == "$nor":
            if any(matches(doc, sub) for sub in cond):
                return False
            continue
        value = _get(doc, key)
        if isinstance(cond, dict) and cond and all(k.startswith("$") for k in cond):
            for op, operand in cond.items():
                if op == "$eq" and not value == operand:
                    return False
                if op == "$ne" and not value != operand:
                    return False
                if op == "$gt" and not _ordered(value, operand, lambda a, b: a > b):
                    return False
                if op == "$gte" and not _ordered(value, operand, lambda a, b: a >= b):
                    return False
                if op == "$lt" and not _ordered(value, operand, lambda a, b: a < b):
                    return False
                if op == "$lte" and not _ordered(value, operand, lambda a, b: a <= b):
                    return False
                if op == "$in" and value not in operand:
                    return False
                if op == "$nin" and value in operand:
                    return False
                if op == "$exists" and (key in doc) != operand:
                    return False
        elif value != cond:
            if isinstance(value, (list, tuple, set)) and cond in value:
                continue
            return False
    return True


class FakeCursor:
    def __init__(self, docs: List[Dict[str, Any]], owner: Any = None) -> None:
        self._docs = docs
        self._owner = owner                      # the collection: a scripted failure surfaces when the cursor is read
        self._skip = 0
        self._limit: Optional[int] = None

    def sort(self, spec):
        for key, direction in reversed(list(spec)):
            self._docs.sort(key=lambda d, k=key: _sort_key(d.get(k)), reverse=direction < 0)
        return self

    def skip(self, count: int):
        self._skip = count
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    async def to_list(self, length: Optional[int] = None) -> List[Dict[str, Any]]:
        await asyncio.sleep(0)
        if self._owner is not None:
            self._owner._maybe_fail()
        window = self._docs[self._skip:]
        if self._limit is not None:
            window = window[: self._limit]
        if length is not None:
            window = window[:length]
        return [copy.deepcopy(d) for d in window]


class _IndexCursor:
    """Index descriptions as the server reports them: ``name``, ``key`` (ordered) and ``unique`` (present only when true)."""

    def __init__(self, entries: List[Dict[str, Any]]) -> None:
        self._entries = entries

    def __aiter__(self):
        async def gen():
            for entry in self._entries:
                yield dict(entry)

        return gen()


class FakeCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: List[Dict[str, Any]] = []
        self.indexes: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self.session_calls: List[tuple] = []     # (operation, session passed or None), parallel to ``calls``
        self.fail_with: Optional[BaseException] = None

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    async def find_one(self, flt, sort=None, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("find_one", session))
        self.calls.append(("find_one", copy.deepcopy(flt)))
        docs = [d for d in self.docs if matches(d, flt)]
        if sort:
            docs = (await FakeCursor(docs).sort(sort).to_list())
        return copy.deepcopy(docs[0]) if docs else None

    def find(self, flt, session=None):
        self.session_calls.append(("find", session))
        self.calls.append(("find", copy.deepcopy(flt)))
        return FakeCursor([d for d in self.docs if matches(d, flt)], owner=self)

    async def count_documents(self, flt, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("count_documents", session))
        self.calls.append(("count_documents", copy.deepcopy(flt)))
        return sum(1 for d in self.docs if matches(d, flt))

    async def insert_one(self, doc, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("insert_one", session))
        self.calls.append(("insert_one", copy.deepcopy(doc)))
        for index in self.indexes.values():
            if not index["unique"]:
                continue
            keys = [k for k, _ in index["keys"]]
            probe = tuple(doc.get(k) for k in keys)
            if any(tuple(d.get(k) for k in keys) == probe for d in self.docs):
                raise _driver_errors().DuplicateKeyError("E11000 duplicate key SECRET-VALUE-IN-DRIVER-TEXT", 11000)
        self.docs.append(copy.deepcopy(doc))
        return SimpleNamespace(inserted_id="x")

    @staticmethod
    def _apply(doc: Dict[str, Any], update: Dict[str, Any]) -> None:
        for field, value in update.get("$set", {}).items():
            target, *path = field.split(".")[::-1]            # dotted paths create the nested documents, as the server does
            holder = doc
            for segment in reversed(path):
                holder = holder.setdefault(segment, {})
            holder[target] = value
        for field, value in update.get("$push", {}).items():
            items = value["$each"] if isinstance(value, dict) and "$each" in value else [value]
            doc.setdefault(field, []).extend(copy.deepcopy(items))
        for field in update.get("$unset", {}):
            doc.pop(field, None)
        for field, value in update.get("$inc", {}).items():
            doc[field] = doc.get(field, 0) + value

    async def update_one(self, flt, update, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("update_one", session))
        self.calls.append(("update_one", copy.deepcopy(flt), copy.deepcopy(update)))
        for doc in self.docs:
            if matches(doc, flt):
                self._apply(doc, update)
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, flt, update, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("update_many", session))
        self.calls.append(("update_many", copy.deepcopy(flt), copy.deepcopy(update)))
        matched = 0
        for doc in self.docs:
            if matches(doc, flt):
                self._apply(doc, update)
                matched += 1
        return SimpleNamespace(matched_count=matched, modified_count=matched)

    async def find_one_and_update(self, flt, update, return_document=True, sort=None, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("find_one_and_update", session))
        self.calls.append(("find_one_and_update", copy.deepcopy(flt), copy.deepcopy(update)))
        for doc in self.docs:
            if matches(doc, flt):
                before = copy.deepcopy(doc)
                self._apply(doc, update)
                return copy.deepcopy(doc) if return_document else before
        return None

    async def delete_one(self, flt, session=None):
        await asyncio.sleep(0)
        self._maybe_fail()
        self.session_calls.append(("delete_one", session))
        self.calls.append(("delete_one", copy.deepcopy(flt)))
        for position, doc in enumerate(self.docs):
            if matches(doc, flt):
                del self.docs[position]
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)

    async def create_index(self, keys, **kwargs):
        self._maybe_fail()
        name = kwargs["name"]
        options = {"keys": [tuple(k) for k in keys], "unique": bool(kwargs.get("unique", False)),
                   "partial": kwargs.get("partialFilterExpression"), "ttl": kwargs.get("expireAfterSeconds")}
        existing = self.indexes.get(name)
        if existing is not None and existing != options:
            raise _driver_errors().OperationFailure("Index already exists with different options SECRET", code=85)
        self.indexes[name] = options
        return name

    async def list_indexes(self):
        if not self.indexes and not self.docs:
            raise _driver_errors().OperationFailure("ns does not exist", code=26)
        entries: List[Dict[str, Any]] = [{"name": "_id_", "key": {"_id": 1}}]
        for name, options in self.indexes.items():
            entry: Dict[str, Any] = {"name": name, "key": {field: direction for field, direction in options["keys"]}}
            if options["unique"]:
                entry["unique"] = True
            entries.append(entry)
        return _IndexCursor(entries)


class FakeDatabase:
    def __init__(self, name: str) -> None:
        self.name = name
        self.collections: Dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self.collections.setdefault(name, FakeCollection(name))


class FakeAdmin:
    def __init__(self, client: "FakeClient") -> None:
        self._client = client

    async def command(self, name: str):
        self._client.commands.append(name)
        if name == "ping" and self._client.ping_gate is not None:
            await self._client.ping_gate.wait()               # lets a test hold a probe "in flight"
        if self._client.fail_connect is not None:
            raise self._client.fail_connect
        if name == "ping":
            return {"ok": 1}
        if name == "hello":
            return dict(self._client.hello)
        raise AssertionError(f"unexpected command {name}")


class FakeSession:
    """
    Mirrors the shape of ``AsyncClientSession``: an async context manager that is NOT itself awaitable, with a
    ``with_transaction`` that commits when the callback returns and aborts (then re-raises) when it raises.
    """

    def __init__(self) -> None:
        self.transactions: List[Dict[str, Any]] = []
        self.committed = 0
        self.aborted = 0
        self.ended = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.ended = True
        return False

    async def with_transaction(self, callback, **options):
        self.transactions.append(options)
        try:
            result = await callback(self)
        except BaseException:
            self.aborted += 1
            raise
        self.committed += 1
        return result


class FakeClient:
    """``hello`` is the server's reply to the ``hello`` command; ``fail_connect`` makes ping and hello raise."""

    def __init__(self, uri: str = "", *, hello: Optional[Dict[str, Any]] = None, fail_connect: Optional[BaseException] = None,
                 **options: Any) -> None:
        self.uri = uri
        self.options = options
        self.hello = hello if hello is not None else {"setName": "rs0", "isWritablePrimary": True}
        self.fail_connect = fail_connect
        self.commands: List[str] = []
        self.ping_gate: Optional[asyncio.Event] = None
        self.closed = False
        self.databases: Dict[str, FakeDatabase] = {}
        self.admin = FakeAdmin(self)
        self.sessions: List[FakeSession] = []

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.databases.setdefault(name, FakeDatabase(name))

    async def close(self) -> None:
        self.closed = True

    def start_session(self) -> FakeSession:
        """A plain method, like the driver's: ``await client.start_session()`` is a TypeError, ``async with`` is correct."""
        session = FakeSession()
        self.sessions.append(session)
        return session


def factory_for(client: FakeClient):
    """A client factory that records its call and returns ``client``."""
    calls: List[Dict[str, Any]] = []

    def factory(uri: str, **options: Any) -> FakeClient:
        calls.append({"uri": uri, "options": options})
        client.uri, client.options = uri, options
        return client

    factory.calls = calls          # type: ignore[attr-defined]
    return factory


class LogCapture:
    """
    Capture the foundation's own log records directly on its logger. ``caplog`` is not enough: once any test has started the
    application, ``setup_logging`` stops the ``doc_extractor`` logger from propagating to the root logger, so a root-attached
    capture silently sees nothing and an assertion that a secret is ABSENT would pass vacuously. Tests that use this must
    also assert that the records they expect ARE present.
    """

    def __init__(self, name: str = "doc_extractor.infrastructure.mongo") -> None:
        import logging

        self._logger = logging.getLogger(name)
        self.records: List[Any] = []
        self._saved = None

        class _Handler(logging.Handler):
            def emit(handler, record):                       # noqa: N805
                self.records.append(record)

        self._handler = _Handler(logging.DEBUG)

    def __enter__(self) -> "LogCapture":
        self._saved = (self._logger.level, self._logger.disabled)
        self._logger.addHandler(self._handler)
        self._logger.setLevel(10)
        self._logger.disabled = False
        return self

    def __exit__(self, *exc) -> None:
        self._logger.removeHandler(self._handler)
        self._logger.setLevel(self._saved[0])
        self._logger.disabled = self._saved[1]

    @property
    def text(self) -> str:
        return "\n".join(record.getMessage() for record in self.records)
