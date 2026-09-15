"""Environment detection and Azure credential resolution.

Reusable across triggers, orchestrators and activities. Local runs are detected
via the ``AzureWebJobsStorage`` value that the Functions host sets from
``local.settings.json`` (Azurite -> ``UseDevelopmentStorage=true``).
"""
import logging
import os
import threading
import time

from azure.core.credentials import AccessToken, TokenCredential
from azure.identity import (
    AzureCliCredential,
    ClientSecretCredential,
    ManagedIdentityCredential,
)

# Local auth is AzureCliCredential by default; set to 'ClientSecretCredential' to use an SP.
LOCAL_AUTH_TYPE = os.getenv("LOCAL_AUTH_TYPE", "AzureCliCredential")
LOCAL_TENANT_ID = os.getenv("LOCAL_TENANT_ID")
LOCAL_CLIENT_ID = os.getenv("LOCAL_CLIENT_ID")
LOCAL_CLIENT_SECRET = os.getenv("LOCAL_CLIENT_SECRET")

# User-assigned managed identity client id used when running in Azure.
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID")

_TOKEN_REFRESH_SKEW_SECONDS = 300
_credential_lock = threading.Lock()
_credential: TokenCredential | None = None


class _CachedTokenCredential(TokenCredential):
    """Share immutable access tokens and single-flight refreshes per request key."""

    def __init__(self, inner: TokenCredential) -> None:
        self._inner = inner
        self._tokens: dict[tuple, AccessToken] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(scopes: tuple[str, ...], kwargs: dict) -> tuple:
        return (
            tuple(scopes),
            kwargs.get("tenant_id"),
            kwargs.get("claims"),
            bool(kwargs.get("enable_cae", False)),
        )

    @staticmethod
    def _valid(token: AccessToken | None) -> bool:
        return token is not None and token.expires_on > time.time() + _TOKEN_REFRESH_SKEW_SECONDS

    def get_token(self, *scopes: str, **kwargs) -> AccessToken:
        key = self._key(scopes, kwargs)
        token = self._tokens.get(key)
        if self._valid(token):
            return token
        with self._lock:
            refresh_lock = self._locks.setdefault(key, threading.Lock())
        with refresh_lock:
            token = self._tokens.get(key)
            if self._valid(token):
                return token
            token = self._inner.get_token(*scopes, **kwargs)
            self._tokens[key] = token
            return token

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if close:
            close()


def is_running_locally() -> bool:
    """True when the host points at Azurite (local development storage)."""
    return os.getenv("AzureWebJobsStorage") == "UseDevelopmentStorage=true"


def _create_credential() -> TokenCredential:
    if is_running_locally():
        if LOCAL_AUTH_TYPE == "ClientSecretCredential":
            logging.info("Using ClientSecretCredential for local authentication")
            return ClientSecretCredential(
                tenant_id=LOCAL_TENANT_ID,
                client_id=LOCAL_CLIENT_ID,
                client_secret=LOCAL_CLIENT_SECRET,
            )
        logging.info("Using AzureCliCredential for local authentication")
        return AzureCliCredential(process_timeout=60)

    logging.info("Using ManagedIdentityCredential for authentication in Azure")
    return ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)


def get_credential() -> TokenCredential:
    """Return one process-wide credential with per-scope token caching."""
    global _credential
    if _credential is None:
        with _credential_lock:
            if _credential is None:
                _credential = _CachedTokenCredential(_create_credential())
    return _credential
