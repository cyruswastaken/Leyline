# 0011. No cross-encoder reranker for now

**Status:** Accepted (Phase 10). The reranker code stays, switched off; the decision is to leave it off.

## Context
Retrieval is good on questions the knowledge base covers but weak at refusing questions next to a covered topic
("How many sick days do I get?" next to the leave policy): only 1 of 12 such questions is refused on the local
model and 5 of 12 on Azure. A cross-encoder reranker, which reads the question and each chunk together, is the
usual remedy, and a score floor could turn a weak match into an honest "not found".

## Decision
- Built behind a `Reranker` port (`ports/rerank.py`) with an HTTP adapter for Hugging Face Text Embeddings
  Inference (`adapters/rerank_http.py`), a circuit breaker, and `services/reranking.py`. It runs after the
  authorization re-check, so it only sees text the asker may read; it can reorder or drop hits and can never add
  one; a failure keeps the search order. `RERANK_BASE_URL` unset (the default) means no reranker.
- Measured with `BAAI/bge-reranker-base` (MIT, run locally from `compose.rerank.yaml`). It is **not adopted**:
  it lowers recall and no score floor gives a usable trade.

## Consequences
- Near-topic refusals remain the known weak spot (floors in `evals/thresholds*.json` pin the baseline); the next
  things to try are a better refusal signal in the prompt or a different model, not this reranker.
- The port, adapter, tests and `--rerank-url` sweep stay, so another reranker can be measured in one command.
- Not running a reranker also keeps about 0.8 s (15 candidates on a laptop CPU) off the first token.

## Alternatives considered
A larger or multilingual reranker (more latency on a CPU container); an LLM as reranker (another model round trip
before the first word); a hosted rerank API (a new external service holding retrieved text); tuning the embedding
floors alone (already done, see 0007).

## Evidence
126 golden questions, reranker applied to the 15 best candidates, best 5 kept.

| Index | Setting | recall@1 | recall@5 | MRR | near-topic refusal |
| --- | --- | --- | --- | --- | --- |
| local (nomic) | search only | 0.910 | 1.000 | 0.949 | 0.083 |
| local (nomic) | reranked, no floor | 0.859 | 0.987 | 0.913 | 0.083 |
| local (nomic) | reranked, floor 0.05 | 0.577 | 0.590 | 0.583 | 0.750 |
| Azure | search only | 0.962 | 1.000 | 0.979 | 0.417 |
| Azure | reranked, no floor | 0.872 | 1.000 | 0.931 | 0.417 |
| Azure | reranked, floor 0.02 | 0.667 | 0.692 | 0.679 | 0.750 |

Without a floor it reorders worse than the hybrid search it follows; with any floor that refuses more, it drops
correct answers (the model scores short, paraphrased matches very low). ACL leaks were 0 throughout. One CPU
call for 15 candidates took about 0.8 s.
