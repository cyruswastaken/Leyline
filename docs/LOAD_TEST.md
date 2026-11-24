# Load test

A [Locust](https://locust.io) test (`loadtest/locustfile.py`) signs virtual users in through the real
Keycloak flow as demo users, then mixes streamed chat turns, conversation and ticket browsing,
profile reads and health probes. It records Locust's request timings plus two custom measures:
`first_token` (question to first text) and `turn_total` (question to the end of the stream).

## What it measures, and what it does not

- It measures **Leyline**: sessions, permission-aware hybrid search, the database, streaming and
  persistence, on one container (2 gunicorn workers). Embeddings still come from the real local
  Ollama; the chat model is a fake (`loadtest/fake_llm.py`) streaming a canned answer at 30 ms per
  word, so a single laptop GPU does not become the bottleneck being measured.
- It does **not** measure model capacity. A real model's time to first token and answer length
  dominate user-visible latency; measure that separately with the model you deploy.
- Rate limits are raised for the run (`loadtest/compose.loadtest.yaml`); never use that file outside
  a load test. Numbers come from a laptop running the whole stack, so treat them as relative, not
  as production capacity.

## Targets

95th percentile, with at most 1% failed requests (checked automatically; the run exits non-zero if
a target is missed):

| Measure | Target |
| --- | --- |
| `first_token` | under 2500 ms |
| `turn_total` | under 6000 ms |
| `GET /api/conversations` | under 400 ms |
| `GET /me` | under 300 ms |

The first token includes about half a second of deliberate delay: output is held back by 96
characters so a credential cannot be released in two halves (see ARCHITECTURE.md).

## Running it

```bash
python loadtest/fake_llm.py &
docker compose -f compose.yaml -f loadtest/compose.loadtest.yaml up -d app
locust -f loadtest/locustfile.py --headless -u 24 -r 3 -t 150s --reset-stats \
    --host http://localhost:8000 --csv loadtest/results/run
```

`--reset-stats` drops the ramp-up, during which every user signs in. Afterwards restore the normal
configuration with `docker compose up -d app` and stop the fake model.

## Results (2026-11-10)

| Run | Users | Failed | `first_token` p95 | `turn_total` p95 | `GET /api/conversations` p95 | Targets |
| --- | --- | --- | --- | --- | --- | --- |
| 4 threads per worker | 8 | 0 | 620 ms | 1200 ms | 11 ms | met |
| 4 threads per worker | 24 | 2 of 2826 | 1500 ms | 2100 ms | **1000 ms** | **missed** |
| **16 threads per worker (shipped)** | 24 | 0 of 3106 | 620 ms | 1200 ms | **10 ms** | met |

What the runs showed:

1. **Head-of-line blocking.** A streamed answer holds a gunicorn thread for its whole length. With
   2 workers x 4 threads, 24 users kept all 8 threads busy, so cheap requests (even `/healthz`)
   waited behind streams and a few connections failed. The work is I/O-bound, so threads are cheap:
   the image now runs 2 workers x 16 threads (32 concurrent answers per container) with a 15 s
   keep-alive, and the 24-user run meets every target.
2. **A measurement trap worth knowing.** The first runs showed an exact 2.1 s stall on any new
   connection, even `/healthz`. It was the client: on Windows `localhost` tries IPv6 first and
   Docker publishes IPv4 only, so each reconnect paid a fallback delay. The locust file now forces
   IPv4. Without that, no target could be met regardless of the app.
3. **Not yet tested:** more than 24 users, several container instances, a real model's latency, a
   long soak, and the database on separate hardware. The next capacity limit is the per-process
   database pool (10 connections per worker) and Postgres `max_connections`.

## Results with the real model (2026-11-24)

The fake model hides the part users feel most. This run uses the deployed models: chat `gpt-5.4-mini` and
embeddings `text-embedding-3-small`, with the Azure floors (0.37 / 0.60), the sample corpus (49 documents, 138
chunks), the same container image and the same Locust file, with a pause of 10 to 20 seconds between actions
(`LOADTEST_WAIT_MIN`, `LOADTEST_WAIT_MAX`) so the users stay under the model deployment's quota. Everything ran
on one laptop (the app, worker, Postgres, Redis and Keycloak in containers) against the Azure OpenAI account in
Korea Central, on a throwaway database. No request failed in either run; the logs show no rate-limit errors,
no circuit-breaker events and no rewrite fallbacks.

| Run | Chat turns | `first_token` p50 / p95 | `turn_total` p50 / p95 | `GET /api/conversations` p95 | `GET /me` p95 |
| --- | --- | --- | --- | --- | --- |
| 4 users, 4 minutes | 37 | 3.2 s / **4.6 s** | 3.5 s / 4.9 s | 18 ms | 19 ms |
| 8 users, 5 minutes | 87 | 2.8 s / **3.8 s** | 3.1 s / 4.0 s | 67 ms | 19 ms |

The 8-user run split by kind of question:

| | Turns | `first_token` p50 / p95 | `turn_total` p50 / p95 |
| --- | --- | --- | --- |
| New conversation | 32 | 2.3 s / 3.1 s | 2.6 s / 3.3 s |
| Follow-up in an open conversation | 55 | 3.2 s / 4.0 s | 3.5 s / 4.3 s |

**Against the targets:** `turn_total` (under 6 s), `GET /api/conversations` and `GET /me` are met. **`first_token`
(under 2.5 s) is not met with the real model**, in either run and for both kinds of question. The target was set
with the fake model; it is not changed here to fit these numbers (see the open question below).

**Where the time goes.** Timed directly against Azure from the same laptop (12 calls each, a realistic prompt of
about 550 tokens and an answer of about 45): the query embedding takes about 0.4 s (p95 0.5 s) and the chat
model's first token about 1.0 s (p95 1.0 to 1.6 s). A follow-up adds a third step: the app first asks the model
to rewrite the question into a standalone search query, so it waits for two model calls in a row. That is why
follow-ups are about 0.9 s slower than new questions, and why most of a turn passes before the first word. The
rest is Leyline's own work (search, database, the 96-character holdback), which stays small.

**The deployment's own ceiling.** The `gpt-5.4-mini` deployment allows 50 requests and 50,000 tokens per minute
for the whole app, and the embedding deployment has the same limits. A turn costs about 600 tokens per call, so
requests bind first: counting the rewrite, a turn is about 1.7 model calls on average, which puts the ceiling near
30 questions a minute for everyone together. The 8-user run held about 17 questions a minute with no throttling.
The ceiling itself was **not** tested (that would need more users and would throttle the deployment).

**What this does not show:**
- It is the app on a laptop, not the deployed containers or the small B1ms database, and the sign-in was
  Keycloak, not Entra. The deployed app sits in the same region as the model, so its model calls should be
  faster than from a laptop; that is expected, not measured.
- 8 users for 5 minutes, one container, a short run: no soak, no behaviour at the quota ceiling.
- 37 and 87 turns are enough to see the shape, not to trust a 95th percentile to the nearest 100 ms.

**Open question (needs a decision, not a tweak):** the first-token target. Options: keep 2.5 s and reduce the
work before the first word (for example, skip the rewrite when a question already stands alone, which saves
about a second on follow-ups; this changes retrieval behaviour, so it needs the retrieval and answer evals), or
agree targets per kind of question from this baseline. Until one of those is done, the latency gate stays open.

**Running it against a real model** (do not commit keys):
1. Keep the key out of files: put it in the shell environment for the command only, and reference it from a
   compose override kept outside the repository (chat and embedding base URL, model names, key, the Azure floors,
   empty embedding prefixes, `EMBEDDING_SEND_DIMENSIONS=true`, `LLM_SEND_TEMPERATURE=false`, a throwaway
   `DATABASE_URL`), listed after `loadtest/compose.loadtest.yaml`.
2. Create and migrate a throwaway database, start the app and worker, run `python -m leyline.seed`, and wait for
   every ingestion job to succeed.
3. **Warm Keycloak first.** A cold Keycloak made the app's first calls time out and every sign-in return 503; a
   45-second single-user run first avoids it.
4. `LOADTEST_WAIT_MIN=10 LOADTEST_WAIT_MAX=20 locust ... -u 8`, then `docker compose down` so no container keeps the
   key.
