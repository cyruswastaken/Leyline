# Retrieval evaluation

Retrieval quality and permission safety are measured, not assumed. A fixed set of
questions is run against the same `SearchIndex` interface the chat pipeline uses,
and CI fails on a quality regression or on **any** ACL leak.

## What is measured

The golden set is [`evals/golden.jsonl`](../evals/golden.jsonl): 126 questions, asked as one of
four personas that mirror the demo users (`employee`, `security`, `hr`, `finance`). The first 78
were written in Phase 3; 48 more (ids `a101`+, `r101`+, `u101`+) were added on 2026-11-24, written from
the corpus before anything was measured, so the failures they show are kept.

| Kind | Count | Correct behaviour |
| --- | --- | --- |
| `answerable` | 78 | A document the asker may read answers it; it should rank high (24 added: public documents beside restricted ones on the same subject, and readers who may see the restricted one) |
| `restricted` | 24 | The real answer is in a document the asker may **not** read; it must never be returned |
| `unanswerable` | 12 + 12 | Nothing in the knowledge base answers it; the right result is *no hits*. The 12 added are tier `near`: close to a covered topic ("How many sick days do I get?") |

Results on the 126-case set (2026-11-24): local model (nomic) recall@1 0.910, recall@3 0.987, recall@5 1.000,
MRR 0.949, standard refusal 0.833, **near-topic refusal 0.083**, 0 ACL leaks. Azure (text-embedding-3-small):
recall@1 0.962, recall@5 1.000, MRR 0.979, standard refusal 1.000, **near-topic refusal 0.417**, 0 ACL leaks.
Refusing questions next to covered topics is the known weak spot (floors in `evals/thresholds*.json` pin the
baseline); [decision 0011](adr/0011-no-cross-encoder-reranker-for-now.md) records the reranker that did not fix it.

Questions are natural paraphrases on purpose ("Someone stole my backpack with my work
laptop inside" for a *lost or stolen device* document). Do not tune them to the
document wording: that would hide exactly the weakness semantic search exists to fix.

| Metric | Meaning |
| --- | --- |
| `recall@1/3/5` | Answerable: an acceptable document is in the top k (documents, not chunks) |
| `mrr` | Answerable: mean reciprocal rank of the first acceptable document |
| `refusal_rate` | Unanswerable: share where nothing was returned |
| `near_refusal_rate` | Unanswerable, tier `near`: the same, scored on its own so the hard cases do not move the standard rate |
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

Result with `gpt-5.4-mini` (Azure OpenAI) and `text-embedding-3-small` (768 dimensions, no prefixes), hybrid
retrieval with the Azure floors (0.37 / 0.60), the same 78 questions, 2026-11-19, no model errors:

| answered | cites a source | cites expected doc | figures supported | refused | restricted pass | ACL leaks |
| --- | --- | --- | --- | --- | --- | --- |
| 0.963 | 1.000 | 1.000 | 0.981 | 1.000 | 1.000 | 0 |

How to read it against the 3B run: every answer cites a source and a correct one (0.833 and 0.778 before),
every unanswerable question is refused, the restricted questions all pass, and nothing forbidden reached the
model. Two answerable questions were refused (a024, client-meal limit; a046, network segmentation); in both the
model said the documents do not give the detail asked for, which is the cautious reading, not an invented
answer.

"Figures supported" first scored 0.870 on this model. That was a flaw in the metric, not invented numbers: it
compared digits only, the corpus spells numbers as words ("fifty gigabytes", "twenty-five days"), and this model
writes digits. All seven flagged answers (a001, a010, a011, a018, a020, a025, a031) stated a figure that is in the
cited document as a word; the 3B model copied the words, so it scored higher. The check now treats a figure
written as a word in the sources as support for the same digits, and counts words for ten and above in an answer
as figures, so spelling out an invented number does not hide it (tests in `tests/test_answer_evals.py`). With
that, a new run of the same 78 questions scores 0.981, level with the 3B model's figure (which was measured
before the fix and has not been re-scored).

