"""Explicit Azure identity provenance and purpose for outbound Azure calls.

Two provenances, which differ only when running in Azure:

* **function** — the app's own identity: Managed Identity in Azure, Azure CLI locally.
* **user**     — the signed-in user: the Easy Auth forwarded ARM token in Azure,
  Azure CLI locally (so local dev exercises the same code path as your account).

Two purpose-named switches are what callers should use, so intent is explicit and
greppable at the call site:

* :func:`for_backend_operation` — asynchronous/background work (collectors, timers).
* :func:`for_user_action`       — anything done on behalf of the connected user.

Every identity exposes both shapes the codebase consumes: a ``TokenCredential``
for SDK clients and a raw bearer token for direct REST clients.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

from azure.core.credentials import AccessToken, TokenCredential

from .credentials import get_credential, is_running_locally

ARM_SCOPE = "https://management.azure.com/.default"

# Header Easy Auth injects (token store enabled) carrying the user's access token.
_EASY_AUTH_TOKEN_HEADER = "X-MS-TOKEN-AAD-ACCESS-TOKEN"


class UserNotAuthenticated(Exception):
    """No user token on the request (Azure, not signed in). Map to HTTP 401."""


def decode_jwt_claims(token: str) -> dict:
    """Best-effort decode of a JWT payload segment (no signature validation)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64 padding
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:  # pragma: no cover - opaque/short tokens
        return {}


def _scope_audience(scope: str) -> str:
    """Return the audience host of a scope, e.g. 'management.azure.com'."""
    return scope.split("://", 1)[-1].split("/", 1)[0].lower()


class _StaticTokenCredential(TokenCredential):
    """Wrap a forwarded bearer token so it can back a ``TokenCredential`` client.

    The forwarded Easy Auth token has a FIXED audience, so requesting any other
    scope must fail loudly — otherwise an ARM token could be sent to a different
    service (e.g. Storage/Key Vault). Expiry is read from the token's ``exp`` claim.
    """

    def __init__(self, token: str, audience_scope: str) -> None:
        self._token = token
        self._audience = _scope_audience(audience_scope)
        exp = decode_jwt_claims(token).get("exp")
        self._expires_on = int(exp) if isinstance(exp, (int, float)) else int(time.time()) + 300

    def get_token(self, *scopes: str, **_kwargs) -> AccessToken:
        for scope in scopes:
            if _scope_audience(scope) != self._audience:
                raise ValueError(
                    f"User identity token is scoped to '{self._audience}'; "
                    f"cannot mint a token for '{_scope_audience(scope)}'."
                )
        return AccessToken(self._token, self._expires_on)


@dataclass(frozen=True)
class AzureIdentity:
    """An Azure identity plus its provenance, usable as credential or bearer token."""

    kind: str          # "function" | "user"
    source: str        # "managed_identity" | "azure_cli" | "easy_auth"
    _credential: TokenCredential

    def credential(self) -> TokenCredential:
        """The ``TokenCredential`` for SDK clients."""
        return self._credential

    def get_token(self, scope: str = ARM_SCOPE) -> str:
        """A raw bearer token for direct REST / ``run_graph_query``."""
        return self._credential.get_token(scope).token


@dataclass(frozen=True)
class UserAuditMetadata:
    """Stable and display-friendly identity fields for user-authored changes."""

    tenant_id: str
    object_id: str
    display_name: str


def user_audit_metadata(who: AzureIdentity, token: str | None = None) -> UserAuditMetadata:
    """Extract audit metadata from a user identity's trusted ARM token."""
    if who.kind != "user":
        raise ValueError("Audit metadata requires a user identity.")
    claims = decode_jwt_claims(token or who.get_token())
    tenant_id = str(claims.get("tid") or "").strip().lower()
    object_id = str(claims.get("oid") or claims.get("sub") or "").strip().lower()
    if not tenant_id or not object_id:
        raise ValueError("User token is missing required tid and oid/sub claims.")
    display_name = str(
        claims.get("name")
        or claims.get("upn")
        or claims.get("preferred_username")
        or claims.get("unique_name")
        or object_id
    ).strip()
    return UserAuditMetadata(tenant_id, object_id, display_name)


# ── Provenance (concrete source) ──────────────────────────────────────────────
def function_identity() -> AzureIdentity:
    """The app's own identity: Managed Identity in Azure, Azure CLI locally."""
    source = "azure_cli" if is_running_locally() else "managed_identity"
    return AzureIdentity("function", source, get_credential())


def user_identity(req) -> AzureIdentity:
    """The signed-in user: Easy Auth token in Azure, Azure CLI locally.

    Raises :class:`UserNotAuthenticated` when running in Azure with no forwarded
    token — never falls back to the Managed Identity (which would over-expose).
    """
    if is_running_locally():
        return AzureIdentity("user", "azure_cli", get_credential())
    token = req.headers.get(_EASY_AUTH_TOKEN_HEADER)
    if not token:
        raise UserNotAuthenticated()
    return AzureIdentity("user", "easy_auth", _StaticTokenCredential(token, ARM_SCOPE))


# ── Purpose (semantic switch — the caller-facing API) ─────────────────────────
def for_backend_operation() -> AzureIdentity:
    """Identity for asynchronous/background work (collectors, timers)."""
    return function_identity()


def for_user_action(req) -> AzureIdentity:
    """Identity for actions performed on behalf of the connected user."""
    return user_identity(req)
