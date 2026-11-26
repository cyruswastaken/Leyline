# 0013. Token usage and cost live on the turn's event, not on the message

**Status:** Accepted (Phase 10).

## Context
Token use and cost were rough estimates. Production needs the provider's own counts, per answered turn, with the
model and the wording of the prompt that produced the answer, and the auditor needs them summed by day. The
plan was to add columns to `messages`.

## Decision
- The chat model port fills a caller-supplied `Usage` object (so a model shared between threads holds no
  per-request state). Streams ask the provider to include usage; a server that reports nothing falls back to an
  estimate, and the record says which.
- Each answered turn's `NORMAL_REQUEST` security event carries the model, a prompt version (a hash of the system
  and rewrite prompts, so it cannot go stale), prompt and completion tokens (the follow-up rewrite included),
  the token source, the cost at the configured prices, and latency. Numbers and names only, never text.
- The auditor's summary sums these by day; the metrics `leyline.llm.tokens` and `leyline.llm.cost` carry the same.
- Not stored on `messages`: conversations are owner-scoped on every query, and summing across owners from that
  table would be the first query that is not. Events are already append-only, auditor-only and text-free.

## Consequences
- No migration was needed. Usage is retrievable per turn only through the event stream, not next to the
  conversation (the owner does not see it).
- Cost is only as right as the configured prices (0 when unset).

## Alternatives considered
Columns on `messages` (breaks owner scoping for aggregates); a separate `usage` table (a migration for the same
data the events already hold); estimates only (what existed).

## Evidence
`tests/test_usage.py` (counts, cost, rewrite included, estimate fallback, no text stored, auditor summary,
other roles denied) and `tests/test_llm.py` (the adapter against a local server speaking the provider protocol).
