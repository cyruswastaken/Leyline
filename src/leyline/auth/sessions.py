"""Server-side sessions stored in Redis.

The browser cookie carries only an unguessable random identifier; the data
(including the principal) never leaves the server and cannot be forged or
edited by the client. Sessions expire after an idle period and after an
absolute lifetime, and the identifier is rotated on login to prevent
session fixation.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Iterator
from typing import Any, cast

from flask import Flask, Request, Response, session
from flask.sessions import SessionInterface, SessionMixin
from redis import Redis
from redis.exceptions import RedisError
from werkzeug.datastructures import CallbackDict

_SID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
_KEY_PREFIX = "leyline:session:"


class ServerSession(CallbackDict[str, Any], SessionMixin):
    def __init__(
        self,
        initial: dict[str, Any] | None = None,
        *,
        sid: str | None = None,
        created_at: float | None = None,
    ) -> None:
        def on_update(_: CallbackDict[str, Any]) -> None:
            self.modified = True
            self.accessed = True

        super().__init__(initial or {}, on_update)
        self.sid = sid
        self.created_at = created_at if created_at is not None else time.time()
        self.rotate = False
        self.modified = False
        self.accessed = False
        # True when the session store could not be reached: the request
        # must then be refused, never treated as anonymous-but-fine.
        self.store_unavailable = False

    def __getitem__(self, key: str) -> Any:
        self.accessed = True
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        self.accessed = True
        return super().get(key, default)

    def __contains__(self, key: object) -> bool:
        self.accessed = True
        return super().__contains__(key)

    def __iter__(self) -> Iterator[str]:
        self.accessed = True
        return super().__iter__()

    def rotate_id(self) -> None:
        """Issue a fresh session id on the next save (call at login)."""
        self.rotate = True
        self.modified = True


class RedisSessionInterface(SessionInterface):
    def __init__(
        self,
        client: Redis,
        *,
        idle_seconds: int,
        absolute_seconds: int,
        secure: bool,
        stateless_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.client = client
        # Paths (health probes) that never read or write a session, so they
        # keep working while the session store is down.
        self.stateless_paths = stateless_paths
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.secure = secure
        # The __Host- prefix makes browsers reject the cookie unless it is
        # Secure, host-only and path "/", which blocks cookie injection from
        # sibling subdomains.
        self.cookie_name = "__Host-leyline_sid" if secure else "leyline_sid"

    # -- loading ---------------------------------------------------------

    def open_session(self, app: Flask, request: Request) -> ServerSession:
        if request.path in self.stateless_paths:
            return ServerSession()
        sid = request.cookies.get(self.cookie_name)
        if sid and _SID_PATTERN.fullmatch(sid):
            try:
                loaded = self._load(sid)
            except RedisError:
                unavailable = ServerSession()
                unavailable.store_unavailable = True
                return unavailable
            if loaded is not None:
                return loaded
        return ServerSession()

    def _load(self, sid: str) -> ServerSession | None:
        raw = self.client.get(_KEY_PREFIX + sid)
        if raw is None:
            return None
        try:
            stored = json.loads(raw)
            data = stored["data"]
            created_at = float(stored["created_at"])
        except (ValueError, KeyError, TypeError):
            self.client.delete(_KEY_PREFIX + sid)
            return None
        if not isinstance(data, dict):
            self.client.delete(_KEY_PREFIX + sid)
            return None
        if time.time() - created_at >= self.absolute_seconds:
            self.client.delete(_KEY_PREFIX + sid)
            return None
        return ServerSession(data, sid=sid, created_at=created_at)

    # -- saving ----------------------------------------------------------

    def save_session(
        self, app: Flask, session: SessionMixin, response: Response
    ) -> None:
        if not isinstance(session, ServerSession) or session.store_unavailable:
            return

        if session.rotate and session.sid:
            self.client.delete(_KEY_PREFIX + session.sid)
            session.sid = None
            session.created_at = time.time()

        if not session:
            # Cleared (logout) or never used: remove any stored session.
            if session.sid:
                self.client.delete(_KEY_PREFIX + session.sid)
                response.delete_cookie(
                    self.cookie_name,
                    path="/",
                    secure=self.secure,
                    httponly=True,
                    samesite="Lax",
                )
            return

        if session.accessed:
            response.vary.add("Cookie")

        if session.sid is None:
            session.sid = secrets.token_urlsafe(32)
            session.modified = True

        key = _KEY_PREFIX + session.sid
        remaining = int(self.absolute_seconds - (time.time() - session.created_at))
        ttl = max(1, min(self.idle_seconds, remaining))

        if session.modified:
            payload = json.dumps(
                {"created_at": session.created_at, "data": dict(session)}
            )
            self.client.set(key, payload, ex=ttl)
        else:
            # Sliding idle timeout: activity extends the session, never
            # beyond the absolute lifetime.
            self.client.expire(key, ttl)

        response.set_cookie(
            self.cookie_name,
            session.sid,
            max_age=None,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="Lax",
        )


def rotate_session_id() -> None:
    """Ask the session interface to issue a new id when saving (call at login)."""
    current = cast("ServerSession", session)
    if isinstance(current, ServerSession):
        current.rotate_id()
