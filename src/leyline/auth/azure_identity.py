"""Tokens from the Azure managed identity of the container the app runs in.

Container Apps (like App Service) gives each container two environment variables,
``IDENTITY_ENDPOINT`` and ``IDENTITY_HEADER``, and answers a local HTTP request with
an access token for the container's identity. Using it needs no secret and no extra
dependency, and nothing here works outside Azure: locally the services keep their
keys (``docs/DEPLOYMENT.md``).

Tokens are cached until shortly before they expire. Each new database or Redis
connection, and each Blob request, asks for one, so a long-running container never
holds an expired credential and never stores one.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests

API_VERSION = "2019-08-01"
# Refresh this long before the token expires.
_EXPIRY_MARGIN_SECONDS = 300

# The resource each Azure service asks for tokens for.
POSTGRES = "https://ossrdbms-aad.database.windows.net"
STORAGE = "https://storage.azure.com"
OPENAI = "https://cognitiveservices.azure.com"
REDIS = "https://redis.azure.com"


class IdentityError(RuntimeError):
    """No token could be obtained (not running in Azure, or the identity is not
    allowed). Callers fail closed: the dependent service is simply unavailable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_on: int  # seconds since the epoch


class ManagedIdentity:
    def __init__(
        self,
        client_id: str | None = None,
        *,
        endpoint: str | None = None,
        header: str | None = None,
        timeout_seconds: float = 5.0,
        session: requests.Session | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client_id = client_id
        self._endpoint = (
            endpoint if endpoint is not None else os.environ.get("IDENTITY_ENDPOINT")
        )
        self._header = (
            header if header is not None else os.environ.get("IDENTITY_HEADER")
        )
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._now = now
        self._cache: dict[str, AccessToken] = {}
        self._lock = threading.Lock()

    def token(self, resource: str) -> AccessToken:
        """A token for ``resource``, from the cache while it is still fresh."""
        with self._lock:
            cached = self._cache.get(resource)
            if cached and cached.expires_on - self._now() > _EXPIRY_MARGIN_SECONDS:
                return cached
            fresh = self._fetch(resource)
            self._cache[resource] = fresh
            return fresh

    def get_token(self, *scopes: str, **_: object) -> AccessToken:
        """The ``TokenCredential`` shape the Azure SDKs expect (``scope`` is the
        resource plus ``/.default``)."""
        if not scopes:
            raise IdentityError("no_scope")
        resource = scopes[0].removesuffix("/.default").rstrip("/")
        return self.token(resource)

    def _fetch(self, resource: str) -> AccessToken:
        if not self._endpoint or not self._header:
            raise IdentityError("not_in_azure")
        params = {"api-version": API_VERSION, "resource": resource}
        if self._client_id:
            params["client_id"] = self._client_id
        try:
            response = self._session.get(
                self._endpoint,
                params=params,
                headers={"X-IDENTITY-HEADER": self._header},
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
            return AccessToken(str(data["access_token"]), int(data["expires_on"]))
        except requests.ConnectionError as exc:
            raise IdentityError("connection_error") from exc
        except requests.Timeout as exc:
            raise IdentityError("timeout") from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise IdentityError(f"http_{status}") from exc
        except (ValueError, KeyError, TypeError) as exc:
            raise IdentityError("bad_response") from exc
