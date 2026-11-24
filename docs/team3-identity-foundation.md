# Internal Identity, Authentication & Federation Foundation (Team Member 3, Phase 5A)

Status: **Phase 5A delivered (Internal Identity & Authentication Foundation).**
Provides a product-owned identity abstraction, RS256 JWT token lifecycle, rotating single-use refresh tokens with replay detection, Multi-Factor Authentication (MFA) enforcement for privileged roles, customer OIDC SSO federation with strict tenant isolation, and full preservation of existing API-key authentication.

---

## 1. Security Boundaries & Constraints

- **PRD Security Requirements Compliance:**
  - **SEC-001 (MFA for Admin and Schema Approver):** Fully implemented. Privileged roles (`admin`, `schema_approver`) are strictly gated by verified MFA. Tokens are only issued or accepted when MFA is verified via trusted authentication mechanisms (RFC 6238 TOTP or verified upstream IdP `amr`/`acr` claims). Self-asserted caller claims (e.g. caller asserting `amr: ["mfa"]`) are rejected.
  - **SEC-002 (Token-Based Authentication & Refresh Lifecycle):** Fully implemented. Access tokens are RS256-only with maximum lifetime of 3600 seconds (1 hour). Refresh tokens are rotating, single-use credentials with cryptographically secure 256-bit entropy; presenting an already-consumed refresh token immediately triggers replay attack defense, invalidating the entire token family.
  - **SEC-003 (Seven Least-Privilege Canonical Roles):** Fully enforced. Roles are validated against `CANONICAL_ROLES` (`admin`, `schema_approver`, `schema_author`, `analyst`, `reviewer`, `auditor`, `integrator`). Explicit API scopes can only narrow, never widen, role-derived scopes.
  - **SEC-006 (Strict Tenant Isolation):** Every user, session, refresh token, and external OIDC identity provider is strictly bound to an authoritative tenant. Cross-tenant authentication or assertion is refused with `CrossTenantIdentityError`.
  - **SEC-012 & API-011 (Sanitized Errors & Anti-Leakage):** Raw secrets, passwords, token hashes, and private keys are never echoed in error messages, exception details, or log streams.
  - **SEC-021 (Security Monitoring & Alerting):** Repeated failed login attempts are recorded by credential fingerprint without logging plaintext credentials.

---

## 2. Architecture & Subsystems

The identity subsystem resides in `src/doc_extractor/infrastructure/identity/`:

| Module | Purpose |
| --- | --- |
| `models.py` | Data models: `UserRecord`, `RefreshTokenRecord`, `TokenPair`, `OidcConfiguration`, and salted PBKDF2 password hashing (`hash_password`, `verify_password`). |
| `errors.py` | Typed sanitized exceptions (`IdentityError`, `InvalidCredentialsError`, `TokenReplayError`, `TokenRevokedError`, `MfaRequiredError`, `MfaVerificationFailedError`, `OidcValidationError`, `CrossTenantIdentityError`, `IdentityConfigurationError`). |
| `mfa.py` | `TotpEngine` (RFC 6238 TOTP verification with time-drift tolerance) and `MfaEnforcer` (evaluates privileged roles and verifies upstream IdP `amr`/`acr` claims). |
| `tokens.py` | `KeyRing` (RSA signing key rotation and RFC 7517 JWKS export), `TokenRevocationStore` (JTI blacklisting and user session revocation), `RefreshTokenRepository` (family grouping and replay invalidation), and `TokenManager`. |
| `oidc.py` | Customer OIDC SSO federation (`OidcClient`, `OidcRegistry`): state/nonce CSRF protection, ID token validation, role mapping, and strict per-tenant binding. |
| `service.py` | `InternalIdentityService`: Central coordinator for user provisioning, password authentication, OIDC SSO, token rotation, session revocation, and `AuthenticationProvider` bridge. |
| `__init__.py` | Clean package exports. |

---

## 3. JWT and Refresh Token Lifecycle (SEC-002)

