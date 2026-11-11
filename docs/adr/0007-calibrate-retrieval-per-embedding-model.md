# 0007. Retrieval floors are calibrated and gated per embedding model

**Status:** Accepted (Phase 8). Extends the Phase 3 decision.

## Context
Search drops chunks below a similarity floor and a keyword floor, because fused ranks cannot say "nothing
relevant". Cosine similarity has a different scale for every model. The shipped floors were calibrated for
`nomic-embed-text`. On Azure's `text-embedding-3-small` they found the right document for only 43% of
questions, and the first live test answered "I couldn't find relevant information" to everything.

## Decision
- Azure has its own recording (`evals/embeddings.azure.json`), floors (`SEARCH_MIN_SIMILARITY=0.37`,
  `SEARCH_MIN_LEXICAL=0.60`, set in `infra/modules/apps.bicep`) and regression gate
  (`evals/thresholds.azure.json`, `tests/test_evals_azure.py`, which reads the floors from the Bicep file).
- The floors sit in the middle of a measured plateau (0.35 to 0.40 gives identical results), not at an edge.
- Floors are never lowered to make a change pass; `acl_leaks` is 0 and `restricted_pass_rate` 1.0, always.

## Consequences
- Changing the model, the corpus, the chunker or a prefix means re-recording and re-sweeping.
- Recordings must be made with no task prefixes and from a folder without a `.env`: a local `.env` with the
  old model's prefixes silently contaminated the first recording.
- Two recordings are committed (about 900 KB each).

## Alternatives considered
Keep the old floors (unusable), lower them by feel (no evidence, no gate), keep `nomic-embed-text` in Azure as an
extra always-on container (cost and operations).

## Evidence
On the 78-case golden set: recall@1 0.426 with the old floors and 0.963 with the new; recall@5 1.000, refusal
1.000, zero ACL leaks; confirmed on the live site by cited answers and a refused out-of-scope question.
