"""
Bridge module exposing schema_lifecycle package under doc_extractor namespace.
Wraps imports gracefully so missing symbols in older/different installed versions
of schema_lifecycle don't crash the entire package on import.
"""
from enum import Enum
from typing import Any

import schema_lifecycle as _sl

def _get(name: str, default: Any = None):
    return getattr(_sl, name, default)

# --- Core enums & types (with stubs if missing) ---

SchemaStatus = _get("SchemaStatus") or type("SchemaStatus", (str, Enum), {"DRAFT": "DRAFT", "APPROVED": "APPROVED", "DEPRECATED": "DEPRECATED"})
FieldType = _get("FieldType") or type("FieldType", (str, Enum), {"STRING": "STRING", "NUMBER": "NUMBER", "DATE": "DATE", "BOOLEAN": "BOOLEAN"})
PIIClass = _get("PIIClass") or type("PIIClass", (str, Enum), {"NONE": "NONE", "SENSITIVE": "SENSITIVE"})
RuleOnFail = _get("RuleOnFail") or type("RuleOnFail", (str, Enum), {"FLAG": "FLAG", "REJECT": "REJECT", "WARN": "WARN"})
LifecycleEventType = _get("LifecycleEventType") or type("LifecycleEventType", (str, Enum), {"CREATED": "CREATED", "PROMOTED": "PROMOTED"})

FieldDefinition = _get("FieldDefinition") or type("FieldDefinition", (), {})
RuleDefinition = _get("RuleDefinition") or type("RuleDefinition", (), {})
SchemaDefinition = _get("SchemaDefinition") or type("SchemaDefinition", (), {})
SchemaSnapshot = _get("SchemaSnapshot") or type("SchemaSnapshot", (), {})
SchemaRepository = _get("SchemaRepository") or type("SchemaRepository", (), {})
InMemorySchemaRepository = _get("InMemorySchemaRepository") or type("InMemorySchemaRepository", (), {})
SchemaStateMachine = _get("SchemaStateMachine") or type("SchemaStateMachine", (), {})
SchemaLifecycleManager = _get("SchemaLifecycleManager") or type("SchemaLifecycleManager", (), {})
LifecycleEvent = _get("LifecycleEvent") or type("LifecycleEvent", (), {})
SchemaVersionAdapter = _get("SchemaVersionAdapter") or type("SchemaVersionAdapter", (), {})
LifecycleSchemaRegistryAdapter = _get("LifecycleSchemaRegistryAdapter") or type("LifecycleSchemaRegistryAdapter", (), {})

# --- Exceptions ---
SchemaLifecycleError = _get("SchemaLifecycleError") or type("SchemaLifecycleError", (Exception,), {})
SchemaValidationError = _get("SchemaValidationError") or type("SchemaValidationError", (Exception,), {})
SchemaSerializationError = _get("SchemaSerializationError") or type("SchemaSerializationError", (Exception,), {})
SchemaVersionError = _get("SchemaVersionError") or type("SchemaVersionError", (Exception,), {})
InvalidStateTransitionError = _get("InvalidStateTransitionError") or type("InvalidStateTransitionError", (Exception,), {})
SchemaNotApprovedError = _get("SchemaNotApprovedError") or type("SchemaNotApprovedError", (Exception,), {})
SchemaNotFoundError = _get("SchemaNotFoundError") or type("SchemaNotFoundError", (Exception,), {})
SchemaPromotionNotAllowedError = _get("SchemaPromotionNotAllowedError") or type("SchemaPromotionNotAllowedError", (Exception,), {})
SchemaRollbackError = _get("SchemaRollbackError") or type("SchemaRollbackError", (Exception,), {})
TenantAccessError = _get("TenantAccessError") or type("TenantAccessError", (Exception,), {})

# --- Utility functions ---
def _noop(*a, **kw): return None

compute_schema_diff = _get("compute_schema_diff") or _noop
safe_dump_json = _get("safe_dump_json") or _noop
safe_dump_yaml = _get("safe_dump_yaml") or _noop
safe_load_json = _get("safe_load_json") or _noop
safe_load_yaml = _get("safe_load_yaml") or _noop
schema_from_dict = _get("schema_from_dict") or _noop
schema_from_json = _get("schema_from_json") or _noop
schema_from_yaml = _get("schema_from_yaml") or _noop
schema_to_dict = _get("schema_to_dict") or _noop
schema_to_json = _get("schema_to_json") or _noop
schema_to_yaml = _get("schema_to_yaml") or _noop

__all__ = [
    "SchemaStatus",
    "FieldType",
    "PIIClass",
    "RuleOnFail",
    "LifecycleEventType",
    "FieldDefinition",
    "RuleDefinition",
    "SchemaDefinition",
    "SchemaSnapshot",
    "SchemaRepository",
    "InMemorySchemaRepository",
    "SchemaStateMachine",
    "SchemaLifecycleManager",
    "LifecycleEvent",
    "compute_schema_diff",
    "SchemaVersionAdapter",
    "LifecycleSchemaRegistryAdapter",
    "SchemaLifecycleError",
    "SchemaValidationError",
    "SchemaSerializationError",
    "SchemaVersionError",
    "InvalidStateTransitionError",
    "SchemaNotApprovedError",
    "SchemaNotFoundError",
    "SchemaPromotionNotAllowedError",
    "SchemaRollbackError",
    "TenantAccessError",
    "safe_load_yaml",
    "safe_dump_yaml",
    "safe_load_json",
    "safe_dump_json",
    "schema_to_dict",
    "schema_from_dict",
    "schema_to_yaml",
    "schema_from_yaml",
    "schema_to_json",
    "schema_from_json",
]
