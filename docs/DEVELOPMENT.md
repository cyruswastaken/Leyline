# Development

## Working conventions

- Routes live in `src/leyline/web/`; application logic lives in
  `src/leyline/services/`. Routes validate input and delegate.
- Read configuration only through `leyline.config.get_settings()`.
- Preserve the authorization-before-retrieval ordering.
- Keep corpus documents small, synthetic, and explicit about their access
  level in the leading header block.
- Do not commit `.env` files, API keys, generated logs, or virtual
  environments.
- Prefer focused changes, and keep the full suite green.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install --no-deps -e .
pre-commit install
```

## Frontend

The interface is built from `frontend/` (Node 22 and npm; see [FRONTEND.md](FRONTEND.md)):

```powershell
cd frontend
npm ci
npm run check      # lint, typecheck, unit tests, build, bundle budget (what CI runs)
```

`npm run build` writes `src/leyline/static/dist/`, which the app serves and the Docker image builds
for itself. For a fast loop against the real stack use `compose.frontend.yaml`.

## Validation

CI (`.github/workflows/ci.yml`) runs the same checks you can run locally:

```powershell
ruff check .
ruff format --check .
mypy
pytest --cov
docker build -t leyline:dev .
```

`mypy` runs in strict mode. On Windows machines whose Application Control
policy blocks compiled extensions, install the pure-Python build with
`pip install --no-binary mypy --force-reinstall mypy`.

## Database and migrations

The schema lives in `src/leyline/db/models.py` and is changed only through Alembic migrations in `src/leyline/db/migrations/versions/`. The driver is **pg8000** (pure Python), so the same code runs on locked-down laptops, in CI and in the container. The pgvector extension is created by the first migration.

```powershell
python -m leyline.db upgrade                       # apply migrations
python -m leyline.db current                       # show the revision
python -m leyline.db check                         # exit 1 if not at head
python -m leyline.db revision -m "add something"   # autogenerate from the models
```

After changing a model, run `upgrade`, then `revision -m ...`, review the generated file, and commit both. A test fails if models and migrations disagree. Never edit a migration that has been merged; add a new one.

## Tests

Most tests need no running services: Redis is replaced by `fakeredis`, the identity provider by a fake client and the database by a stub that fails the test if touched. Shared personas, fakes and helpers are in `tests/support.py`; `login_as(client, principal)` creates a real server-side session without going through an identity provider.

Integration tests (ACLs, ingestion, the worker, the admin API, migrations) use a **real Postgres**: start it with `docker compose up -d postgres azurite`. A session-scoped fixture drops and recreates a `leyline_test` database and applies the real migrations, and the `db` fixture empties the tables before each test. Without Postgres they are skipped locally; with `CI=true` (or `LEYLINE_REQUIRE_DB=1`) they fail instead, so CI can never silently skip them. Override the connection with `TEST_DATABASE_URL`.

- `test_access_matrix.py` is the single declaration of who may call which route and fails if a route has no declared policy. **Add every new route there.**
- `test_regressions.py` holds one test per audited vulnerability.
- `test_search_acl.py` and `test_chat_access.py` are the permission-model tests: who sees what, and that ACL changes and deletions apply immediately.
- `test_ingestion_service.py` covers the document lifecycle, retries, leases and concurrent workers.
- What fakes cannot prove (Authlib's token verification, Keycloak claim mapping) is covered by `python scripts/e2e_login.py` against the running compose stack; CI runs it on every push.

## Retrieval evaluation

Retrieval changes are judged by numbers: `python -m leyline.evals` (details, results and the
record/replay workflow in [EVALUATION.md](EVALUATION.md)). CI enforces `evals/thresholds.json`.
Never lower a floor to make a change pass, never edit golden questions to fit the search, and
re-record `evals/embeddings.json` (needs a local model) whenever the corpus, chunker, golden set,
model or a prefix changes. A stale cache fails loudly with the exact command to run.

The full suite takes about 6 minutes (it seeds real Postgres databases); run a subset while
iterating, e.g. `pytest tests/test_hybrid_search.py -q`.

## Dependencies

`pyproject.toml` declares dependency ranges. `requirements.txt` is the pinned
runtime lock used by the Docker image; regenerate it from a clean virtual
environment when ranges change, then run the full suite.
`requirements-dev.txt` adds pinned test and lint tooling.

## Security-sensitive changes

Any change to identity handling, role mapping, document filtering, prompt
construction, or logging must include tests for both the allowed and the
denied paths. Every audited vulnerability has a regression test in
`tests/test_regressions.py`. Never weaken authorization to make a query
return results.

## Roadmap

Planned phases and acceptance criteria are in [ROADMAP.md](ROADMAP.md).
