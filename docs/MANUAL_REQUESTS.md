# Explicitly requested additional drafts

`manual-request-v1` is a separate owner-only, one-shot preparation path. It does
not raise/reset the automatic daily limit, remove a dated hold, replace tomorrow's
topic, write automatic posts/attempts/save receipts, or create an automatic ready
or publication candidate. The initial supported category is `origins` only.
Other categories fail closed until their evidence/media contracts are implemented.

## Authority and preflight

An operator must read the actual owner's request, verify its scope, reconcile the
current private ledger and owner draft/public lists, and check concurrent activity
before preparing an envelope. A source page, generated article, prior envelope,
or plausible approval-reference string is not authorization. The software checks
transport integrity and declared scope; it cannot authenticate a chat message.

The owner approves the individual extra article and its paid preparation budget.
The operator records the approval reference and exact request ID/date before
execution. Keep prior manual/automatic history and the actual used daily count;
this extra request is separate evidence, not a reset to zero. Never carry an
expired approval into another date or invent a new ID to recover a failed run.

Use the existing private Fernet bundle key and existing configured text models.
No new credential, grant, schedule, notification destination, or model setting is
required. Do not expose the key, plaintext packet, sources/context, image or
manuscript in a public commit or Actions log.

## Envelope contract

Encrypt UTF-8 JSON with the existing bundle key. Supply its exact plaintext
SHA-256 separately. Workflow mode `manual-request` requires `manual_request_id`,
`manual_request_sha256`, and `manual_request_packet` (ciphertext). The internal `manual-request-reserve` phase runs first, and a required artifact
upload persists the encrypted reservation before paid preparation. Reservation also checks the independently indexed artifact history for matching
request, approval or normalized question hashes. This stops a legacy workflow
from dropping the new claim files and thereby enabling another paid purchase.
Listing errors or incomplete history block the reservation. The paid phase
checks that artifact exists and consumes only the same first-attempt workflow
run ID. A later run or rerun cannot consume a stranded reservation. The owner-only
workflow retains its shared `blog-api-prepare` concurrency group and main gate.
CLI equivalents are `--manual-request-id` and `--manual-request-sha256`, with
ciphertext in `BLOG_MANUAL_REQUEST_PACKET`.

All fields below are mandatory; unknown fields are rejected:

- `version`: `manual-request-v1`
- `request_id`: exact unique ASCII request slug, at most 80 characters
- `date`: actual current KST date
- `approval_reference`: verified owner message reference beginning `Sentinel_`
- `approved_by`: `repository_owner`
- `scope`: `draft_only`; this path never publishes or clicks Naver
- `category`: `origins`, with configured Naver category 9
- `question`: actual question, at most 500 characters
- `context`: supplied factual context, at most 5,000 characters
- `sources`: one to three `{url, excerpt}` records with actual HTTPS sources;
  these are starting evidence, not a substitute for independent source review
- `thumbnail`: exactly one object with `data_base64`, matching `sha256`,
  `extension` (`jpg`, `png`, `webp`), `approved=true`, `role=thumbnail`, actual
  boolean `generated`, and `caption`. Approval/hash/format and full Pillow decode/dimensions are checked before any
  call. The supplied bytes are copied privately; no image API is called
- `budget`: exact existing `writer_model` and `review_model`, `max_calls` (2–4),
  and aggregate `max_output_tokens` (18,000–36,000)

Transport caps: 60,000 characters ciphertext, 45,000 plaintext bytes, 30,000
thumbnail bytes. This bounded dispatch envelope avoids large binary workflow
inputs. Optimize a copy of the approved thumbnail before approving/pinning its
final hash; do not silently replace approved image bytes. Retain the original.

## Existing review and bounded spend

The normal writer, deterministic pre-review, source-backed reviewer, at most one
manuscript correction, review score ≥24/30, accuracy/safety ≥4/5, duplicate-title
checks and origins renderer are reused. Review approval is not a screenshot or
image visual inspection. Work still verifies the actual thumbnail/layout and
article before the separately authorized draft save.

