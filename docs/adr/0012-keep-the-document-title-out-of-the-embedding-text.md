# 0012. Keep the document title out of the embedding text

**Status:** Accepted (Phase 10). The option exists (`EMBEDDING_INCLUDE_TITLE`) and is off.

## Context
A chunk such as "Submit receipts within thirty days" never names its subject. Prefixing each chunk's embedding
text with the document title ("Expense Reimbursement Policy") is a common way to keep such chunks near questions
about that subject. Only the embedded text changes; the stored chunk, the keyword index and what is shown to the
model do not.

## Decision
Implemented as `EMBEDDING_INCLUDE_TITLE` (default false) and measured. It is **not adopted**, because the plan
required recall, MRR and refusal to improve or hold, and they did not hold.

## Consequences
- No re-index or re-recording is needed to stay as is.
- Turning it on later means re-indexing every document and re-recording `evals/embeddings*.json`.
- Measured on one embedding model and 126 questions: a difference of one question either way is within noise, so
  this is "no demonstrated benefit", not "proven harmful". It was not tried on the Azure embedding model.

## Alternatives considered
Prepend title and heading together (what was tried); store the title inside the chunk text (would change
keyword matching and the text shown to the model, a larger change for the same idea).

## Evidence
Local model, hybrid, 126 cases. Search only vs with title: recall@1 0.910 vs 0.897, recall@5 1.000 vs 0.987
(the security "credential compromise" question lost its incident-response document), MRR 0.949 vs 0.940,
refusal 0.833 vs 0.833, near-topic refusal 0.083 vs 0.167, follow-up MRR 0.884 vs 0.923, 0 ACL leaks both.
