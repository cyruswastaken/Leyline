"""Resolve a user's groups through Microsoft Graph when the token has too many.

Entra puts at most 200 groups in an ID token. A user in more gets an "overage" marker
instead (``_claim_names``) and the app must ask Graph. Leyline treats that marker as
"groups unknown" and grants nothing beyond ``*`` documents; with
``OIDC_RESOLVE_GROUP_OVERAGE`` it asks Graph for the user's groups instead.

Safety properties, each tested:

* It calls only the configured Graph address (never a URL taken from the token).
* It sends the user's own delegated access token, which is never stored or logged.
* Anything that goes wrong raises ``GraphError``; the caller then keeps the
  fail-closed result, so a Graph outage or a missing permission can never grant access.
* Group object IDs come back, but only IDs mapped in ``OIDC_GROUP_NAMES`` count, exactly
  as for groups in the token.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import requests

logger = logging.getLogger(__name__)

# More than any realistic directory; stops a runaway paging loop.
_MAX_PAGES = 20


class GraphError(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def fetch_group_ids(
    access_token: str,
    *,
    base_url: str = "https://graph.microsoft.com/v1.0",
    timeout_seconds: float = 5.0,
    session: requests.Session | None = None,
) -> list[str]:
    """Object IDs of every security group the signed-in user belongs to, nested
    groups included (``/me/getMemberObjects``)."""
    if not access_token:
        raise GraphError("no_access_token")
    http = session or requests.Session()
    url: str | None = base_url.rstrip("/") + "/me/getMemberObjects"
    body = {"securityEnabledOnly": True}
    ids: list[str] = []
    for _ in range(_MAX_PAGES):
        if url is None:
            return _unique(ids)
        try:
            response = http.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except requests.ConnectionError as exc:
            # Could not reach Graph (this includes a connect timeout).
            raise _failed("connection_error", exc) from exc
        except requests.Timeout as exc:
            raise _failed("timeout", exc) from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            raise _failed(f"http_{status}", exc) from exc
        except ValueError as exc:
            raise _failed("bad_response", exc) from exc

        values = data.get("value") if isinstance(data, dict) else None
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise GraphError("bad_response")
        ids.extend(values)
        # Paging follows the next link only if it stays on the configured host.
        next_link = data.get("@odata.nextLink")
        if isinstance(next_link, str) and next_link.startswith(base_url.rstrip("/")):
            url, body = next_link, {"securityEnabledOnly": True}
        else:
            url = None
    raise GraphError("too_many_pages")


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _failed(reason: str, exc: Exception) -> GraphError:
    # The reply can echo request details; log the class and reason only.
    logger.warning("graph group lookup failed: %s (%s)", reason, type(exc).__name__)
    return GraphError(reason)
