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
