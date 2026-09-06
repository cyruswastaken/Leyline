# Usage

This guide covers local development and the behavior exposed by the Flask application.

## Prerequisites

- Python 3.11 or compatible
- An Azure OpenAI deployment, or another compatible endpoint
- A shell with access to the repository

## Installation

Create and activate a virtual environment, then install the pinned dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Configuration

Create a local `.env` file. Do not commit it or share its contents.

```text
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<api-key>
AZURE_OPENAI_DEPLOYMENT=<deployment-name>
```

The application loads these values when the language-model service is imported and fails clearly when a required value is missing. Keep credentials in environment variables or a managed secret store; never place them in source files.

## Running the application

```powershell
python app.py
```

Open `http://127.0.0.1:5000/` in a browser.

The local application uses the role values defined in `services/authorization.py`. Authentication is represented by the local identity and role configured in `app.py`; production identity integration is described in the [deployment guide](DEPLOYMENT.md).

## HTTP endpoints

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/` | Chat interface |
| `GET` | `/health` | Health check |
| `POST` | `/chat` | Process a message |
| `GET` | `/documents` | List loaded documents |
| `GET` | `/documents/<user_role>` | List documents allowed for a role |
| `GET` | `/search/<user_role>?q=<query>` | Search authorized documents |
| `GET` | `/monitoring` | Monitoring dashboard |
| `GET` | `/monitoring/summary` | Security event summary |
| `GET` | `/monitoring/events` | Recent security events |

Example request:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:5000/chat `
  -ContentType "application/json" `
  -Body '{"message":"How should I protect my corporate password?"}'
```

## Expected behavior

- General employees can retrieve documents marked `Access Level: general`.
- Security administrators can retrieve both `general` and `restricted` documents.
- Blocked prompt-extraction and document-inventory patterns are rejected before retrieval.
- Requests without relevant context return an unavailable response rather than an invented answer.
- Excessive requests from one identity receive HTTP `429`.

## Validation checklist

1. `GET /health` returns HTTP 200.
2. A general question returns content from a general document.
3. An unknown question returns an unavailable response.
4. An employee cannot retrieve restricted documents.
5. A security administrator can retrieve restricted documents.
6. A blocked-pattern request returns HTTP 403.
7. The rate limiter returns HTTP 429 after the configured threshold.

The detailed Azure identity and deployment test matrix is in the [Portal lab guide](rag-helpdesk-lab-guide.txt).
