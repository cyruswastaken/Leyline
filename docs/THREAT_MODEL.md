# Threat model

Scope: the Leyline web app, worker and their data stores as built through Phase 6, running with
synthetic data. It uses STRIDE for the system and the OWASP Top 10 for LLM Applications (2025) for
the model-specific risks. Controls are listed with the test that proves each one; the red-team
suite is `tests/test_redteam.py` (T-numbers below match its test names).

## Assets

| Asset | Why it matters |
| --- | --- |
| Document text and ACLs | The product's promise is that people only get answers from documents they may read |
| Conversations, tickets, feedback | Per-user content that may quote sensitive questions |
| Session and identity tokens | Impersonation |
| Security events and audit trail | Evidence; must not be altered or leak content |
| System prompt and configuration | Reveals defences; secrets must never be in it |

## Trust boundaries

1. **Browser to app.** Untrusted input: message text, ids, headers. Identity comes only from the server-side session.
2. **App to model.** The model is *untrusted*: it can be wrong, manipulated by retrieved text, or simply malicious. It gets no tools and only text the user may already read.
3. **Document text to prompt.** Retrieved text is attacker-controllable (anyone who can upload, or edit a source) and is treated as data.
4. **Support agents.** Trusted for tickets only; not trusted with documents they have no group for.
5. **App to Postgres/Redis/Blob.** Internal; credentials from configuration, the app role should be least-privileged.

## Threats and controls

| ID | Threat (STRIDE) | Controls | Evidence |
| --- | --- | --- | --- |
| T1 | **Information disclosure:** a user gets text from a document they may not read, directly or by talking the model into it | ACL predicate inside both search legs; `authorize_hits` re-check; the model only ever receives authorized text, so even a fully compromised model has nothing restricted to leak; assistant text is never fed back into later prompts | `test_t1_*` (compromised model, 8 paraphrased attacks, identity smuggling); `test_chat_access.py`; retrieval evals gate `acl_leaks == 0` |
| T2 | **Tampering / injection via documents (LLM01, indirect):** poisoned text tries to close delimiters, forge sources or give orders | Context wrapped in `<untrusted_context>`; look-alike tags neutralised; numbered sources; grounding rules treat context as data; no tools to abuse | `test_t2_*`, `test_guardrails.py` |
| T3 | **Information disclosure via output (LLM02, LLM07):** credentials, the system prompt, or markup-based exfiltration in the answer | Rolling-buffer credential redaction; verbatim system-prompt reproduction replaced by a refusal and logged (`PROMPT_LEAK_BLOCKED`); pages render model text with `textContent` only and never create links or images; no secrets in prompts or logs | `test_t3_*`, `test_pages_never_build_markup_from_data` |
| T4 | **Spoofing of provenance (LLM09):** forged citations make an answer look sourced | Citation markers verified against the sources actually retrieved; uncited sources marked "consulted"; stored citations re-authorized on display | `test_t4_*`, `test_conversations.py` |
| T5 | **Elevation of privilege / IDOR:** reading or changing another user's conversation, message, ticket or draft | Owner scoping in every query; ticket visibility inside the provider; absent and foreign objects answer identically (404); app roles never grant document access; route x persona matrix | `test_t5_*`, `test_access_matrix.py` |
| T6 | **Denial of service (LLM10):** flooding, huge inputs, model outages | Per-IP and per-user Redis rate limits (also tickets); message and request size limits; model timeouts, retries and a circuit breaker with a 503 degraded mode; bounded context budget | `test_t6_*`, `test_llm.py`, `test_rate_limit.py` |
| T7 | **Repudiation:** someone denies an action or an event is altered | Append-only `security_events` and `audit_events` (trigger rejects UPDATE/DELETE); events carry ids and reasons, never content; stdout copy for the log platform | `test_security_events.py` |
| T8 | **Tampering with uploads:** malicious files | Validation by content, size/page/expansion bounds, storage under ids, parsers in a worker with a job lease | `test_ingestion_*`, `test_regressions.py` |
| T9 | **Spoofing of sessions:** theft, fixation, CSRF | Opaque server-side sessions (rotated on login), `HttpOnly`/`Secure`/`SameSite` cookie, CSRF token on every state change, CSP with nonces, fail-closed on store failure | `test_sessions.py`, `test_csrf_and_headers.py` |
| T10 | **Supply chain (LLM03):** vulnerable or malicious dependencies and images | Pinned lock file; `pip-audit`, Trivy image scan, CodeQL, gitleaks in CI (`.github/workflows/security.yml`) | CI |

## OWASP Top 10 for LLM Applications mapping

| Item | Status |
| --- | --- |
| LLM01 Prompt injection | Mitigated by design: authorization before retrieval, no tools, delimited context, injection scoring (high-confidence block). Cannot be eliminated, which is why the model is never trusted (T1, T2) |
| LLM02 Sensitive information disclosure | Mitigated: model only sees what the user may read, output redaction, event log carries no content (T1, T3) |
| LLM03 Supply chain | Pinned deps, scanners in CI, local models pulled by name (T10); model provenance for Ollama is not verified |
| LLM04 Data and model poisoning | Partly: only `kb_admin` uploads; documents are validated; no fine-tuning. A malicious admin could still plant misleading text |
| LLM05 Improper output handling | Mitigated: text-only rendering, verified citations, redaction (T3, T4) |
| LLM06 Excessive agency | Not applicable: the model has no tools; ticket creation is a user-confirmed action |
| LLM07 System prompt leakage | Mitigated: no secrets in the prompt; verbatim reproduction is blocked (T3). The prompt itself is not secret and is in the repository |
| LLM08 Vector and embedding weaknesses | ACL enforced in SQL for the vector leg; recorded-embedding eval cache is checked in CI; embedding inversion is out of scope with synthetic data |
| LLM09 Misinformation | Reduced by grounding, citations and refusal floors; measured by `leyline.evals.answers`; not eliminated, especially with a small model |
| LLM10 Unbounded consumption | Rate limits, size limits, context budget, timeouts, breaker (T6). Cost metering is estimated tokens only |

## Residual risks and accepted gaps

- **Group changes lag** until the session ends (default 12 h); document-side ACL changes are immediate.
- **Near-topic out-of-scope questions** can still retrieve loosely related (but readable) documents.
- **Model quality:** a small local model omits citations about one time in six and sometimes refuses wrongly. The security properties do not depend on it; answer quality does.
- **Injection scoring is evadable.** It is a tripwire and a log signal, not a boundary.
- **Parsers run unsandboxed** in the worker (bounded inputs, lease-limited).
- **Event storage is synchronous** and fails open: if the database rejects an event the request continues, an `EVENT_STORE_FAILED` line goes to stdout, and the event is lost from the table. A queue-backed sink is future work.
- **`security_events` is append-only by trigger.** `TRUNCATE`, a superuser, or table drops bypass it; production must give the application role INSERT and SELECT only.
- **Support agents see ticket text** in full, including anything a user chose to paste. The draft excludes assistant and document text; it cannot stop a user pasting it.
- **Azure-specific controls** (managed identity, Key Vault, network rules, Entra group overage) are Phase 7.
- Third-party services (Keycloak, Ollama, Azurite) are development stand-ins and were not security tested.

## How to extend

Add the attack to `tests/test_redteam.py` first, watch it fail if the defence is missing, then fix.
Re-run the answer evaluation and retrieval gates after any prompt, model or retrieval change.
