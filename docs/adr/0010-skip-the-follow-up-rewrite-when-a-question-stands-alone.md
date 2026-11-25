# 0010. Skip the follow-up rewrite when a question stands alone

**Status:** Accepted (Phase 9).

## Context
A follow-up in a conversation was always rewritten into a standalone search query by the chat model before
retrieval could start. That is a full model round trip (about a second) in front of every follow-up, and it was
the main reason follow-ups were slower than new questions in the real-model load test (first token p95 3.8 s
against a 2.5 s target).

## Decision
- `services/rewrite_policy.py` decides without a model call. A message is rewritten if it has a reference word
  ("it", "that", "they", "same", "again"...), starts like a continuation ("and", "what about", "why"), or has
  fewer than four words. Anything else is searched as typed.
- The check reads only the question text, never earlier answers or documents, so it cannot widen what anyone may
  read. When in doubt it rewrites: a wrong "rewrite" is only slower, never less correct.
- `REWRITE_MODE=always` restores the old behaviour. The `RETRIEVAL` event records `rewrite_skipped`.
- Lowering `reasoning_effort` was tried first and gave no first-token gain, so no such setting was added.

## Consequences
- Follow-ups that stand alone no longer wait for a model call. Follow-ups that depend on earlier questions still do.
- The rule is a heuristic with a hand-written word list; it is measured, not proven (see Evidence).

## Alternatives considered
Always rewrite (slower, the status quo); never rewrite (loses "and on a Mac?"); ask the model whether to rewrite
(the same round trip it is meant to save); per-kind latency targets (weakens the claim without fixing anything).

## Evidence
30 hand-written two-turn conversations (`evals/followups.jsonl`), hybrid index: searching the message as typed
gives recall@5 0.929, an ideal rewrite 0.964, the shipped rule 0.964 while skipping 40% of rewrites, with 0 ACL
leaks. Real-model load test, 8 users: first token p95 3.8 s to 3.0 s, p50 2.8 s to 2.2 s. The ideal rewrite is the
author's, not the model's, so this does not measure the model's own rewrites.
