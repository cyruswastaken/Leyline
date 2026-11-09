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
