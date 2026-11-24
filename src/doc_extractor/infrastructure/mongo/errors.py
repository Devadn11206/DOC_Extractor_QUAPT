"""
Typed persistence errors for the MongoDB foundation.

Every error has a FIXED message and a fixed machine-readable ``reason`` in ``details``. None of them ever carries a
connection string, a credential, a query value, a document field or the text of a driver error (those can quote any of
them: a duplicate-key error message contains the offending key values, a server-selection error contains host names).

Operational faults derive from ``StorageError``: the REST API already answers a ``StorageError`` with a generic 500
``INTERNAL_ERROR`` and logs only the fixed ``reason``. A configuration fault derives from ``ConfigurationError``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from doc_extractor.exceptions import ConfigurationError, StorageError


def _details(reason: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    details: Dict[str, Any] = {"reason": reason}
    if extra:
        details.update(extra)
    return details


class PersistenceError(StorageError):
    """Base of every database failure raised by the MongoDB foundation."""

    default_message = "A persistence operation failed."
    default_reason = "persistence_failed"
    default_code = "PERSISTENCE_FAILED"

    def __init__(self, reason: Optional[str] = None, *, collection: Optional[str] = None) -> None:
        extra = {"collection": collection} if collection else None
        super().__init__(
            message=self.default_message,
            code=self.default_code,
            details=_details(reason or self.default_reason, extra),
        )


class PersistenceUnavailableError(PersistenceError):
    """The database could not be reached, or an operation timed out. Safe to retry later; nothing is known to be written."""

    default_message = "The database is unavailable."
    default_reason = "persistence_unavailable"
    default_code = "PERSISTENCE_UNAVAILABLE"


class DuplicateKeyPersistenceError(PersistenceError):
    """A unique index refused a write. The duplicated value is deliberately not reported."""

    default_message = "A record with the same unique key already exists."
    default_reason = "duplicate_key"
    default_code = "PERSISTENCE_DUPLICATE_KEY"


class PersistenceDataError(PersistenceError):
    """A document could not be serialized, or a stored document could not be read back into its model."""

    default_message = "A stored or submitted record is not valid."
    default_reason = "invalid_data"
    default_code = "PERSISTENCE_DATA_INVALID"


class IndexConflictError(PersistenceError):
    """An index with the same name exists with different options. The foundation never drops or rebuilds an index."""

    default_message = "An existing index conflicts with the declared index."
    default_reason = "index_conflict"
    default_code = "PERSISTENCE_INDEX_CONFLICT"


class TransactionUnavailableError(PersistenceError):
    """A transaction was requested but the deployment is not a replica set (or the connection is not established)."""

    default_message = "Transactions are not available on this database deployment."
    default_reason = "transactions_unavailable"
    default_code = "PERSISTENCE_TRANSACTIONS_UNAVAILABLE"


class TenantScopeError(PersistenceError, ValueError):
    """A repository call lacked a valid tenant, or tried to read, write or change another tenant's data (SEC-006)."""

    default_message = "The operation is not tenant scoped."
    default_reason = "tenant_scope_violation"
    default_code = "PERSISTENCE_TENANT_SCOPE"


class UnsafeQueryError(PersistenceError, ValueError):
    """A filter or update used an operator or value that the tenant-scoped repository base does not permit."""

    default_message = "The query or update is not permitted."
    default_reason = "unsafe_query"
    default_code = "PERSISTENCE_UNSAFE_QUERY"


class PersistenceConfigurationError(ConfigurationError):
    """The MongoDB configuration is invalid or incomplete. The message never contains the connection string."""

    def __init__(self, reason: str, *, setting: Optional[str] = None) -> None:
        extra = {"setting": setting} if setting else None
        super().__init__(
            message="The MongoDB configuration is not valid.",
            details=_details(reason, extra),
        )


def is_driver_error(exc: BaseException) -> bool:
    """
    True for a PyMongo or BSON error: the errors ``translate_driver_error`` is meant for. Anything else (an application
    exception, a programming error) is not a driver fault and must not be disguised as one. Decided by the class hierarchy's
    module names, so the driver is not imported here (``bson`` ships inside the ``pymongo`` distribution).
    """
    return any((klass.__module__ or "").split(".")[0] in ("pymongo", "bson") for klass in type(exc).__mro__)


def translate_driver_error(exc: BaseException, *, collection: Optional[str] = None) -> PersistenceError:
    """
    Map a driver exception to a typed persistence error with a FIXED message. The driver text is dropped on purpose: a
    duplicate-key message quotes the key values, a server-selection message quotes host names. ``pymongo`` is imported
    only here and only when an error actually happened, so this module imports without the driver.
    """
    if isinstance(exc, PersistenceError):
        return exc
    module = type(exc).__module__ or ""
    if module.startswith("bson"):
        return PersistenceDataError("document_cannot_be_encoded", collection=collection)
    try:
        from pymongo import errors as driver
    except ImportError:                                  # pragma: no cover - no driver, no driver error
        return PersistenceError(collection=collection)
    if isinstance(exc, driver.DuplicateKeyError):
        return DuplicateKeyPersistenceError(collection=collection)
    if isinstance(exc, (driver.ConnectionFailure, driver.ExecutionTimeout)):
        # ServerSelectionTimeoutError, AutoReconnect, NetworkTimeout, NotPrimaryError and WaitQueueTimeoutError all derive
        # from ConnectionFailure.
        return PersistenceUnavailableError(collection=collection)
    if isinstance(exc, driver.OperationFailure):
        code = getattr(exc, "code", None)
        if code in (13, 18):                             # Unauthorized, AuthenticationFailed
            return PersistenceError("authorization_failed", collection=collection)
        if code in (85, 86):                             # IndexOptionsConflict, IndexKeySpecsConflict
            return IndexConflictError(collection=collection)
        return PersistenceError("operation_failed", collection=collection)
    if isinstance(exc, driver.PyMongoError):
        return PersistenceError("driver_error", collection=collection)
    return PersistenceError("unexpected_error", collection=collection)