The one remaining case (a051) repeated the figure from the user's own question ("fifteen thousand"); the
source only says "up to twenty thousand". The metric compared with the sources alone, so a figure the user
stated and the answer repeated counted as unsupported. It was not an invented number. The check now also treats
the question's own figures as known (`tests/test_answer_evals.py`); a figure in neither the sources nor the
question is still flagged. Re-run on 2026-11-24 with the same model and corpus: answered 0.963, cites a source
1.000, cites an expected document 1.000, **figures supported 1.000**, refused 1.000, restricted pass 1.000, 0 ACL
leaks. Only the metric changed; the answers are as before.

## Follow-up questions

A follow-up such as "and on a Mac?" needs context, but asking the chat model to rewrite every follow-up costs a
model round trip (about a second) before retrieval can start. `services/rewrite_policy.py` decides without a model
call: a message with a reference word ("it", "that", "they", "same", "again"...), a continuation opener ("and",
"what about", "why"), or fewer than four words is rewritten; anything else is searched as typed. It only reads the
question text, so it cannot widen what anyone may read. `REWRITE_MODE=always` restores rewriting every follow-up.

`evals/followups.jsonl` has 30 two-turn conversations written from the corpus (28 answerable, 2 restricted,
4 personas), and `python -m leyline.evals --followups` compares three ways of searching them. "Ideal rewrite" is the
author's hand-written standalone question, so it is a ceiling and says nothing about how good the real model's
rewrites are.

| Policy (hybrid, nomic-embed-text) | recall@5 | MRR |
| --- | --- | --- |
| Message as typed | 0.929 | 0.833 |
| Ideal rewrite for every follow-up | 0.964 | 0.866 |
| **Shipped rule** (rewrite only when needed) | **0.964** | **0.884** |

The rule skips 40% of rewrites, matches the ideal rewrite's recall, leaks nothing (0 ACL leaks, restricted pass
1.000) and agrees with the author's own label on 29 of 30 questions. Its one miss (f008, "Where do they park?")
fails with the ideal rewrite too. Floors are in `evals/thresholds.json` (`followups`) and enforced by
`tests/test_evals.py`. Limits: 30 cases is a small sample, the questions are mine, and this runs on the nomic
recording only (the Azure embedding recording has no follow-up queries).

The run was made against a throwaway local database (`leyline_evals`, loaded with the Azure embedding model)
and the Azure chat deployment, so it measures the model and the prompt, not the deployed database.

## Answer quality in CI (recorded answers)

A live model cannot run in CI, but everything Leyline does around it can. `evals/answers.azure.json` holds
gpt-5.4-mini's raw replies to the golden questions (98 answers, recorded 2026-11-24 with
`python -m leyline.evals.answers --record-answers evals/answers.azure.json`), keyed by a hash of the exact
prompt. `tests/test_answers_replay.py` replays them through today's retrieval (with the recorded Azure
embeddings and floors), context, prompt, redaction and citation checks and grades the result against
`evals/thresholds.answers.azure.json`.

| Metric (recorded run) | Value | Floor |
| --- | --- | --- |
| answered (answerable) | 0.949 | 0.90 |
| cites a source / an expected document | 1.000 / 1.000 | 0.97 |
| figures supported by sources | 1.000 | 0.97 |
| citations grounded in their source | 0.962 | 0.92 |
| refused (unanswerable) | 0.917 | 0.88 |
| restricted pass | 1.000 | 1.00 |
| ACL leaks | 0 | 0 |

"Grounded" is deterministic, no model: every sentence with a citation must share at least a quarter of its
content words with the source it cites. A changed prompt, context builder, chunker or retrieval changes the
prompt text, so replay stops with "no recorded answer" and tells you to re-record, rather than grading something
else. It guards Leyline's own handling of an answer; it says nothing about another model, which still needs a live
run (about 100 model calls).

## Reranking, the title in the embedding, and what was not adopted

