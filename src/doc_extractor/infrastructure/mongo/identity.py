"""
MongoDB-backed persistent identity repositories (Team Member 3, Phase 5C).

Provides durable persistence for:
- User accounts and tenant associations (PRD §10.1 SEC-006, SEC-003).
- Rotating single-use refresh tokens with atomic conditional consumption (PRD §10.1 SEC-002).
- Token replay detection and family-wide revocation.
- Session revocation and JTI blacklisting.
- Account activation and deactivation.
- Multi-Factor Authentication (MFA) state persistence.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
from datetime import datetime, timezone
import logging
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from doc_extractor.infrastructure.identity.errors import (
    IdentityConfigurationError,
    InvalidCredentialsError,
    TokenReplayError,
    TokenRevokedError,
)
from doc_extractor.infrastructure.identity.models import RefreshTokenRecord, UserRecord
from doc_extractor.infrastructure.mongo.errors import PersistenceError, translate_driver_error
from doc_extractor.infrastructure.mongo.indexes import (
    ASCENDING,
    IndexBootstrapReport,
    IndexRegistry,
    IndexSpec,
    ensure_indexes,
)
from doc_extractor.infrastructure.mongo.repository import (
    MAX_PAGE_SIZE,
    TenantScopedRepository,
    validate_tenant,
)

logger = logging.getLogger("doc_extractor.infrastructure.mongo.identity")

USERS_COLLECTION = "users"
REFRESH_TOKENS_COLLECTION = "refresh_tokens"
TOKEN_REVOCATIONS_COLLECTION = "token_revocations"

IDENTITY_INDEXES: Sequence[IndexSpec] = (
    # Users collection
    IndexSpec(
        USERS_COLLECTION,
        (("tenant_id", ASCENDING), ("user_id", ASCENDING)),
        "usr_tenant_user_uq",
        unique=True,
    ),
    IndexSpec(
        USERS_COLLECTION,
        (("tenant_id", ASCENDING), ("email", ASCENDING)),
        "usr_tenant_email_uq",
        unique=True,
    ),
    # Refresh tokens collection
    IndexSpec(
        REFRESH_TOKENS_COLLECTION,
        (("token_hash", ASCENDING),),
        "rt_token_hash_uq",
        unique=True,
        tenant_exempt=True,
    ),
    IndexSpec(
        REFRESH_TOKENS_COLLECTION,
        (("tenant_id", ASCENDING), ("token_id", ASCENDING)),
        "rt_tenant_token_uq",
        unique=True,
    ),
    IndexSpec(
        REFRESH_TOKENS_COLLECTION,
        (("tenant_id", ASCENDING), ("family_id", ASCENDING)),
        "rt_tenant_family",
    ),
    IndexSpec(
        REFRESH_TOKENS_COLLECTION,
        (("tenant_id", ASCENDING), ("user_id", ASCENDING)),
        "rt_tenant_user",
    ),
    IndexSpec(
        REFRESH_TOKENS_COLLECTION,
        (("expires_at", ASCENDING),),
        "rt_expires_ttl",
        expire_after_seconds=0,
        tenant_exempt=True,
    ),
    # Token revocations collection
    IndexSpec(
        TOKEN_REVOCATIONS_COLLECTION,
        (("jti", ASCENDING),),
        "rev_jti_uq",
        unique=True,
        tenant_exempt=True,
    ),
    IndexSpec(
        TOKEN_REVOCATIONS_COLLECTION,
        (("tenant_id", ASCENDING), ("user_id", ASCENDING)),
        "rev_tenant_user_uq",
        unique=True,
    ),
    IndexSpec(
        TOKEN_REVOCATIONS_COLLECTION,
        (("expires_at", ASCENDING),),
        "rev_expires_ttl",
        expire_after_seconds=0,
        tenant_exempt=True,
    ),
)


async def ensure_identity_indexes(database: Any) -> IndexBootstrapReport:
    """Idempotently bootstrap indexes for persistent identity collections."""
    registry = IndexRegistry(IDENTITY_INDEXES)
    return await ensure_indexes(database, registry=registry)


def _run_coroutine_sync(coro: Any) -> Any:
    """Safely executes an asynchronous coroutine from a synchronous context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(lambda: asyncio.run(coro)).result()
    return asyncio.run(coro)


class _ConnWrapper:
    def __init__(self, db: Any) -> None:
        self._db = db

    def collection(self, name: str) -> Any:
        return self._db[name]

    @property
    def database(self) -> Any:
        return self._db


