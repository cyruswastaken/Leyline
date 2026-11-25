# Service objectives

What "working" means for the deployed Leyline, how each objective is measured, and which alert guards it. The
numbers are targets for a single-region demo deployment with one replica and a small database; they are
written down so a change can be judged against them and so an alert means something. Nothing here is a
promise to anyone.

Everything is measured from what the containers already write to Log Analytics, so there is no extra service.
Run the queries in the portal under **Log Analytics workspace `log-leyline` > Logs**.

| Objective | Target | Measured by | Alert |
| --- | --- | --- | --- |
| **Availability** | `/readyz` answers 200 in at least 99% of 5-minute windows over 30 days | the platform probes `/readyz` every 15 s | `leyline-site-down` (no 200 for 10 minutes), `leyline-server-errors` (5 or more 5xx in 10 minutes) |
| **First token** | 95th percentile of time from question to first word under **2.5 s** | `first_token_ms` on each streamed answer's `NORMAL_REQUEST` event | `leyline-slow-first-token` (over 2.5 s across 30 minutes, at least 10 answers) |
| **Whole answer** | 95th percentile of the whole turn under **6 s** | `latency_ms` on the same event | none yet (the load test gate covers it) |
| **Model health** | fewer than 2% of turns end in a model error | `MODEL_ERROR` events against answered turns | `leyline-model-errors` (5 or more in 10 minutes), `leyline-model-throttled` (3 or more HTTP 429 in 10 minutes) |
| **Event record** | no security event is dropped | `EVENT_STORE_FAILED` lines | `leyline-event-store-failing` (any) |
| **Ingestion** | the worker is alive | a "worker heartbeat" line every minute | `leyline-worker-silent` (none for 10 minutes) |
| **Database** | CPU under 80% and storage under 85% | Postgres metrics | `leyline-database-cpu`, `leyline-database-storage` |

## Error budget

A 99% availability objective over 30 days allows about **7.2 hours** (432 minutes) without a successful readiness
probe. Spend it on planned changes (a rebuild takes about 40 minutes; an image-only deploy under a minute) and on
incidents; if more than half is gone, stop shipping features until the cause of the spend is fixed. With a single
replica a deploy that fails its readiness check keeps the old revision serving (section 15 of the runbooks), so
most deploys cost nothing.

## The first-token objective and the latency gate

The first-token target of 2.5 s is the production-grade gate that has stayed open (`docs/ROADMAP.md`): the
laptop load test measured 3.0 s at the 95th percentile, and the stages timed from inside Azure add up to about
1.5 s for a new question. The query below is the end-to-end figure from real traffic on the deployed site, which is
what closes the gate. Follow-ups that really need a rewrite add one model call; the `rewrite_skipped` field on the
matching `RETRIEVAL` event says which kind of turn it was.

## Queries

First token and whole answer, last 24 hours:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'leyline-web'
| where Log_s has 'NORMAL_REQUEST' and Log_s has 'first_token_ms'
| extend e = parse_json(Log_s)
| extend first_token = todouble(e.details.first_token_ms), whole = todouble(e.details.latency_ms)
| where TimeGenerated > ago(24h)
| summarize answers = count(),
            first_token_p50 = percentile(first_token, 50), first_token_p95 = percentile(first_token, 95),
            whole_p95 = percentile(whole, 95)
```

Availability by day (share of 5-minute windows with at least one successful readiness probe):

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == 'leyline-web'
| where Log_s has 'GET /readyz'
| summarize ok = countif(Log_s has '" 200 ') by bin(TimeGenerated, 5m)
| summarize windows = count(), good = countif(ok > 0) by day = bin(TimeGenerated, 1d)
| extend availability = round(100.0 * good / windows, 2)
```

Model errors by reason, last 24 hours:

```kusto
ContainerAppConsoleLogs_CL
| where Log_s has 'MODEL_ERROR'
| extend e = parse_json(Log_s)
| where TimeGenerated > ago(24h)
| summarize n = count() by reason = tostring(e.reason)
```

Cost and tokens by day come from the auditor page (Signals) and the `leyline.llm.tokens` / `leyline.llm.cost` metrics.

## What the objectives do not cover

Sign-in at Microsoft Entra (not our service), quality of answers (measured by the evaluations, not live),
and anything about the laptop or network of the person using it. Alerts run on logs, so a site that is down
and not logging at all is caught by the "site down" rule (it fires on silence), not by an error count.