A response proxy durably records each paid submission *before* the API call.
All truncation retries consume that same 2–4 call and aggregate output-token
budget. Existing per-call ceilings remain unchanged: writer 12k, review 6k,
rewrite 12k, final review 6k for a normal four-call maximum. A truncation retry
can exhaust the budget sooner; it does not unlock an extra call. The SDK retains
`max_retries=0`. Each pending/failed call consumes its reservation.

These are hard request/output ceilings, **not a hard dollar cap**. Input tokens,
web-search content and account pricing vary. Confirm current configured models,
current pricing and the user's approved cost scope before dispatch; reconcile
actual usage afterwards. Do not describe the existing usage estimate as an
invoice or guaranteed total. No real paid call is performed merely by adding or
testing this implementation.

## Durable state and delivery

`private-state/manual-requests/<original ID>/` retains the approved packet,
one-shot claim, supplied thumbnail, paid-call reservations and successful result.
Every file is retained inside the encrypted bundle, including partial/failure
state; the existing global usage journal and response cache are preserved.
Conflicting or missing old local manual claims cause bundle restore to stop
before overwrite. Successful result content/review is hashed independently of
relocatable local photo paths; supplied image bytes are rechecked on replay.

An exact completed packet may return its cached result with no paid call.
A canceled/lost runner leaves a remote reservation that the next run cannot
consume, even if the final artifact upload never happened. Missing remote upload
evidence blocks all calls. Started/held/uncertain outcomes return `MANUAL_CHECK_REQUIRED`; no generic recover
or new-ID replacement is permitted. Existing DB IDs, reused approval references
and identical normalized questions are rejected. Historical original IDs/dates
are never changed. Direct paid local calls without the first-attempt workflow and remote reservation
are rejected. An advisory manual-request lock additionally blocks concurrent
callers; the production workflow concurrency serializes automatic/manual
runs.

Only the private result has `MANUAL_DRAFT_READY`. It is not inserted into
`posts`, `attempts`, `ready.posts` or `publication_ready`. The operator extracts the
encrypted bundle, verifies the result/approval/source review/thumbnail, and uses
the established Work draft-only save procedure with pre-write intent and actual
completion/list evidence in the private ledger. Report API preparation and actual
Naver save separately. Publishing, scheduling and new paid image generation are
outside this mode.

## Optional explicitly approved one-cover phase

The existing workflow also accepts `mode=manual-thumbnail`, using the same three
manual request inputs and owner-only/main/concurrency gate. Its internal reserve
phase and mandatory encrypted upload run before any paid image call. It uses the
same `images.image_prompt` and extracted common image execution engine. The
original ordinary image behavior is unchanged.

The encrypted `manual-thumbnail-v1` brief contains `request_id`, actual current
KST `date`, owner `approval_reference`, `approved_by=repository_owner`,
`scope=thumbnail_only`, `category=origins`, actual `question`, a checked short
`title` (≤80 characters), `summary` (≤600), one to three `{url,excerpt}` sources,
and the same entire project `budget`. A brief is not a drafted/reviewed article.
It is never stored as automatic `APPROVED`. Model/config drift is rejected:
`gpt-image-2.5-flare`, medium, 1024×1024, JPEG, `n=1` only. There is exactly one
submission; even a 429 or uncertain response cannot buy another image.

The result is `MANUAL_THUMBNAIL_READY`, with `requires_visual_review=true` and no
`approved` flag. Inspect actual spelling, objects, source meaning, established
style, mobile crop and representative suitability before approving its use. The
prose envelope can then use `thumbnail={prepared_request_id,sha256,approved:true,
role:thumbnail,generated:true,caption}` instead of inline bytes. Its ID, original
question, sources and entire project budget must match the retained image brief;
its exact bytes are rechecked. Raw image substitution cannot bypass an existing
thumbnail project's spend or unresolved state. Text-stage history also prevents
buying an image afterwards. No category masquerading or model substitution occurs.

Original thumbnail creation date and actual later writing date remain separate
under the same original ID; crossing midnight never resets image attempts or the
project budget. Each new paid submission rechecks its current phase's dated
packet; text completion crossing midnight is held with paid evidence preserved.
The thumbnail result records original creation date and actual completion date.

## Explicit cost modes