Two retrieval ideas were built and measured on 2026-11-24 and **not adopted**; both stay available and off.
See [0011](adr/0011-no-cross-encoder-reranker-for-now.md) (a `bge-reranker-base` cross-encoder after search: lower
recall, and no score floor that refuses more without dropping correct answers) and
[0012](adr/0012-keep-the-document-title-out-of-the-embedding-text.md) (the document title in front of each chunk's
embedded text: recall@5 1.000 to 0.987). To measure a reranker again:
`docker compose -f compose.yaml -f compose.rerank.yaml up -d rerank`, then
`python -m leyline.evals --index hybrid --embeddings evals/embeddings.json --rerank-url http://127.0.0.1:8081`.

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

## The Azure model (text-embedding-3-small)

The Azure deployment embeds with `text-embedding-3-small` at 768 dimensions and **no task prefixes**.
Its similarity scale differs from `nomic-embed-text`, so it has its own recording
(`evals/embeddings.azure.json`), its own floors in `infra/modules/apps.bicep`
(`SEARCH_MIN_SIMILARITY=0.37`, `SEARCH_MIN_LEXICAL=0.60`) and its own gate
(`evals/thresholds.azure.json`, `tests/test_evals_azure.py`, which reads the floors from the Bicep
file so the gate cannot drift from what is deployed).

| Floors (78 cases) | recall@1 | recall@3 | recall@5 | MRR | refusal | ACL leaks |
| --- | --- | --- | --- | --- | --- | --- |
| nomic floors (0.58 / 0.5) on the Azure model | 0.426 | 0.426 | 0.426 | 0.426 | 1.000 | 0 |
| Azure floors (0.37 / 0.60) | 0.963 | 1.000 | 1.000 | 0.978 | 1.000 | 0 |

With the nomic floors, chat answered "I couldn't find relevant information" to nearly everything on
Azure; the sweep showed a plateau at similarity 0.35-0.40 (0.33 lets unanswerable questions through,
0.43 loses a document) and 0.37 is its middle.

Re-record and re-sweep (from a folder **without** a `.env`, see the pitfall below):

```powershell
# the key is read from Azure for this command only; do not paste or save it
$env:EMBEDDING_API_KEY = az cognitiveservices account keys list -g rg-leyline -n leyline-openai --query key1 -o tsv
$env:EMBEDDING_BASE_URL = "https://leyline-openai.openai.azure.com/openai/v1/"
$env:EMBEDDING_MODEL = "text-embedding-3-small"; $env:EMBEDDING_SEND_DIMENSIONS = "true"
python -m leyline.evals --index hybrid --golden <repo>\evals\golden.jsonl --seed --reindex --record-embeddings <repo>\evals\embeddings.azure.json
python -m leyline.evals --index hybrid --golden <repo>\evals\golden.jsonl --embeddings <repo>\evals\embeddings.azure.json --sweep
```

**Pitfall (cost a wrong first recording):** a local `.env` that sets the nomic prefixes
(`EMBEDDING_*_PREFIX`) is read by the app even when you clear the variables in your shell, and PowerShell
treats `$env:X = ""` as *unset*. A recording made that way embeds prefixed text the deployment never sends,
and replay then fails with `MissingEmbeddingError`. Run the commands from a directory with no `.env`
(point `CORPUS_DIR` at the repo's `corpus`) and check `Settings().embedding_document_prefix` is empty first.
Use a throwaway database (`CREATE DATABASE leyline_evals`, then `python -m leyline.db upgrade`), not your
development one.

Known miss: `u005` ("Can I bring my dog to work?") is now refused; no unanswerable case slips through.

## The CI gate

`tests/test_evals.py` and the *Retrieval evaluation (offline)* CI step enforce
[`evals/thresholds.json`](../evals/thresholds.json) for both indexes. Rules:

- `acl_leaks` must be 0 and `restricted_pass_rate` must be 1.0, always.
- Raise the floors when retrieval improves. **Never lower them to make a change pass**:
  investigate the regression instead.
- Tests also prove the gate itself works: with the search's permission filter deliberately
  disabled, the evaluation reports leaks and fails.
- Another `SearchIndex` implementation (Azure AI Search in Phase 8) is added to the
  contract suite in `tests/test_search_contract.py` and measured with the same harness.
