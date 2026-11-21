# Pre-review draft correction

## 현행 작성·평가 계약 — seonoo-production-v7 · 2026-11-18

신규 써누애비 원고는 MOBILE_BLOG_STANDARD.md의 5개 페르소나(문장구조·문체·디자인·주제·독자)로 심사한다. 각100점, 평균85점 이상·각80점 이상·필수검수·7단계·동일최종본확인·미해결0개를 모두 충족해야 품질완료다. 반올림·기본점수로 통과시키지 않는다. 핵심사실·허위경험·자료/캡션·대표·모바일·고지 결함은 점수로 상쇄하지 않는다. 80미만은 실제수정문을 제시하고 미확인은 null로 남긴다.

기존공개글은 수정·삭제·비공개·재발행하지 않고 배드케이스로 보존한다. 후속명시요청만 예외다. 워머4캡처·충주냥이헤더·늦은첫표·늦은뜻풀이·복수질문혼합을 신규검수에서 방지한다. 후보→초안→미디어→5인1차심사·보완→임시저장→PC저장본5인2차심사·보완→모바일심사·보완·최종저장·재열람을 적용한다.

기존6축·29/30·35점·일률표·강제1인칭/아빠결말은 최종집필·품질기준에서 교체한다. 정확성·안전·고지·권한은 보존한다. 일반근거는 하단 기관·자료명·날짜 한 줄씩, 목적 링크는 실제목적지를 검증한다. 아래는 유형별·운영 참고이며 상충채점은 적용하지 않는다. API의 기존6숫자 응답은 기술호환 진단으로만 유지하며 Work의 최종5인 품질완료로 집계하지 않는다. scripts/seonoo-review-check.py로 실제저장본의 근거·평균·필수차단조건을 검사한다. 일정·활성·예산·인증·교류·공개권한을 확대하지 않는다.

## 과거 유형별·운영 참고 (상충 채점은 적용하지 않음)

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


## 실제 평가 입력과 최종 보고 (2026-11-21)

`--review-packet`은 기존 `--blind-input`의 허용 필드에 평가 기준과 PNG/JPG 픽셀을 묶는다. 필수 옵션은 `--snapshot`, `--draft-id`, `--standard-commit`, `--list-140`, `--mobile-360`, `--pc-screen`, `--rubric`, `--official-references`, `--output`이다. 본문 화면은 반복 `--body-screen`으로 추가한다. 기존 출력파일은 덮어쓰지 않으며 모델을 호출하지 않는다.

각 steps.first_review/second_review의 receipt_artifact는 artifacts의 JSON을 가리킨다. 이 JSON은 packet_artifact, response_artifact, transport_artifact를 참조한다. 모든 파일은 artifact-root 내부 상대경로와 SHA256으로 등록한다.

- response: call_id, draft_id, revision, stage, scores, unresolved, criteria. criteria의 5개 축 각각 5항목은 criterion(1~5), score(0~20), reason, evidence(location/observation)를 갖는다.
- transport: call_id, sent_packet_sha256, response_sha256. 2차 격리 방식과 상속 범위는 MOBILE_BLOG_STANDARD.md의 승인된 방식에 맞춰 실제 값으로 기록한다. 실제 호출 경계에서 보존한 원기록을 사용한다.
- `record.json --request-id ID --revision SHA --artifact-root ROOT --standard-commit SHA --review-history history.json --final-report`가 최종 보고 경로다. 같은 revision의 검증된 응답에 숫자 근거가 있으면 `assessment.score`를 보고한다. 역할별 관측점수를 관측 만점으로 나눠 100점으로 환산하고, 관측된 역할과 회차를 각각 같은 비중으로 평균한다. 미확인 항목의 원점수는 null로 보존한다.
- `assessment`에는 실제 채점 항목 수와 예정 50항목 대비 비율, 회차별 점수와 차이, 미확인 사유 및 근거를 함께 남긴다. `possible_score_range`는 채점된 회차에서 빠진 항목이 각각 0점 또는 20점일 때의 평균 범위다. 근거 충족률은 확률이 아니다. 현재 revision의 숫자 근거가 하나도 없을 때만 `assessment`가 null이다.
- 품질 검증이 끝나지 않으면 기존 exit 2 및 final_scores/final_mean=null을 유지한다. 이 필드는 품질 통과에 결속된 기존 인터페이스다. 사용자 보고에선 `assessment.score`와 품질 판정을 함께 제공한다.

이 옵션은 원격 실행기를 만들거나 자동화 설정을 배포하지 않는다. 기존 호출 수단이 없으면 입력 준비와 차단 결과만 보고한다. 테스트의 합성 영수증 통과는 실제 모델 실행이나 콘텐츠 품질 통과 증거가 아니다.
