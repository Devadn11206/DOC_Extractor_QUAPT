"""
Document Extraction Engine - REST API Foundation (Module M5).

Provides HTTP application factory, request middleware, centralized configuration,
canonical API-010 error handling, authentication/tenant interfaces, repository and
durable queue protocols.
"""

try:
    from doc_extractor.api.app import create_app
except RuntimeError as _err:
    if "python-multipart" in str(_err):
        def create_app(*args, **kwargs):  # type: ignore[no-redef]
            raise RuntimeError('Form data requires "python-multipart" to be installed.')
    else:
        raise
from doc_extractor.api.composition import (
    ApplicationContainer,
    TenantComponentCache,
    TenantComponents,
    ThreadSafeRoutedExtractor,
)
from doc_extractor.api.config import (
    ApiSettings,
    bridge_to_api_settings,
    bridge_to_engine_settings,
    load_api_settings,
)
from doc_extractor.api.services.document_service import DocumentService
from doc_extractor.api.token_core import (
    DEFAULT_KEY_LIFETIME_DAYS,
    DEFAULT_TOKEN_PREFIX,
    MAX_KEY_LIFETIME_DAYS,
    TOKEN_ENTROPY_MIN_BYTES,
    GeneratedToken,
    create_api_key_principal,
    create_api_key_record,
    extract_token_prefix,
    generate_api_secret,
    hash_api_token,
    validate_api_key_roles,
    validate_api_key_scopes,
    validate_key_expiration,
)

__all__ = [
    "create_app",
    "ApplicationContainer",
    "TenantComponents",
    "TenantComponentCache",
    "ThreadSafeRoutedExtractor",
    "DocumentService",
    "ApiSettings",
    "bridge_to_api_settings",
    "bridge_to_engine_settings",
    "load_api_settings",
    "DEFAULT_KEY_LIFETIME_DAYS",
    "DEFAULT_TOKEN_PREFIX",
    "MAX_KEY_LIFETIME_DAYS",
    "TOKEN_ENTROPY_MIN_BYTES",
    "GeneratedToken",
    "create_api_key_principal",
    "create_api_key_record",
    "extract_token_prefix",
    "generate_api_secret",
    "hash_api_token",
    "validate_api_key_roles",
    "validate_api_key_scopes",
    "validate_key_expiration",
]
