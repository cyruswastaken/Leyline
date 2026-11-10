# Retrieval evaluation

Retrieval quality and permission safety are measured, not assumed. A fixed set of
questions is run against the same `SearchIndex` interface the chat pipeline uses,
and CI fails on a quality regression or on **any** ACL leak.

## What is measured

The golden set is [`evals/golden.jsonl`](../evals/golden.jsonl): 78 questions, asked as one of
four personas that mirror the demo users (`employee`, `security`, `hr`, `finance`).

| Kind | Count | Correct behaviour |
| --- | --- | --- |
| `answerable` | 54 | A document the asker may read answers it; it should rank high |
| `restricted` | 12 | The real answer is in a document the asker may **not** read; it must never be returned |
| `unanswerable` | 12 | Nothing in the knowledge base answers it; the right result is *no hits* |

Questions are natural paraphrases on purpose ("Someone stole my backpack with my work
laptop inside" for a *lost or stolen device* document). Do not tune them to the
document wording: that would hide exactly the weakness semantic search exists to fix.

| Metric | Meaning |
| --- | --- |
| `recall@1/3/5` | Answerable: an acceptable document is in the top k (documents, not chunks) |
| `mrr` | Answerable: mean reciprocal rank of the first acceptable document |
| `refusal_rate` | Unanswerable: share where nothing was returned |
| `restricted_pass_rate` | Restricted: share where no forbidden document was returned |
| `acl_leaks` | **Every** case: documents returned that the asker may not read. Must be 0 |

`acl_leaks` is checked against the access rules declared in the corpus files themselves, an
oracle independent of the database and of the search code under test. The golden set is
validated against that oracle on load: an "answerable" case whose answer the persona cannot
read, or a "restricted" case they actually can read, is rejected.

## Results

Measured on the 78-case set (`python -m leyline.evals`), `nomic-embed-text` for the hybrid rows:

| | recall@1 | recall@3 | recall@5 | MRR | refusal | restricted | leaks |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Keyword only (Phase 2) | 0.519 | 0.741 | 0.796 | 0.640 | 0.250 | 1.000 | 0 |
| Hybrid, no relevance floors | 0.667 | 0.889 | 0.926 | 0.789 | 0.000 | 1.000 | 0 |
| **Hybrid, calibrated floors (shipped)** | **0.889** | **0.981** | **1.000** | **0.936** | **0.833** | **1.000** | **0** |

The keyword search failed on vocabulary gaps ("stolen backpack" vs *lost or stolen device*,
"expecting a baby" vs *parental leave*) and returned junk for out-of-scope questions
because common words matched. Its scores for answerable and unanswerable questions
overlapped completely, so no score threshold could fix refusals; semantic scores can.

**Known remaining misses:** two unanswerable questions are *near-topic* to the corpus and
still return documents ("Can I bring my dog to work?" lands on visitor and access
documents; "personal income tax" on expenses). A similarity floor cannot fully separate
near-topic out-of-scope questions; the grounding prompt and citations handle those.

**On over-fitting:** the two floors were chosen on this same set, so the gains were
checked on independent halves (odd and even cases). Both improve consistently
(recall@5 0.70/0.89 -> 1.00/1.00; MRR 0.57/0.71 -> 0.98/0.90). The result is a plateau, not a
knife edge (similarity 0.55-0.60 and lexical 0.4-0.6 behave alike) with a cliff at
similarity 0.65, so the shipped 0.58 / 0.5 leaves margin. Re-check this as the corpus and
golden set grow.

## Answer quality (manual gate)

`python -m leyline.evals.answers [--verbose] [--json out.json]` runs every golden question through
the real pipeline (retrieval, re-authorization, delimited prompt, the configured chat model,
citation verification) and scores the **answers**. It needs a chat model, so unlike the retrieval
gate it is run by hand and its results are recorded here. There is no model-as-judge: each metric
is computed from the sources shown to the model and the corpus ACLs.

| Metric | Meaning |
| --- | --- |
| answered | answerable questions that got an answer instead of a refusal |
| cites a source | answers with at least one valid `[n]` citation |
| cites an expected document | a cited source is one the golden case expects |
| figures supported | every number in the answer appears in the sources (cheap check against invented figures) |
| refused | unanswerable questions that were refused |
| restricted pass | restricted questions where no forbidden document was shown, cited or named |
| ACL leaks | sources shown to the model that the asker may not read (must be 0) |

Result with `qwen2.5:3b` (Ollama, RTX 3050 4 GB), hybrid retrieval, 78 questions, 2026-11-10:

| answered | cites a source | cites expected doc | figures supported | refused | restricted pass | ACL leaks |
| --- | --- | --- | --- | --- | --- | --- |
| 0.926 | 0.833 | 0.778 | 0.981 | 1.000 | 0.917 | 0 |

How to read it: permission safety held (no leaks, every unanswerable question refused). The misses
are the small model's: about one answer in six omits citations, and it sometimes refuses
or hedges when the sources do contain the answer (a007, a009, a025, a049). The one restricted miss
(r012) is a refusal that names the topic "Disciplinary Procedure", which the title-match metric counts;
the forbidden text was never shown to the model. A larger model (OpenAI, Azure OpenAI) is expected
to improve citation behaviour; re-run this after changing the model, the prompt or the context
format, and record the numbers. Retrieval and ACL safety stay CI-gated.

## Running it

```powershell
python -m leyline.evals --seed                                   # keyword search
python -m leyline.evals --index hybrid --embeddings evals/embeddings.json --seed --reindex --check evals/thresholds.json
python -m leyline.evals --verbose                                # list every failing case
python -m leyline.evals --index hybrid --sweep                   # grid of relevance floors
```

Exit status: `0` success; `1` an ACL leak or a threshold regression; `2` environment
problem (no database, a stale embedding cache, indexing failures, a golden-set error).
The evaluation is strict: if the embedding service fails it stops rather than silently
measuring keyword search, and it waits for the ingestion queue to be idle first so it never
measures a half-indexed database. Results are deterministic (ties break by title and chunk
position, not random ids).

## Offline hybrid evaluation (record and replay)

A real model cannot run in CI. Its vectors for the corpus chunks and the golden questions
are **recorded once** into [`evals/embeddings.json`](../evals/embeddings.json) (about
900 KB, keyed by the exact text embedded including task prefixes) and **replayed** by CI.
Hybrid quality is then measured on the real model, reproducibly, with no model or network.

Re-record whenever the corpus, the chunker, the golden questions, the model or a prefix
changes. Replay fails loudly (with these instructions) instead of measuring something else:

```powershell
# ollama serve   (and: ollama pull nomic-embed-text)
$env:EMBEDDING_BASE_URL = "http://localhost:11434/v1"
$env:EMBEDDING_MODEL = "nomic-embed-text"
$env:EMBEDDING_QUERY_PREFIX = "search_query: "
$env:EMBEDDING_DOCUMENT_PREFIX = "search_document: "
python -m leyline.evals --index hybrid --seed --reindex --record-embeddings evals/embeddings.json
```

## Calibrating relevance floors for a different model

Cosine similarity has a different scale for every model, so the shipped floors
(`SEARCH_MIN_SIMILARITY=0.58`, `SEARCH_MIN_LEXICAL=0.5`) are calibrated for `nomic-embed-text`
**with its task prefixes**. After changing the model or a prefix: re-index
(`--reindex`), run `--sweep`, pick a point on a plateau (not at a cliff edge), set the two
environment variables, and update `evals/thresholds.json`. The keyword floor applies only
while the vector leg is running; keyword-only search (no embedder, or the embedding service
down) keeps full keyword recall.

## The CI gate

`tests/test_evals.py` and the *Retrieval evaluation (offline)* CI step enforce
[`evals/thresholds.json`](../evals/thresholds.json) for both indexes. Rules:

- `acl_leaks` must be 0 and `restricted_pass_rate` must be 1.0, always.
- Raise the floors when retrieval improves. **Never lower them to make a change pass**:
  investigate the regression instead.
- Tests also prove the gate itself works: with the search's permission filter deliberately
  disabled, the evaluation reports leaks and fails.
- Another `SearchIndex` implementation (Azure AI Search in Phase 7) is added to the
  contract suite in `tests/test_search_contract.py` and measured with the same harness.
