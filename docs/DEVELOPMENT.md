# Development

## Working conventions

- Keep application logic in `services/` and route definitions in `app.py`.
- Preserve the authorization-before-retrieval ordering.
- Keep corpus documents small, synthetic, and explicit about their access level.
- Do not commit `.env` files, API keys, generated logs, or local virtual environments.
- Prefer focused changes and validate behavior before opening a pull request.

## Local validation

Install dependencies with:

```powershell
pip install -r requirements.txt
```

Run the application with:

```powershell
python app.py
```

Then follow the checklist in [USAGE.md](USAGE.md). If tests are added, keep them focused on authorization, blocked requests, rate limiting, retrieval, and grounded responses.

## Security-sensitive changes

Any change to identity handling, role mapping, document filtering, prompt construction, or logging should include a review of both allowed and denied paths. Never weaken authorization to make a query return results.
