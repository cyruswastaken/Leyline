# Usage

This guide covers running Leyline locally.

## Prerequisites

- Python 3.11 or newer (CI tests 3.11 and 3.13; the container uses 3.13)
- Docker Desktop (Postgres, Redis, Keycloak and Azurite run in containers)
- An Azure OpenAI deployment (or another OpenAI-compatible endpoint) to get answers from `/chat`. Without one you can sign in, upload and search; a question that finds relevant documents fails when it reaches the model.

## Configuration

Copy `.env.example` to `.env` and fill in the Azure OpenAI values. Do not commit `.env`. All settings are validated at startup by [`config.py`](../src/leyline/config.py); the example file lists every option. The compose file already wires Postgres, Redis, Keycloak and Azurite, so a local `.env` only needs the model settings (and, optionally, an embedding model; see below).

`LEYLINE_ENV=production` refuses to start unless the model, identity, database and storage settings are present, session cookies are `Secure`, and `FLASK_DEBUG` is off.

## Run with Docker

```powershell
docker compose up --build
docker compose run --rm app python -m leyline.seed   # load the sample corpus (49 documents)
```

This starts the app, the ingestion **worker**, Postgres (with pgvector), Redis, a Keycloak configured from [`deploy/keycloak/leyline-realm.json`](../deploy/keycloak/leyline-realm.json), and Azurite (Azure Blob Storage emulator). A one-shot `migrate` container brings the database schema up to date first. Open `http://localhost:8000/` and sign in. The first start takes about a minute while Keycloak imports the realm.

Seeding only queues the documents; the worker parses, chunks and indexes them in the background (a few seconds for the sample corpus).

### Demo users

All demo users share the throwaway password `Leyline-dev-1!` and exist only in the local Keycloak.

| User | Groups (document access) | App role |
| --- | --- | --- |
| `alice.employee` | staff | user |
| `sam.security` | staff, **security** | user |
| `ada.auditor` | staff | **security_auditor** |
| `kim.admin` | staff | **kb_admin** |
| `sue.agent` | staff | **support_agent** |
| `hank.hr` | staff, **hr** | user |
| `fiona.finance` | staff, **finance** | user |

Groups decide which documents someone can **read**; app roles decide what they can **do**. For example `kim.admin` manages the knowledge base but, being only in `staff`, cannot read HR or security documents through chat.

### Try it

- As `alice.employee`, ask about the *internal network architecture segments*: the restricted document is never used. As `sam.security`, it is.
- As `kim.admin`, open **Documents** (`/admin/documents`), upload a Markdown file for the `hr` group, and watch it go *queued → indexing → indexed*. Then as `hank.hr` ask about its content; as `alice.employee` you cannot find it. Edit the groups in the admin page and ask again: the change applies to the very next question.
- Only `ada.auditor` can open **Monitoring** (`/monitoring`).

The Keycloak admin console is at `http://localhost:8080/` (`admin` / `admin-dev-only`). The sample corpus lives in [`corpus/`](../corpus); see "Corpus format" below.

## Documents and permissions

Each document has an **ACL**: the list of groups allowed to read it. A user can read a document when one of their groups is in its ACL, or the ACL contains the special entry `*` (every signed-in user). A document with an empty ACL is readable by nobody.

Administrators (role `kb_admin`) can upload `.pdf`, `.docx`, `.md` and `.txt` files (up to 10 MB), upload new versions, change ACLs, re-index and delete. They never see document text through the admin API, only metadata and status. Files are validated by their content, not by their name or declared type.

A new version stays invisible until it is fully indexed and then replaces the previous version atomically. Deleting a document makes it unsearchable immediately; its stored files are purged shortly afterwards.

### Semantic search (optional, recommended)

Without an embedding model, search is keyword-only. With one, retrieval becomes **hybrid**:
a keyword leg and a semantic (vector) leg, each permission-filtered inside its own query,
fused by reciprocal rank and gated by relevance floors. Questions are then matched by meaning
("someone stole my backpack" finds the *lost or stolen device* policy). On the golden set
this moves recall@5 from 0.80 to 1.00 and correctly refuses 83% of out-of-scope questions
(was 25%); see [EVALUATION.md](EVALUATION.md).

Any OpenAI-compatible embeddings endpoint works. The recommended local setup uses Ollama:

```powershell
winget install Ollama.Ollama.Portable      # or install Ollama from ollama.com
ollama serve                               # starts the local API on port 11434
ollama pull nomic-embed-text               # about 275 MB, 768-dimension vectors
```

Then create a git-ignored `.env` next to `compose.yaml` (quotes keep the trailing spaces):

```text
EMBEDDING_BASE_URL=http://host.docker.internal:11434/v1   # http://localhost:11434/v1 when running on the host
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_QUERY_PREFIX="search_query: "
EMBEDDING_DOCUMENT_PREFIX="search_document: "
```

`nomic-embed-text` is trained with those task prefixes; quality drops without them. Restart the
stack, then index the documents (`docker compose run --rm app python -m leyline.seed` for the
sample corpus, or **Re-index** in the admin page). `/readyz` reports
`optional.embeddings_configured: true` when it is on. Each indexed version records the model
that produced its vectors; changing the model or a prefix marks documents **needs re-index**
(the old chunks keep serving until the new ones replace them).

The vector size is fixed at 768 in the schema. `text-embedding-3-small` (OpenAI or Azure OpenAI)
also works: set `EMBEDDING_SEND_DIMENSIONS=true` so the API returns 768 dimensions, and
re-calibrate the relevance floors (below). If the embedding service is down at question time,
search falls back to keyword results instead of failing.

