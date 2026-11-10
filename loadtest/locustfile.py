"""Load test for Leyline: real sign-in, then a mix of chat, browsing and probes.

    python loadtest/fake_llm.py &
    docker compose -f compose.yaml -f loadtest/compose.loadtest.yaml up -d app
    locust -f loadtest/locustfile.py --headless -u 8 -r 2 -t 90s \\
        --host http://localhost:8000 --csv loadtest/results/run

Each virtual user signs in through Keycloak as one of the demo users, then asks
questions over the streaming endpoint. Besides Locust's own timings it records
two custom measurements: ``first_token`` (question to first text) and
``turn_total`` (question to the end of the stream). The run fails (exit code 1)
when a target in ``TARGETS`` is missed. Targets and results: docs/LOAD_TEST.md.
"""

from __future__ import annotations

import html
import itertools
import json
import re
import socket
import time

import urllib3.util.connection
from locust import HttpUser, between, events, task
from locust.env import Environment

# On Windows "localhost" resolves to IPv6 first and Docker publishes IPv4 only, so
# every new connection would wait ~2 s for the fallback and swamp the numbers.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET

PASSWORD = "Leyline-dev-1!"  # noqa: S105  # throwaway local-dev password (realm file)
USERS = ["alice.employee", "sam.security", "hank.hr", "fiona.finance"]
QUESTIONS = [
    "How do I connect to the corporate VPN?",
    "I lost my laptop, what do I do?",
    "How do I reset my password?",
    "Someone stole my backpack with my work laptop inside",
    "How many days of annual leave do I get?",
    "What are the rules for expenses on a business trip?",
    "How do I get a guest into the building?",
    "How do I connect to the office Wi-Fi?",
]
_next_user = itertools.cycle(USERS)

# 95th percentile limits in milliseconds, by request name.
TARGETS = {
    "first_token": 2500,
    "turn_total": 6000,
    "GET /api/conversations": 400,
    "GET /me": 300,
}
MAX_FAILURE_RATIO = 0.01


class Leyline(HttpUser):
    wait_time = between(1, 3)

    def on_start(self) -> None:
        self.username = next(_next_user)
        self._sign_in()
        self.conversation_id: str | None = None
        self.turns = 0

    def _sign_in(self) -> None:
        page = self.client.get(
            "/login?next=/", headers={"Accept": "text/html"}, name="sign-in"
        )
        for cookie in self.client.cookies:
            cookie.secure = False  # requests treats http://localhost as insecure
        form = re.search(r'<form[^>]*action="([^"]+)"', page.text)
        if not form:
            raise RuntimeError("no Keycloak login form")
        self.client.post(
            html.unescape(form.group(1)),
            data={"username": self.username, "password": PASSWORD},
            name="sign-in",
        )
        self.csrf = self.client.get("/me", name="GET /me").json()["csrf_token"]

    def _record(
        self, name: str, started: float, exception: Exception | None = None
    ) -> None:
        self.environment.events.request.fire(
            request_type="SSE",
            name=name,
            response_time=(time.perf_counter() - started) * 1000,
            response_length=0,
            exception=exception,
            context={},
        )

    @task(8)
    def ask(self) -> None:
        # Start a fresh conversation every few turns, like a real user would.
        if self.turns and self.turns % 3 == 0:
            self.conversation_id = None
        question = QUESTIONS[(self.turns + len(self.username)) % len(QUESTIONS)]
        self.turns += 1
        started = time.perf_counter()
        first_token_at: float | None = None
        outcome = "no done event"
        with self.client.post(
            "/api/chat/stream",
            json={"message": question, "conversation_id": self.conversation_id},
            headers={"X-CSRF-Token": self.csrf},
            stream=True,
            name="POST /api/chat/stream (headers)",
            catch_response=True,
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
                return
            event = ""
            for raw in response.iter_lines(decode_unicode=True):
                if raw.startswith("event: "):
                    event = raw[7:]
                elif raw.startswith("data: "):
                    data = json.loads(raw[6:])
                    if event == "meta":
                        self.conversation_id = data["conversation_id"]
                    elif event == "delta" and first_token_at is None:
                        first_token_at = time.perf_counter()
                        self._record("first_token", started)
                    elif event == "done":
                        outcome = "ok"
                    elif event == "error":
                        outcome = f"error event: {data.get('reason')}"
            if outcome == "ok":
                response.success()
            else:
                response.failure(outcome)
        self._record(
            "turn_total", started, None if outcome == "ok" else RuntimeError(outcome)
        )

    @task(3)
    def browse(self) -> None:
        self.client.get("/api/conversations", name="GET /api/conversations")
        self.client.get("/api/tickets", name="GET /api/tickets")

    @task(1)
    def profile(self) -> None:
        self.client.get("/me", name="GET /me")

    @task(1)
    def probe(self) -> None:
        self.client.get("/healthz", name="GET /healthz")


@events.quitting.add_listener
def check_targets(environment: Environment, **_: object) -> None:
    stats = environment.stats
    problems = []
    for name, limit in TARGETS.items():
        entry = next((e for n, e in stats.entries.items() if n[0] == name), None)
        if entry is None or entry.num_requests == 0:
            problems.append(f"{name}: no samples")
            continue
        p95 = entry.get_response_time_percentile(0.95)
        if p95 > limit:
            problems.append(f"{name}: p95 {p95:.0f} ms > {limit} ms")
    total = stats.total
    if total.num_requests:
        ratio = total.num_failures / total.num_requests
        if ratio > MAX_FAILURE_RATIO:
            problems.append(f"failure ratio {ratio:.2%} > {MAX_FAILURE_RATIO:.0%}")
    if problems:
        print("LOAD TEST TARGETS MISSED:\n  " + "\n  ".join(problems))
        environment.process_exit_code = 1
    else:
        print("LOAD TEST TARGETS MET")
        environment.process_exit_code = 0