class MongoUserRepository(TenantScopedRepository):
    """
    Durable MongoDB-backed user repository with strict tenant isolation.
    """

    def __init__(self, connection: Any) -> None:
        conn = connection if hasattr(connection, "collection") else _ConnWrapper(connection)
        super().__init__(conn, USERS_COLLECTION)

    def _doc_to_user(self, doc: Dict[str, Any]) -> UserRecord:
        return UserRecord(
            user_id=doc["user_id"],
            tenant_id=doc["tenant_id"],
            email=doc["email"],
            hashed_password=doc.get("hashed_password"),
            roles=set(doc.get("roles", ["analyst"])),
            scopes=set(doc.get("scopes", [])),
            mfa_enabled=bool(doc.get("mfa_enabled", False)),
            mfa_secret=doc.get("mfa_secret"),
            is_active=bool(doc.get("is_active", True)),
            created_at=doc["created_at"] if isinstance(doc.get("created_at"), datetime) else datetime.now(timezone.utc),
            updated_at=doc["updated_at"] if isinstance(doc.get("updated_at"), datetime) else datetime.now(timezone.utc),
            external_idp_id=doc.get("external_idp_id"),
            external_subject=doc.get("external_subject"),
        )

    async def async_save_user(self, user: UserRecord) -> UserRecord:
        validate_tenant(user.tenant_id)
        existing = await self.find_one(user.tenant_id, {"user_id": user.user_id})
        doc_data = {
            "user_id": user.user_id,
            "tenant_id": user.tenant_id,
            "email": user.email.lower().strip(),
            "hashed_password": user.hashed_password,
            "roles": sorted(user.roles),
            "scopes": sorted(user.scopes),
            "mfa_enabled": user.mfa_enabled,
            "mfa_secret": user.mfa_secret,
            "is_active": user.is_active,
            "updated_at": datetime.now(timezone.utc),
            "external_idp_id": user.external_idp_id,
            "external_subject": user.external_subject,
        }
        if existing is None:
            doc_data["created_at"] = user.created_at
            await self.insert_one(user.tenant_id, doc_data)
        else:
            await self.update_one(
                user.tenant_id,
                {"user_id": user.user_id},
                {"$set": doc_data},
            )
        return user

    def save_user(self, user: UserRecord) -> UserRecord:
        return _run_coroutine_sync(self.async_save_user(user))

    async def async_get_user(self, tenant_id: str, user_id: str) -> Optional[UserRecord]:
        doc = await self.find_one(tenant_id, {"user_id": user_id})
        return self._doc_to_user(doc) if doc else None

    def get_user(self, tenant_id: str, user_id: str) -> Optional[UserRecord]:
        return _run_coroutine_sync(self.async_get_user(tenant_id, user_id))

    async def async_get_user_by_email(self, tenant_id: str, email: str) -> Optional[UserRecord]:
        clean_email = email.lower().strip()
        doc = await self.find_one(tenant_id, {"email": clean_email})
        return self._doc_to_user(doc) if doc else None

    def get_user_by_email(self, tenant_id: str, email: str) -> Optional[UserRecord]:
        return _run_coroutine_sync(self.async_get_user_by_email(tenant_id, email))

    async def async_update_user(
        self,
        tenant_id: str,
        user_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[UserRecord]:
        allowed_updates: Dict[str, Any] = {}
        for k in ("roles", "scopes", "mfa_enabled", "mfa_secret", "is_active", "hashed_password", "external_idp_id", "external_subject"):
            if k in update_data:
                val = update_data[k]
                if isinstance(val, set):
                    allowed_updates[k] = sorted(val)
                else:
                    allowed_updates[k] = val
        if not allowed_updates:
            return await self.async_get_user(tenant_id, user_id)

        allowed_updates["updated_at"] = datetime.now(timezone.utc)
        res = await self.update_one(tenant_id, {"user_id": user_id}, {"$set": allowed_updates})
        if res == 0:
            return None
        return await self.async_get_user(tenant_id, user_id)

    def update_user(
        self,
        tenant_id: str,
        user_id: str,
        update_data: Dict[str, Any],
    ) -> Optional[UserRecord]:
        return _run_coroutine_sync(self.async_update_user(tenant_id, user_id, update_data))

    async def async_set_active(self, tenant_id: str, user_id: str, is_active: bool) -> bool:
        res = await self.update_one(
            tenant_id,
            {"user_id": user_id},
            {"$set": {"is_active": is_active, "updated_at": datetime.now(timezone.utc)}},
        )
        return bool(res > 0)

    def set_active(self, tenant_id: str, user_id: str, is_active: bool) -> bool:
        return _run_coroutine_sync(self.async_set_active(tenant_id, user_id, is_active))

    async def async_list_users(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[UserRecord]:
        capped = min(max(1, limit), MAX_PAGE_SIZE)
        docs = (await self.find_many(tenant_id, {}, limit=capped + offset, sort=[("created_at", ASCENDING)]))[offset:]
        return [self._doc_to_user(d) for d in docs]

    def list_users(
        self,
        tenant_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> List[UserRecord]:
        return _run_coroutine_sync(self.async_list_users(tenant_id, limit, offset))


class MongoRefreshTokenRepository:
    """
    Durable MongoDB-backed repository for rotating refresh tokens.
    Supports atomic single-use token consumption, token family replay detection,
    and family-wide invalidation.
    """

    def __init__(self, connection: Any) -> None:
        if hasattr(connection, "collection"):
            self._connection = connection
        else:
            self._connection = _ConnWrapper(connection)
        self._col = self._connection.collection(REFRESH_TOKENS_COLLECTION)

    def _doc_to_record(self, doc: Dict[str, Any]) -> RefreshTokenRecord:
        return RefreshTokenRecord(
            token_id=doc["token_id"],
            token_hash=doc["token_hash"],
            family_id=doc["family_id"],
            user_id=doc["user_id"],
            tenant_id=doc["tenant_id"],
            roles=set(doc.get("roles", [])),
            scopes=set(doc.get("scopes", [])),
            mfa_verified=bool(doc.get("mfa_verified", False)),
            expires_at=doc["expires_at"],
            created_at=doc["created_at"],
            revoked=bool(doc.get("revoked", False)),
            used=bool(doc.get("used", False)),
            replaced_by=doc.get("replaced_by"),
        )

    async def async_save_token(self, record: RefreshTokenRecord) -> None:
        doc = {
            "token_id": record.token_id,
            "token_hash": record.token_hash,
            "family_id": record.family_id,
            "user_id": record.user_id,
            "tenant_id": record.tenant_id,
            "roles": sorted(record.roles),
            "scopes": sorted(record.scopes),
            "mfa_verified": record.mfa_verified,
            "expires_at": record.expires_at,
            "created_at": record.created_at,
            "revoked": record.revoked,
            "used": record.used,
            "replaced_by": record.replaced_by,
        }
        try:
            await self._col.insert_one(doc)
        except Exception as exc:
            raise translate_driver_error(exc, collection=REFRESH_TOKENS_COLLECTION) from None

    def save_token(self, record: RefreshTokenRecord) -> None:
        _run_coroutine_sync(self.async_save_token(record))

    async def async_get_by_hash(self, token_hash: str) -> Optional[RefreshTokenRecord]:
        try:
            doc = await self._col.find_one({"token_hash": token_hash})
        except Exception as exc:
            raise translate_driver_error(exc, collection=REFRESH_TOKENS_COLLECTION) from None
        return self._doc_to_record(doc) if doc else None

    def get_by_hash(self, token_hash: str) -> Optional[RefreshTokenRecord]:
        return _run_coroutine_sync(self.async_get_by_hash(token_hash))

    async def async_mark_used(self, token_id: str, replaced_by: str) -> bool:
        """
        Atomically mark a refresh token as used.
        Returns True if the token was successfully transitioned from used=False to used=True.
        Returns False if the token was already consumed or revoked (concurrent request / replay).
        """
        now = datetime.now(timezone.utc)
        try:
            res = await self._col.update_one(
                {"token_id": token_id, "used": False, "revoked": False},
                {"$set": {"used": True, "replaced_by": replaced_by, "used_at": now}},
            )
            return bool(res.modified_count > 0)
        except Exception as exc:
            raise translate_driver_error(exc, collection=REFRESH_TOKENS_COLLECTION) from None

    def mark_used(self, token_id: str, replaced_by: str) -> bool:
        return _run_coroutine_sync(self.async_mark_used(token_id, replaced_by))

    async def async_revoke_family(self, family_id: str) -> int:
        """Revoke all tokens in a family (triggered upon replay detection)."""
        try:
            res = await self._col.update_many(
                {"family_id": family_id},
                {"$set": {"revoked": True, "revoked_at": datetime.now(timezone.utc)}},
            )
            count = int(res.modified_count)
            logger.warning(
                "Family '%s' revoked due to token replay attack (%d tokens invalidated)",
                family_id,
                count,
            )
            return count
        except Exception as exc:
            raise translate_driver_error(exc, collection=REFRESH_TOKENS_COLLECTION) from None

    def revoke_family(self, family_id: str) -> int:
        return _run_coroutine_sync(self.async_revoke_family(family_id))

    async def async_revoke_user_tokens(self, user_id: str, tenant_id: str) -> int:
        """Revoke all tokens for a user in a tenant."""
        validate_tenant(tenant_id)
        try:
            res = await self._col.update_many(
                {"tenant_id": tenant_id, "user_id": user_id, "revoked": False},
                {"$set": {"revoked": True, "revoked_at": datetime.now(timezone.utc)}},
            )
            return int(res.modified_count)
        except Exception as exc:
            raise translate_driver_error(exc, collection=REFRESH_TOKENS_COLLECTION) from None

    def revoke_user_tokens(self, user_id: str, tenant_id: str) -> int:
        return _run_coroutine_sync(self.async_revoke_user_tokens(user_id, tenant_id))


class MongoTokenRevocationStore:
    """
    Durable MongoDB-backed token and session revocation store.
    Tracks blacklisted JTIs and user session invalidation timestamps.
    """

    def __init__(self, connection: Any) -> None:
        if hasattr(connection, "collection"):
            self._connection = connection
        else:
            self._connection = _ConnWrapper(connection)
        self._col = self._connection.collection(TOKEN_REVOCATIONS_COLLECTION)

    async def async_revoke_jti(self, jti: str, expires_at: datetime) -> None:
        doc = {
            "jti": jti,
            "expires_at": expires_at,
            "revoked_at": datetime.now(timezone.utc),
        }
        try:
            existing = await self._col.find_one({"jti": jti})
            if existing is None:
                await self._col.insert_one(doc)
            else:
                await self._col.update_one({"jti": jti}, {"$set": doc})
        except Exception as exc:
            raise translate_driver_error(exc, collection=TOKEN_REVOCATIONS_COLLECTION) from None

    def revoke_jti(self, jti: str, expires_at: datetime) -> None:
        _run_coroutine_sync(self.async_revoke_jti(jti, expires_at))

    async def async_is_jti_revoked(self, jti: str) -> bool:
        try:
            doc = await self._col.find_one({"jti": jti})
            if doc is None:
                return False
            expires_at = doc.get("expires_at")
            if isinstance(expires_at, datetime) and datetime.now(timezone.utc) >= expires_at:
                return False
            return True
        except Exception as exc:
            raise translate_driver_error(exc, collection=TOKEN_REVOCATIONS_COLLECTION) from None

    def is_jti_revoked(self, jti: str) -> bool:
        return _run_coroutine_sync(self.async_is_jti_revoked(jti))

    async def async_revoke_user_sessions(
        self,
        user_id: str,
        tenant_id: str,
        revoked_at: Optional[datetime] = None,
    ) -> None:
        validate_tenant(tenant_id)
        at = revoked_at or datetime.now(timezone.utc)
        doc = {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "revoked_at": at,
        }
        try:
            existing = await self._col.find_one({"tenant_id": tenant_id, "user_id": user_id})
            if existing is None:
                await self._col.insert_one(doc)
            else:
                await self._col.update_one({"tenant_id": tenant_id, "user_id": user_id}, {"$set": doc})
        except Exception as exc:
            raise translate_driver_error(exc, collection=TOKEN_REVOCATIONS_COLLECTION) from None

    def revoke_user_sessions(
        self,
        user_id: str,
        tenant_id: str,
        revoked_at: Optional[datetime] = None,
    ) -> None:
        _run_coroutine_sync(self.async_revoke_user_sessions(user_id, tenant_id, revoked_at))

    async def async_is_user_revoked(
        self,
        user_id: str,
        tenant_id: str,
        issued_at: datetime,
    ) -> bool:
        validate_tenant(tenant_id)
        try:
            doc = await self._col.find_one({"tenant_id": tenant_id, "user_id": user_id})
            if doc is None:
                return False
            revoked_at = doc.get("revoked_at")
            if isinstance(revoked_at, datetime):
                return issued_at <= revoked_at
            return False
        except Exception as exc:
            raise translate_driver_error(exc, collection=TOKEN_REVOCATIONS_COLLECTION) from None

    def is_user_revoked(
        self,
        user_id: str,
        tenant_id: str,
        issued_at: datetime,
    ) -> bool:
        return _run_coroutine_sync(self.async_is_user_revoked(user_id, tenant_id, issued_at))
