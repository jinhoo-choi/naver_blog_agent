# Pre-review draft correction

## Pipeline

1. Generate the requested draft once using the existing writer. Keep its completed raw JSON and observed search/open/citation URLs in the private response cache. A malformed field is no longer coerced to text or silently defaulted before checking.
2. Run a local, deterministic pre-review check: required field types, allowed subcategory, KST date, observed source identity, opening summary, heading/length requirements, forbidden inline image markup, existing safety gates, and explicit unsupported personal-experience/child-age markers.
3. Apply only lossless presentation normalization and verified source-identity alignment locally. For example, a citation can use the exact URL that was observed when its equivalent canonical identity is established. Unknown document IDs and unsupported URLs stop here.
4. If only actionable content/schema/structure issues remain, use one bounded writer-model correction. The correction sees the original request, actual observed URLs, original draft and issue codes. It gets no search tools, cannot expand evidence, has a 12,000-output-token ceiling and has no truncation/timeout retry.
5. Run the same deterministic checks again. A failed correction stops with an explicit phase and issue code. There is no correction loop.
6. Submit the checked draft to the existing strict formal reviewer. Existing source-reading checks, score dimensions, thresholds and blocking issues are unchanged. A model correction already used before review consumes the automatic manuscript revision slot; otherwise the formal reviewer can still request one bounded editorial rewrite and re-review.
7. Only formally approved text proceeds to existing images and temporary-save handling.

This is a preflight, not a factual approval. The experience check catches explicit patterns, not every possible fabricated anecdote; the formal reviewer must still enforce source support and the owner's supplied-experience boundary.

## Cost and retry boundaries

- A good draft or a lossless source/format alignment adds **zero model calls** and zero new web-search calls.
- An actionable malformed draft can use **one** pre-review correction call. It replaces the old structural-rewrite path; it is not stacked with a later editorial rewrite.
- Automatic post-review rewriting also uses a single output attempt. The writer and formal reviewer's existing transport/truncation limits are unchanged.
- Daily reservations, generation models, score thresholds, image budgets, schedules and saver behavior are unchanged. A new correction prompt still has a variable token cost; no exact dollar cost is promised.
- Source evidence that cannot be safely reconciled is held without purchasing a correction. Scores are never raised to force a pass.
- New prechecked drafts cannot enter either legacy rejected-draft recovery or legacy approval repair to obtain additional paid revisions. Contradictory legacy-style approvals are held as REPAIR_PENDING with a manual-check result.

## Durable resume

The private response-cache stores a per-KST-day/request pre-review packet, an exclusively created manuscript-revision claim and, for an automatic editorial rewrite, its exact enriched input. The claim is written and fsynced before a paid revision. Concurrent or interrupted runs can only attempt an exact cache-only replay. A missing result, changed input or uncertain call holds the job; it never buys a second revision.

`request_json` continues to key completed responses by day, model, stage, request ID, input/tool arguments and schema. `pre_review_correction` is its own cached stage. The automatic rewrite saves the exact prompt after reference-evidence enrichment so a cache-only restart cannot accidentally reconstruct a different input. The existing encrypted bundle transports all three checkpoint files, preserving limits across cloud/Work relocation.

The writer's completed raw response is reused on recovery, including schema-invalid JSON objects. Refusals, incomplete output and non-JSON output still fail through the existing response gates. No existing API response is promoted to approval merely because it was cached.

## Narrow FDA source equivalence

The existing source-identity whitelist now includes only:

- HTTPS `www.fda.gov`
- `/drugs/understanding-over-counter-medicines/sunscreen-how-help-protect-your-skin-sun`
- The exact query `linkId=100000002918349`

Public inspection on 2026-10-04 established that this query variant declares the query-free article as canonical and og:url and has identical normalized main content. Alignment still requires one equivalent URL to have been actually observed by the draft's source workflow. Other linkId values, article paths, hosts, schemes, extra document parameters, and distinct DART receipts stay distinct. No global query stripping was added.

This proves the narrow public alias, not the encrypted sunlight incident's exact source path, factual claims, reviewer approval, images or Naver save outcome. It does not authorize paid incident recovery.

## Diagnostics and verification

Public progress includes `pre_review_check`, `pre_review_correction`, `pre_review_recheck` and `post_review_recheck`, plus static issue codes. It does not print private draft text, URLs, raw provider error text or personal input.

Offline regressions cover good drafts, exact observed alias alignment, unknown/semantic URL changes, mixed malformed schema and hard stops, supplied-experience boundaries, successful/failed correction, exact cached resume, concurrent claims, encrypted relocation, formal-review ordering and prevention of legacy budget bypasses. Offline tests do not establish live factual quality or successful unattended Naver temporary saving.
