"""Smoke test of a deployed Leyline, signed out: is it up, and is it closed?

    python scripts/smoke_test.py https://<site>

Used by the deploy workflow after an update; if it fails the workflow puts the previous
image back. It needs no account and changes nothing: only GET requests, the signed-out view.
It checks that the site is ready, that the public pages work, and that the protected
ones refuse a stranger (a deploy that broke sign-in protection must not stay live).
Exit status 0 when every check passed, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

TIMEOUT = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # return the redirect itself instead of following it


_opener = urllib.request.build_opener(_NoRedirect)


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: str


def get(url: str) -> Reply:
    request = urllib.request.Request(url, headers={"Accept": "text/html,*/*"})  # noqa: S310
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:
            return Reply(
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                response.read(200_000).decode("utf-8", "replace"),
            )
    except urllib.error.HTTPError as error:  # 3xx/4xx/5xx are answers, not failures
        return Reply(
            error.code,
            {k.lower(): v for k, v in error.headers.items()},
            error.read(200_000).decode("utf-8", "replace"),
        )


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_checks(base: str) -> list[Check]:
    base = base.rstrip("/")
    checks: list[Check] = []

    def check(name: str, test: Callable[[], str | None]) -> None:
        try:
            problem = test()
        except Exception as exc:  # a failed request is a failed check
            problem = f"{type(exc).__name__}: {exc}"
        checks.append(Check(name, problem is None, problem or ""))

    def alive() -> str | None:
        reply = get(base + "/healthz")
        ok = reply.status == 200 and json.loads(reply.body).get("status") == "ok"
        return None if ok else f"HTTP {reply.status}"

    def ready() -> str | None:
        reply = get(base + "/readyz")
        try:
            status = json.loads(reply.body).get("status")
        except ValueError:
            status = None
        return (
            None
            if reply.status == 200 and status == "ready"
            else (f"HTTP {reply.status}, status {status!r}")
        )

    def root_goes_to_welcome() -> str | None:
        reply = get(base + "/")
        location = reply.headers.get("location", "")
        ok = (
            reply.status in (301, 302, 303, 307)
            and urlsplit(location).path == "/welcome"
        )
        return None if ok else f"HTTP {reply.status}, location {location!r}"

    def welcome_page() -> str | None:
        reply = get(base + "/welcome")
        return (
            None
            if reply.status == 200 and "Leyline" in reply.body
            else (f"HTTP {reply.status}")
        )

    def strict_csp() -> str | None:
        policy = get(base + "/welcome").headers.get("content-security-policy", "")
        if not policy:
            return "no Content-Security-Policy header"
        return "the policy allows unsafe-inline" if "unsafe-inline" in policy else None

    def closed(path: str) -> Callable[[], str | None]:
        def test() -> str | None:
            reply = get(base + path)
            leaked = reply.status == 200
            return f"a signed-out visitor got HTTP {reply.status}" if leaked else None

        return test

    check("process is alive (/healthz)", alive)
    check("dependencies are ready (/readyz)", ready)
    check("the front door leads to the welcome page", root_goes_to_welcome)
    check("the public welcome page renders", welcome_page)
    check("the Content-Security-Policy has no unsafe-inline", strict_csp)
    for path in (
        "/me",
        "/api/conversations",
        "/api/tickets",
        "/api/admin/documents",
        "/monitoring/summary",
        "/monitoring/events",
    ):
        check(f"{path} refuses a signed-out visitor", closed(path))
    return checks


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].startswith(("https://", "http://")):
        print("usage: smoke_test.py https://<site>", file=sys.stderr)
        return 2
    results = run_checks(args[0])
    for result in results:
        print(
            f"{'ok  ' if result.ok else 'FAIL'}  {result.name}"
            + (f"  ({result.detail})" if result.detail else "")
        )
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
