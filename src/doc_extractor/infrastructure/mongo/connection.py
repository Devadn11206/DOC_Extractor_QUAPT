"""
Centralised MongoDB connection management (Team Member 3, Phase 2A-1).

ONE ``MongoConnection`` per process owns ONE asynchronous client. Everything that talks to MongoDB goes through it, so
timeouts, write and read concerns, replica-set checks, health and shutdown exist in exactly one place.

LIFECYCLE
- Construction is pure: it validates nothing external, opens no socket and imports no driver. The application container
  builds it during composition (a synchronous step).
- ``connect()`` runs inside the event loop at application start-up: it creates the client, pings the server and, when a
  replica set is required, verifies the server really is one (and the expected one). Any failure closes the half-open client
  and raises a typed error with a fixed message; the application does not start, and there is NO fallback to another store.
- ``aclose()`` closes the client at shutdown. Both are idempotent.

DEFAULTS THE FOUNDATION IMPOSES (the connection string cannot weaken them: explicit options override string options)
- write concern ``majority`` with journaling; read concern ``majority``; read preference ``primary``;
  retryable reads and writes on; UTC-aware datetimes; standard UUID representation;
- bounded server-selection, connect and socket timeouts and a bounded pool (see ``MongoSettings``).

HEALTH
- ``health()`` always probes. ``cached_health()`` is what the unauthenticated HTTP health endpoint uses: a short-lived result
  cache with ONE probe in flight, so a flood of anonymous requests costs at most one ping per interval (see its docstring).

The driver (``pymongo``, extra ``mongo``) is imported only when a client is created. Tests and the rest of the application
import this module without it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timezone
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple, TypeVar

from doc_extractor.infrastructure.mongo.errors import (
    PersistenceConfigurationError,
    PersistenceError,
    PersistenceUnavailableError,
    TransactionUnavailableError,
    is_driver_error,
    translate_driver_error,
)
from doc_extractor.infrastructure.mongo.settings import MongoSettings

logger = logging.getLogger("doc_extractor.infrastructure.mongo")

T = TypeVar("T")
ClientFactory = Callable[..., Any]

STATUS_OK = "ok"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_UNAVAILABLE = "unavailable"

# Health-probe cache (see ``MongoConnection.cached_health``). A result younger than the TTL is returned as is. An older one
# (up to the staleness bound) is still returned immediately while ONE refresh runs in the background, so a slow or hung
# database never makes a liveness probe wait. Older than that, a caller waits for the shared refresh.
HEALTH_CACHE_TTL_SECONDS = 5.0
HEALTH_MAX_STALE_SECONDS = 30.0
_MAX_HEALTH_TTL_SECONDS = 60.0
_MAX_HEALTH_STALE_SECONDS = 300.0


@dataclass(frozen=True)
class MongoHealth:
    """Result of a health probe. Never carries a host, credential, connection string or driver text."""

    status: str
    replica_set: Optional[str] = None
    latency_ms: Optional[float] = None
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_public(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"status": self.status}
        if self.replica_set is not None:
            data["replica_set"] = self.replica_set
        if self.latency_ms is not None:
            data["latency_ms"] = self.latency_ms
        if self.reason is not None:
            data["reason"] = self.reason
        return data


def default_client_factory(uri: str, **options: Any) -> Any:
    """Create the asynchronous PyMongo client. Imported here so the driver is needed only when a connection is made."""
    try:
        from pymongo import AsyncMongoClient
    except ImportError:
        raise PersistenceConfigurationError("mongo_driver_is_not_installed", setting="mongo extra") from None
    return AsyncMongoClient(uri, **options)


def build_client_options(settings: MongoSettings) -> Dict[str, Any]:
    """The explicit client options derived from the settings. Explicit options win over the connection string."""
    options: Dict[str, Any] = {
        "appname": settings.app_name,
        "serverSelectionTimeoutMS": settings.server_selection_timeout_ms,
        "connectTimeoutMS": settings.connect_timeout_ms,
        "socketTimeoutMS": settings.socket_timeout_ms,
        "waitQueueTimeoutMS": settings.server_selection_timeout_ms,
        "maxPoolSize": settings.max_pool_size,
        "minPoolSize": settings.min_pool_size,
        "retryWrites": True,
        "retryReads": True,
        "w": "majority",
        "journal": True,
        "readConcernLevel": "majority",
        "readPreference": "primary",
        "tz_aware": True,
        "tzinfo": timezone.utc,
        "uuidRepresentation": "standard",
    }
    if settings.replica_set is not None:
        options["replicaSet"] = settings.replica_set
    return options


class MongoConnection:
    """The process's single MongoDB connection. See the module docstring for the lifecycle."""

    def __init__(
        self,
        settings: MongoSettings,
        *,
        client_factory: Optional[ClientFactory] = None,
        health_cache_ttl: float = HEALTH_CACHE_TTL_SECONDS,
        health_max_stale: float = HEALTH_MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < health_cache_ttl <= _MAX_HEALTH_TTL_SECONDS:
            raise ValueError("The health cache interval must be positive and at most 60 seconds.")
        if not health_cache_ttl <= health_max_stale <= _MAX_HEALTH_STALE_SECONDS:
            raise ValueError("The health staleness bound must be between the cache interval and 300 seconds.")
        self._settings = settings
        self._factory = client_factory or default_client_factory
        self._client: Optional[Any] = None
        self._replica_set_name: Optional[str] = None
        self._lock = asyncio.Lock()
        # Health cache state is PER INSTANCE (no module-level lock or task shared between applications or event loops).
        self._health_ttl = health_cache_ttl
        self._health_max_stale = health_max_stale
        self._clock = clock
        self._health_cache: Optional[Tuple[float, MongoHealth]] = None
        self._health_task: Optional["asyncio.Task[MongoHealth]"] = None

    # -- state -----------------------------------------------------------------------------------------------------------

    @property
    def settings(self) -> MongoSettings:
        return self._settings

    @property
    def is_connected(self) -> bool:
        return self._client is not None

    @property
    def replica_set_name(self) -> Optional[str]:
        """The replica set the server reported at connect time (``None``: standalone or not connected)."""
        return self._replica_set_name

    @property
    def supports_transactions(self) -> bool:
        return self._client is not None and self._replica_set_name is not None

    @property
    def client(self) -> Any:
        if self._client is None:
            raise PersistenceUnavailableError("not_connected")
        return self._client

    @property
    def database(self) -> Any:
        return self.client[self._settings.database]

    def collection(self, name: str) -> Any:
        return self.database[name]

    # -- lifecycle -------------------------------------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect and verify. Idempotent. A failure leaves nothing open and raises a typed error."""
        async with self._lock:
            if self._client is not None:
                return
            client = self._factory(self._settings.uri.get_secret_value(), **build_client_options(self._settings))
            try:
                await client.admin.command("ping")
                hello = await client.admin.command("hello")
                set_name = hello.get("setName") if isinstance(hello, dict) else None
                if self._settings.require_replica_set and not set_name:
                    raise PersistenceConfigurationError("deployment_is_not_a_replica_set", setting="mongo_uri")
                expected = self._settings.effective_replica_set
                if expected is not None and set_name is not None and set_name != expected:
                    raise PersistenceConfigurationError("replica_set_name_does_not_match", setting="mongo_replica_set")
            except PersistenceConfigurationError:
                await self._close_quietly(client)
                logger.error("MongoDB connection refused by configuration checks (reason=configuration)")
                raise
            except Exception as exc:                                     # noqa: BLE001 - mapped to a typed error below
                await self._close_quietly(client)
                error = translate_driver_error(exc)
                logger.error("MongoDB connection failed (reason=%s)", error.details.get("reason"))
                raise error from None
            self._client = client
            self._replica_set_name = set_name
            self._health_cache = None
            logger.info("MongoDB connected (%s)", _log_summary(self._settings, set_name))

    async def aclose(self) -> None:
        """Close the client. Idempotent; a failing close is logged by reason and never raised from shutdown."""
        async with self._lock:
            client, self._client, self._replica_set_name = self._client, None, None
            task, self._health_task, self._health_cache = self._health_task, None, None
        if task is not None and not task.done():
            try:
                task.cancel()
            except RuntimeError:                                         # its event loop is already closed
                pass
        if client is not None:
            await self._close_quietly(client)
            logger.info("MongoDB connection closed")

    async def __aenter__(self) -> "MongoConnection":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    @staticmethod
    async def _close_quietly(client: Any) -> None:
        try:
            result = client.close()
            if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
                await result
        except Exception:                                                # noqa: BLE001 - shutdown must not raise
            logger.warning("MongoDB client close failed (reason=close_failed)")

    # -- health ----------------------------------------------------------------------------------------------------------

    async def health(self) -> MongoHealth:
        """Probe the server with a bounded ping. NEVER raises; reports a fixed status and reason only."""
        client = self._client
        if client is None:
            return MongoHealth(status=STATUS_NOT_CONNECTED, reason="not_connected")
        started = time.perf_counter()
        try:
            await asyncio.wait_for(
                client.admin.command("ping"), timeout=self._settings.server_selection_timeout_ms / 1000 + 1.0
            )
        except Exception as exc:                                         # noqa: BLE001 - reported, not raised
            reason = translate_driver_error(exc).details.get("reason") if not isinstance(exc, asyncio.TimeoutError) else "timeout"
            return MongoHealth(status=STATUS_UNAVAILABLE, replica_set=self._replica_set_name, reason=str(reason))
        latency = round((time.perf_counter() - started) * 1000, 1)
        return MongoHealth(status=STATUS_OK, replica_set=self._replica_set_name, latency_ms=latency)

    async def cached_health(self) -> MongoHealth:
        """
        ``health()`` behind a short-lived cache, for an endpoint that anonymous callers can hit at any rate. NEVER raises.

        - A result younger than the TTL (monotonic clock) is returned without any I/O, healthy or not.
        - Otherwise ONE probe is started; concurrent callers share it instead of starting their own.
        - While a refresh runs, a cached result that is not older than the staleness bound is returned immediately: during an
          outage a liveness probe is never held for a server-selection timeout. Only when there is no usable cached result
          does a caller wait for the shared probe.
        - Recovery (or a new outage) becomes visible as soon as the refresh that the first request after the TTL started has
          finished, i.e. to the next request.
        - Not connected: answered without I/O and never cached.
        """
        if self._client is None:
            return await self.health()
        cached = self._health_cache
        now = self._clock()
        if cached is not None and now - cached[0] < self._health_ttl:
            return cached[1]
        task = self._shared_probe()
        if cached is not None and now - cached[0] < self._health_max_stale:
            return cached[1]
        try:
            return await asyncio.shield(task)          # shield: one caller going away must not cancel the shared probe
        except asyncio.CancelledError:
            if task.cancelled():                       # cancelled by aclose(), not by this caller
                return MongoHealth(status=STATUS_NOT_CONNECTED, reason="not_connected")
            raise

    def _shared_probe(self) -> "asyncio.Task[MongoHealth]":
        task = self._health_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(self._probe_and_cache())
            self._health_task = task
        return task

    async def _probe_and_cache(self) -> MongoHealth:
        result = await self.health()
        if self._client is not None:                   # closed while probing: nothing to remember
            self._health_cache = (self._clock(), result)
        return result

    # -- transactions ----------------------------------------------------------------------------------------------------

    async def run_in_transaction(
        self, callback: Callable[[Any], Awaitable[T]], *, max_commit_time_ms: Optional[int] = None
    ) -> T:
        """
        Run ``callback(session)`` in a transaction with majority concerns on the primary, retrying transient errors and an
        unknown commit result as the driver specifies. Only on a replica set; otherwise ``TransactionUnavailableError``.
        Every database call inside the callback must pass the ``session`` it receives (reads too).

        A driver or BSON error becomes a typed ``PersistenceError`` (fixed message, no driver text). Any other exception
        raised by the callback aborts the transaction and propagates unchanged.
        """
        if not self.supports_transactions:
            raise TransactionUnavailableError()
        try:
            from pymongo.read_concern import ReadConcern
            from pymongo.read_preferences import ReadPreference
            from pymongo.write_concern import WriteConcern
        except ImportError:
            raise PersistenceConfigurationError("mongo_driver_is_not_installed", setting="mongo extra") from None
        try:
            # ``start_session()`` is a plain method returning an async context manager; it is NOT awaitable.
            async with self.client.start_session() as session:
                return await session.with_transaction(
                    callback,
                    read_concern=ReadConcern("majority"),
                    write_concern=WriteConcern("majority", j=True),
                    read_preference=ReadPreference.PRIMARY,
                    max_commit_time_ms=max_commit_time_ms,
                )
        except PersistenceError:
            raise
        except Exception as exc:                                         # noqa: BLE001 - narrowed on the next line
            if not is_driver_error(exc):
                raise
            # ONLY driver and BSON errors are translated. An application exception raised by the callback (a validation or
            # conflict error, anything) is not ours to disguise: ``with_transaction`` has already aborted the transaction and
            # it propagates with its own type. Cancellation is a BaseException and is never caught here.
            raise translate_driver_error(exc) from None


def _log_summary(settings: MongoSettings, replica_set: Optional[str]) -> str:
    """Non-secret one-line description: database name and topology only, never a host or credential."""
    summary = settings.summary()
    return "database=%s replica_set=%s hosts=%s" % (summary["database"], replica_set or "none", summary["host_count"])
