# 0001. Authorize documents in the database query and again in code

**Status:** Accepted (Phases 2 and 3)

## Context
Leyline answers questions from documents with different audiences (HR, finance, security, everyone).
Text a person may not read must never reach the model, because the model could repeat it. Filtering after
retrieval, or relying on the prompt to hold something back, leaves a window where restricted text is already
in the model's context.

## Decision
- A document's audience is one table, `document_acl`, and the only readers of it are `readable_by` (SQL) and
  `can_read` (Python), which a test keeps equal.
- Search takes the caller's `Principal` as an argument and applies `readable_by` **inside the query**, so
  unauthorized chunks are never selected. Results are checked again with `authorize_hits` before use.
- App roles (even `kb_admin`) never grant read access; groups do.
- Authorization happens before chunking, retrieval and prompt building. Retrieved text is delimited as
  untrusted; the model has no tools; its output is redacted and its citations verified.

## Consequences
- Two implementations of one rule must stay in step, so a test compares them.
- Permission changes on a document apply immediately; a person's own groups are captured at sign-in and lag
  until the session ends (12 hours at most). This is a documented gap.
- Search cannot be swapped for a service that cannot express the ACL filter (see 0002).

## Alternatives considered
Filter results after retrieval (leaks into the model's context); a per-user index (cost and staleness);
trusting the prompt to refuse (not a control).

## Evidence
The evaluation gate requires `acl_leaks` = 0 and `restricted_pass_rate` = 1.0 on every run, including the
Azure model's recording; a test with the filter deliberately disabled proves the gate fails; the red-team
tests and `tests/test_regressions.py`.
