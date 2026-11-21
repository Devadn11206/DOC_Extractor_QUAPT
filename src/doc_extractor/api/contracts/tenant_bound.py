"""
One tenant-identifier rule for every API model that carries a ``tenant_id`` (review finding R11).

Principals were validated; the persisted records (API keys, review tasks, webhooks, audit events, queue messages) accepted
any non-empty string, so a record could name a tenant that storage would later refuse (or, worse, that could address
another tenant's directory). Every tenant-bearing model now inherits ``TenantBoundModel``: the same check, through every
way pydantic can produce an instance. The rule is ``doc_extractor.tenancy.require_path_safe_tenant_id`` (a safety bound, not
a tenant-ID format: that policy is decision M5-D16, Team Member 3's).
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, field_validator

from doc_extractor.tenancy import require_path_safe_tenant_id


class TenantBoundModel(BaseModel):
    """
    Base of every model that carries the authoritative ``tenant_id``.

    The tenant is checked when the model is created (a field validator) AND on every other way of producing an
    instance that pydantic does not validate: ``model_copy(update=...)``, the deprecated ``copy(update=...)``,
    ``copy.replace`` (``__replace__``) and ``model_construct``. Without this, ``principal.model_copy(update=
    {"tenant_id": "a/../b"})`` yielded a principal that no validator had seen.

    ``object.__setattr__`` on a frozen instance cannot be intercepted from Python; the storage layer
    (``doc_extractor.tenancy``) re-verifies the tenant at use, so that is a second, independent boundary.
    """

    @field_validator("tenant_id", check_fields=False)
    @classmethod
    def _tenant_id_must_be_usable(cls, value: str) -> str:
        """Tenant IDs name storage directories: reject values that could address another tenant (SEC-006)."""
        return require_path_safe_tenant_id(value)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None, deep: bool = False):  # type: ignore[override]
        clone = super().model_copy(update=update, deep=deep)
        require_path_safe_tenant_id(getattr(clone, "tenant_id", None))
        return clone

    def copy(self, *, include=None, exclude=None, update=None, deep: bool = False):  # type: ignore[override]
        if include is not None or exclude is not None:
            raise TypeError("include/exclude are not supported: use model_copy(update=...)")
        return self.model_copy(update=update, deep=deep)

    def __replace__(self, **changes: Any):
        return self.model_copy(update=changes)

    @classmethod
    def model_construct(cls, _fields_set=None, **values: Any):  # type: ignore[override]
        instance = super().model_construct(_fields_set, **values)
        require_path_safe_tenant_id(getattr(instance, "tenant_id", None))
        return instance