**Relevance floors.** `SEARCH_MIN_SIMILARITY` (default 0.58) and `SEARCH_MIN_LEXICAL`
(default 0.5) decide what counts as relevant at all, which is how out-of-scope questions get
"I couldn't find relevant information" instead of weakly related text. The defaults are
calibrated for `nomic-embed-text` with the prefixes above; for another model run
`python -m leyline.evals --index hybrid --sweep` (see [EVALUATION.md](EVALUATION.md)).

### Corpus format

Corpus files start with a header block, then the body:

```text
Document Title: Annual Leave Policy
Document ID: HR-LEAVE-001
Groups: *

# Annual Leave Policy
...
```

`Groups:` is a comma-separated ACL (`*` for everyone). The older `Access Level: general | restricted` header is still understood (`general` = everyone, `restricted` = the `security` group). Metadata is read only from the leading header block. Re-running the seed command is safe: unchanged files are skipped, changed files become new versions, and ACLs you changed in the admin page are never overwritten.

## Run with Python

Run the services with Docker and the app on the host:

```powershell
docker compose up -d postgres redis keycloak azurite
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pip install --no-deps -e .

$env:DATABASE_URL = "postgresql+pg8000://leyline:leyline-dev-only@localhost:5432/leyline"
$env:BLOB_CONNECTION_STRING = "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://localhost:10000/devstoreaccount1;"
$env:OIDC_METADATA_URL = "http://localhost:8080/realms/leyline/.well-known/openid-configuration"
$env:OIDC_CLIENT_ID = "leyline-web"
$env:OIDC_CLIENT_SECRET = "leyline-dev-secret-change-me"

python -m leyline.db upgrade     # create/update the schema
python -m leyline.seed           # queue the sample corpus
python -m leyline.worker         # in a second terminal: index documents
python -m leyline.app            # the web app
```

`python -m leyline.app` serves `http://localhost:5000/`. The realm registers redirect URIs only for port 8000, so for host-run development either add `http://localhost:5000/auth/callback` to the client in Keycloak or run the app with gunicorn on port 8000. Set `FLASK_DEBUG=true` only on a trusted machine; the debugger allows remote code execution. (The Azurite key above is Microsoft's published development key, not a secret.)

`python -m leyline.worker --once` drains the queue and exits; `python -m leyline.seed --index` seeds and indexes in one go without a separate worker.

## HTTP endpoints

| Method | Endpoint | Access | Purpose |
| --- | --- | --- | --- |
| `GET` | `/healthz` | public | Liveness: the process is up |
| `GET` | `/readyz` | public | Readiness: Redis, database, schema, storage, identity provider and model (503 otherwise) |
| `GET` | `/login` | public | Start sign-in (`?next=/path` returns you afterwards) |
| `GET` | `/auth/callback` | public | Identity provider redirect target |
| `POST` | `/logout` | signed in + CSRF | End the session and the identity provider's session |
| `GET` | `/me` | signed in | Current user, roles, groups and CSRF token |
| `GET` | `/` | signed in | Chat interface |
| `POST` | `/chat` | signed in + CSRF | Ask a question |
| `GET` | `/monitoring`, `/monitoring/summary`, `/monitoring/events` | `security_auditor` | Security dashboard and events |
| `GET` | `/admin/documents` | `kb_admin` | Knowledge-base admin page |
| `GET`, `POST` | `/api/admin/documents` | `kb_admin` (+ CSRF) | List documents; upload a new one (multipart: `file`, `title`, `groups`) |
| `GET`, `DELETE` | `/api/admin/documents/<id>` | `kb_admin` (+ CSRF) | Details; delete |
| `POST` | `/api/admin/documents/<id>/versions` | `kb_admin` + CSRF | Upload a new version |
| `PUT` | `/api/admin/documents/<id>/acl` | `kb_admin` + CSRF | Replace the ACL (`{"groups": [...]}`) |
| `POST` | `/api/admin/documents/<id>/reindex` | `kb_admin` + CSRF | Queue a re-index |

Unauthenticated browser navigation is redirected to `/login`; unauthenticated API calls get `401`; signed-in users without the required role get `403`. State-changing requests need the `X-CSRF-Token` header (the pages read it from a `<meta>` tag; `/me` also returns it).

## Behavior

- Identity comes only from the signed-in session; nothing in the request can change who you are.
- Search only ever considers documents the user may read, and every hit is checked against the database again before it is given to the model.
- Blocked prompt-extraction patterns return HTTP 403 before retrieval.
- Rate limits return HTTP 429: 10 chat requests per user and 120 requests per client IP, per 60 seconds by default.
- Invalid input returns HTTP 400: non-JSON bodies, non-string or empty messages, and messages over 2,000 characters.
- Questions with no sufficiently relevant context (below the relevance floors) return an unavailable response rather than an invented answer.
- If Redis or the database is unavailable, requests that need them return 503.

## Tests and checks

```powershell
docker compose up -d postgres azurite   # needed by the integration tests
pytest --cov      # about 580 tests
ruff check .      # lint
ruff format .     # formatting
mypy              # strict type checking
```

Most tests are hermetic (fake Redis, fake identity provider, stubbed database). Integration tests use a real Postgres: they create a throwaway `leyline_test` database, apply the real migrations, and run the actual search, ingestion and worker code. If Postgres is not reachable they are skipped locally; in CI (`CI=true`) they fail instead.

With the full stack running and the corpus seeded, `python scripts/e2e_login.py` performs the real sign-in flow for every demo user, uploads a document as an administrator, waits for the worker, and checks who can retrieve it as its ACL changes, through to deletion. CI runs it too.
