# 0015. Alerts are built on the logs the containers already write

**Status:** Accepted (Phase 11). Written and compiled; **no alert has been seen firing in Azure yet**.

## Context
Production needs to know when the site is down, slow or losing events, without someone watching. The plan was to export
the OpenTelemetry metrics to Application Insights. That needs the Azure Monitor exporter package (compiled parts a
locked-down machine may block) and an extra service, and the first-token metric would still have to be wired up.

## Decision
- Alert rules are Log Analytics queries over what the containers already write (`infra/modules/monitoring.bicep`):
  readiness probe silence, 5xx bursts, the first-token 95th percentile (`first_token_ms` now on each streamed
  answer's `NORMAL_REQUEST` event), model errors and throttling (HTTP 429), dropped events, a silent worker (the worker
  now logs a heartbeat each minute), plus Postgres CPU and storage metrics.
- Who is told is a separate action group, `infra/alerts.ps1 -Email`, so the address stays out of the repository;
  `deploy.ps1` and the Deploy workflow attach it to every rule.
- The objectives and the queries that measure them are in `docs/SLO.md`; each alert names a runbook section, checked by
  a test.
- The OpenTelemetry metrics stay (console or OTLP); they are not the alerting path.

## Consequences
- No new dependency, service or cost beyond the alert rules; everything uses one workspace.
- Alerts react to log text, so renaming an event or a log line silently breaks one; `tests/test_alerts.py` pins
  every token a query depends on to the code that emits it.
- The first-token alert only sees streamed answers and needs 10 of them in 30 minutes: a quiet site cannot trip it.
- Alert state lives in the app resource group and is rebuilt with it; the action group must be re-created after a
  rebuild (`alerts.ps1`).

## Alternatives considered
Application Insights with the Azure Monitor exporter (dependency and service for the same signals); a workbook
dashboard (nothing to alert on); an availability test against the public address (needs Application Insights, and the
readiness probes already say the same).

## Evidence
`tests/test_alerts.py`: every rule names an existing runbook section and an objective in the SLO document; the numbers
agree between the template and the document; the queries read only fields the code writes (including a real streamed
answer's log line parsing as JSON with `first_token_ms`); the gunicorn patterns match real log lines.