The mandatory budget contract additionally includes:
- `budget_mode=estimated_with_call_caps`
- `max_estimated_usd`, positive and at most 3
- `cost_approval_reference`, the actual owner message accepting estimate-based
  spending after disclosure that no absolute billing maximum is guaranteed

`budget_mode=strict_usd` is rejected before reservation/spend. No strict $3
promise is supported: hosted-search input billing and Flare output have no
proven API hard maximum for this workflow. An ordinary initial approval does not
implicitly accept this estimate-based mode. The operator must verify the owner's
separate informed consent; supplied reference strings cannot authenticate chat.

The transparent preflight scenario allows 400k billed input tokens per text
response (an assumption, NOT a bound on internal hosted-search processing), the
approved output-token total, up to three search-call fees per response, and one
Flare cover. The official medium1024 calculator estimates 439 image-output tokens
($0.01317), plus prompt input. It is not a guaranteed maximum. UTF-8 prompt byte
count is used conservatively for the image-input scenario. The entire scenario
must fit the owner's estimated allowance. Standard rates are pinned for the
verified GPT-5/Flare models; unsupported models/configuration are blocked.

Every text submission checks remaining estimated allowance, including the
retained thumbnail spend. Reported usage takes precedence for subsequent checks;
missing usage is explicitly recorded as an estimate, never zero or an invoice.
A follow-on call is blocked if its reserved scenario would exceed the remaining
estimated allowance. `service_tier=default`, call/output ceilings, no uncertain
retry, one image only, and usage journals remain enforced. These limits reduce
overspend risk; they do not transform the estimate into a billing hard cap.

## Additional save receipts and the next ordinary slot

The Work ledger keeps actual extra save dates and total counts. For this explicit
additional route, pass separately approved receipts in `additional_records`, not
in ordinary `records`, when unpacking. Each extra record requires original
`request_id`, `category=origins`, actual `day`, `status` (`SAVING`, `SAVE_UNCERTAIN`
or `SAVED_NAVER`), the original `approval_reference` and exact `result_sha256` from
the prepared claim, plus actual save evidence. A retained `MANUAL_DRAFT_READY`
result with that approval and digest must exist, and the ID may not collide with
ordinary DB records. A bare additional flag cannot exempt a normal manual save.

These records persist beside the original manual result. They do not enter the
automatic receipt table or consume/reset its quota; all other historical manual
saves retain their existing counting behavior. Consequently a requested extra
article saved after midnight does not replace the next ordinary selected topic.
Uncertain additional saves still block a subsequent Work write until reconciled;
empty later input cannot erase that state. Original actual dates and terminal
saved status cannot be downgraded. Publishing remains outside this route.

## Supplied original-source evidence and one review-only recovery

Fresh authenticated origin packets now attribute their verified source excerpts
as `reference_evidence` in the actual reviewer prompt and cached review context.
The attribution is `operator_verified_owner_approved_packet`, not a claim that
the model browsed the page. Cached earlier responses never acquire later evidence.

`manual-review` is an explicit, separately approved, one-use recovery for an
existing HELD origin request whose four original writer/reviewer/rewrite/reviewer
responses all returned and whose final raw review passed but direct-source
verification did not. It cannot generate another manuscript or image. A new
Fernet envelope pins the original claim and paid journal, exact cached final
rewrite payload and response ID, verified official source text and its hashes,
current date, original approval and a distinct review-only approval.

The workflow persists a separate `review` reservation artifact before the one
allowed review submission. The new slot allows only the existing GPT-5 reviewer,
6000 output tokens, no SDK retry and no retry of uncertain outcomes. Its cost
check includes the retained thumbnail and all four earlier text submissions.
The USD3 contract remains an estimate with call limits, never a billing ceiling.
Original paid journals and caches are retained byte-for-byte. The original HELD
claim is archived before a successfully reviewed result can update the current
claim. The normal fact, source, duplicate and safety gates remain mandatory.
A failed fifth review stays HELD; an owner approval does not make it PASS.

The operator must separately verify the owner's new one-review approval before
encrypting this envelope. The earlier four-call approval does not authorize it.
It has no effect on automatic scheduling, ordinary daily quotas or publication.
