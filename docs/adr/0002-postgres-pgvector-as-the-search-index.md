# 0002. Postgres with pgvector is the search index, not Azure AI Search

**Status:** Accepted (Phase 8). Changes the original plan, which named Azure AI Search.

## Context
The roadmap planned Azure AI Search for the cloud. By Phase 8 hybrid search (keyword plus vector, merged by
reciprocal-rank fusion, with the ACL filter in the same query) already ran on Postgres and passed every
retrieval and ACL gate. The budget is an Azure for Students credit of about 100 USD.

## Decision
Keep Postgres (with pgvector and full-text search) as the search index in Azure. The `SearchIndex` port stays,
so an AI Search adapter can still be added.

## Consequences
- One fewer service to run, secure and pay for; the ACL filter stays inside a query we test.
- Vector search runs on a small Burstable Postgres: fine for tens of documents, not for large corpora.
- An AI Search adapter, if added, must pass the same contract tests and ACL evaluations.

## Alternatives considered
Azure AI Search free tier (one per subscription, 50 MB: too small to rely on) and the paid tiers (would use most
of the credit).

## Evidence
Hybrid search on the Azure embedding model measured recall@1 0.963 and recall@5 1.000 with zero ACL leaks
on the 78-question golden set (see 0007).