### Access Token Management
- **Algorithm:** RS256-only (`ALGORITHM = "RS256"`). Symmetric HS256 algorithm confusion and `none` algorithm are strictly refused.
- **Maximum Lifetime:** 3600 seconds (1 hour). Tokens exceeding this duration are refused at issue and verify.
- **Header Identification (`kid`):** Access tokens embed a `kid` header identifying the active signing key version.
- **Key Rotation:** The `KeyRing` manages the active private key for signing and an archive of historical public keys for verification. Rotating from `v1` to `v2` allows existing unexpired `v1` tokens to verify while newly minted tokens use `v2`.
- **Public Key Distribution:** `KeyRing.to_jwks()` exports all public verification keys as an RFC 7517 JWKS dictionary for microservices, API gateways, and external verifiers.
- **Token Revocation:** `TokenRevocationStore` maintains a revocation list by token ID (`jti`) and supports invalidating all active sessions for a user upon sign-out or compromised credentials.

### Rotating Single-Use Refresh Tokens
- **One-Time Use:** Refresh tokens are single-use credentials. Upon successful exchange, the current refresh token is marked as `used = True` and a replacement token is issued.
- **Replay Detection & Family Invalidation:**
  - Each refresh token chain belongs to a `family_id`.
  - If a client or attacker presents an already-consumed refresh token (`used == True`), the identity service detects a token replay attack.
  - The service immediately invalidates the **entire token family** (`revoke_family`), revokes the user's active session, and logs a security alert.

```
Initial Issue:   Token RT-1 (Family F-1, used=False)
Rotation 1:      RT-1 consumed -> RT-1 (used=True), RT-2 issued (Family F-1, used=False)
Rotation 2:      RT-2 consumed -> RT-2 (used=True), RT-3 issued (Family F-1, used=False)
Replay Attack:   Attacker presents RT-1 (already used)
                 ==> REPLAY DETECTED!
                 ==> Family F-1 completely revoked: RT-1, RT-2, and RT-3 invalidated immediately.
```

---

## 4. Multi-Factor Authentication (MFA) Enforcement (SEC-001)

- **Gated Roles:** `admin` and `schema_approver` (SEC-001).
- **Trusted Verification Mechanisms:**
  1. **RFC 6238 TOTP (Local Verification):**
     - Cryptographically secure Base32 secret generation.
     - 6-digit TOTP codes calculated via HMAC-SHA1 over 30-second time steps.
     - Constant-time verification with configurable drift window (default ±1 step = ±30s).
  2. **Upstream OIDC Assertions (Federated Verification):**
     - Verified ID tokens from customer IdPs are inspected for trusted `amr` (e.g. `mfa`, `otp`, `hwk`, `fido`) or `acr` claims.
- **Anti-Spoofing:** Self-asserted caller claims in request headers or payloads are never trusted. Access tokens carrying privileged roles are minted only when the identity service confirms trusted MFA execution, embedding `"amr": ["pwd", "mfa"]` and setting `mfa_verified=True`.

---

## 5. Customer OIDC Federation & SSO

- **Tenant-Bound IdP Association:** Each `OidcConfiguration` is strictly associated with a single `tenant_id`. An IdP configured for Tenant A cannot authenticate or issue identities for Tenant B.
- **CSRF & Replay Defense:**
  - Cryptographic `state` validated on callback to prevent CSRF attacks.
  - Cryptographic `nonce` embedded in authorization requests and verified against ID token claims to prevent token replay.
- **Signature & Claim Validation:** ID tokens are verified against the customer IdP's JWKS using RS256, validating issuer, audience (`client_id`), and expiration.
- **Role Mapping:** External identity claims are mapped into the seven canonical roles (`CANONICAL_ROLES`) or fall back to tenant-configured default roles under least privilege.
- **SAML 2.0 Web SSO Roadmap:** SAML 2.0 is an enterprise requirement for environments relying on legacy corporate identity providers (ADFS, PingFederate). In Phase 5A, SAML is documented as an enterprise bridge requirement mapping into the OIDC provider abstraction.

---

## 6. Preservation of Existing API-Key Authentication

The existing API-key infrastructure is completely preserved without breaking changes:
- `ApiKeyAuthenticationProvider` validates scoped API keys via deterministic SHA-256 hash lookup.
- Strict tenant boundary isolation and scope filtering remain intact.
- Machine credentials always have `mfa_verified = False` (SEC-001 invariant).
- Last-used timestamp tracking and safe error logging are fully retained.

---

## 7. Open Decisions Register

The following related decisions remain **Open**:
- **M5-D08 (Replay of a shown-once API-key secret):** Remains Open (Owned by PRD Owner - Security).
- **M5-D16 (Tenant-ID policy):** Remains Open (Owned by Team Member 3).
- **M5-D17 (Tenant provisioning):** Remains Open (Owned by Team Member 3).
