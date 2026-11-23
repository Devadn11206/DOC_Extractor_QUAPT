"""
Routes a bearer credential to the right authentication provider (PRD SEC-001, SEC-002).

- A three-part dotted token is a JWT: users and internal services (RS256, SEC-002).
- Anything else is an API key: integrators (SEC-002, scoped keys).

Both providers fail closed. A credential that matches neither shape is refused, and there is no anonymous fallback.
"""

from __future__ import annotations

from typing import Any, Optional

from doc_extractor.api.exceptions import AuthenticationRequiredError
from doc_extractor.api.ports.authentication import ApiPrincipal, AuthenticationProvider
from doc_extractor.api.providers.jwt_auth import _bearer_token


class CompositeAuthenticationProvider(AuthenticationProvider):
    def __init__(
        self,
        *,
        jwt_provider: Optional[AuthenticationProvider] = None,
        api_key_provider: Optional[AuthenticationProvider] = None,
    ) -> None:
        if jwt_provider is None and api_key_provider is None:
            raise ValueError("At least one authentication provider is required.")
        self._jwt = jwt_provider
        self._api_key = api_key_provider

    async def authenticate(self, credentials: Any, **kwargs: Any) -> Optional[ApiPrincipal]:
        token = _bearer_token(credentials)
        if token is None:
            raise AuthenticationRequiredError(
                message="Authorization header or token credential is required.",
                code="AUTHENTICATION_REQUIRED",
            )
        try:
            if token.count(".") == 2 and self._jwt is not None:
                return await self._jwt.authenticate(credentials, **kwargs)
            if self._api_key is not None:
                return await self._api_key.authenticate(credentials, **kwargs)
            raise AuthenticationRequiredError(
                message="This credential type is not accepted here.",
                code="AUTHENTICATION_REQUIRED",
            )
        except AuthenticationRequiredError:
            # SEC-021: repeated failures are alerted by credential fingerprint. The token itself is never recorded.
            from doc_extractor.alerts import credential_fingerprint, get_default_monitor

            get_default_monitor().record_failed_login(credential_fingerprint(token))
            raise
