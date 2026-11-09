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

## Tests

The suite needs no running services: Redis is replaced by `fakeredis` and the identity provider by a fake client, so `pytest` is fast and hermetic. Shared personas and helpers are in `tests/support.py`; `login_as(client, principal)` creates a real server-side session without going through an identity provider.

- `test_access_matrix.py` is the single declaration of who may call which route and fails if a route has no declared policy. **Add every new route there.**
- `test_regressions.py` holds one test per audited vulnerability.
- What fakes cannot prove (Authlib's token verification, Keycloak claim mapping) is covered by `python scripts/e2e_login.py` against the running compose stack; CI runs it on every push.

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
