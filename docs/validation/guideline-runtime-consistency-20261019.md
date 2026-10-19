# 지침·실행 정합성 검증 — 2026-10-19

기준 main: `e5bce608e226f1cceb55e277493f24b47e3c9ff3`. 시작 시 열린 PR0건. PR #50·#51은 병합 완료였으므로 재적용하지 않았다. GitHub 조회와 전체 트리 SHA `4dde5409525bdda3cafefb56918f1e615e76302a`로 로컬 기준 일치를 확인했다.

## 충돌과 수정

| 항목 | 변경 결과 | 주요 파일 |
|---|---|---|
| 태그 | 모든 유형 최소5·최대8. 주제 선정 전 실제 트렌드, 선정 후 해당 키워드의 네이버 상위3~5개 본문·태그 대조. 네이티브 등록과 본문 목록 분리 | editorial.py, llm.py, pre_review.py, writer/reviewer, templates5종, TEMPLATES/RESET_DESIGN 등 |
| 태그 국소 보완 | 표기·공백·대소문자 중복·번호 태그를 무료 정리하고 8개 상한 유지. 5개 미만은 운영 참고. 무관·동의어 관련성은 문맥 검수 대상으로 남김 | normalize_tags, tag_hints |
| 직접 발행 | owner_only 설정. CLI는 DB 접근 전 거부, core도 트랜잭션 전 거부. publication_ready 생성 금지·과거 후보 제거. 수동 발행 관측/증빙 import 유지 | config, cli, core, cloud |
| 과거 편성·문체 | 정적 writer/reviewer 및 origins PLAN에서 과거 요일 설명 제거. 네 경로 공통 VOICE의 도치/강조/유머 횟수 폐기 | writer/reviewer, editorial, origins, VOICE |
| 지정 편성 | 10/19 달나라·CXMT, 10/20 염력·곰돌이 기존 ID 각2개를 예약으로 보존. 기본 주제 생성/재등록을 차단하고 기존 원고로 인계 | config, planning, inputs, pipeline |
| 썸네일 | 답은 비공개 사실검증 맥락으로 보존하되 시각·문구·소품에서 정답/반전 노출 금지. 수동 썸네일은 질문을 사용 | images, manual_thumbnail, RESET_DESIGN |
| AI 영상 예외 | 완성 영상/동일 영상 실제 프레임만 대표로 사용. 생성 썸네일 API 대체를 보류하며 다른 카테고리에 확장하지 않음 | images, editorial, ai-video.html |
| scaled10 | 직접 발행 모드에서 첫 원고 발행 전 둘째 입력·준비·인계 허용. 편집·저장 직렬, 불확실 상태·일/주 제한 유지 | inputs, cloud, RESET_OPERATIONS |

기존 캐시/해시·비용 상한·호출 횟수·출처 검증·저장 영수증·미확정 제출 처리·Work의 API 자동실행 제한은 유지했다. 기존 캐시를 삭제하거나 새 정책으로 소급 재작성하지 않았다. 새 입력에 따른 캐시 키 차이를 기존 파일 초기화/예산 초기화로 처리하지 않는다.

## 검증 결과

- 관련 회귀: `pytest -q tests/test_public_copy_guidance.py tests/test_parenting_prompt_routing.py tests/test_manual_thumbnail.py tests/test_topic_quality.py tests/test_prepare_failure.py` → 190 passed.
- 전체: `pytest -q` → 1,529 passed (35.52초).
- 정적 검사: `ruff check src tests` 통과. `git diff --check` 통과.
- 오프라인 실제 입력 조립: 아래8유형×writer/reviewer/rewrite/pre_review_correction=32개 최종 문자열을 호출 직전에 캡처했다. 모두 최신 태그 기준 포함, 과거 요일 설명·문체 횟수·보통3~5개 지시 없음. 실제 API/검색 호출은 모의 I/O로 대체했으며 유료 실행0회다.
- 태그4개 미충족, 5개 충족, 6~8개 허용, 9개 입력은 기존8개 상한 적용. 중복/번호는 유효 개수에 포함되지 않으며 본문 직접 표기 없는 후보는 관련성 확인 대상으로 남긴다. 이 관찰은 의미적 관련성을 자동 증명하지 않는다.
- 태그 부족/중복 후보의 pre-review는 보완 유료 호출 없이 통과하고, 부족 범위를 숨기지 않는다. 핵심 출처/정확성/안전 기준은 낮추지 않았다.
- bootstrap5/scaled10와 저장/승인/불확실 상태에서 선점을 반복 거부한 전후 DB 전체 덤프·ready.json 바이트가 동일하다. CLI는 PUBLICATION_DISABLED/owner_only, 종료코드1이며 DB를 열지 않는다.
- 사용자의 수동 발행 증빙 import는 멱등이며 PUBLISHED URL/글ID/시각 관측을 유지한다. 에이전트 선점 시각은 생기지 않는다.
- 과거 자동 발행 회귀는 `archived_automatic_profile`에서만 실행한다. 성공 기대 테스트에 archived 이름/fixture를 붙여 현재 기준과 분리했다. 해당 fixture는 테스트용 별도 TOML만 작성한다.
- scaled10에서 첫 원고 SAVED_NAVER 후 둘째 입력/인계 가능, 동일ID 재등록 거부, 불확실 저장은 계속 차단됨을 실행 검증했다. scaled10은 현재 비활성이므로 기존 제한을 현재 운영 장애로 단정하지 않는다.

### 최종 조립 입력 지문

| 유형 | 경로 | 길이(문자) | SHA-256 |
|---|---|---:|---|
| parenting/article/article/default | writer | 22270 | `ef005746e9a772c658d254aa0b66a9ba652e9217318b9cd3dfae54479616e80d` |
| parenting/article/article/default | reviewer | 21251 | `b78105647d165b8930917358631765e05816dfd025b855ec6d46488b57185b69` |
| parenting/article/article/default | rewrite | 23136 | `99b96c71dce8148ec1d83b8ce65e0c53abc6ce30b3b6d8c9abe52a77d29b1bb9` |
| parenting/article/article/default | pre_review_correction | 22695 | `d067e510ded9341e05447f0161d65959bf9af9c8b80b4aa968f61f0185ebb21a` |
| exercise/article/article/default | writer | 19587 | `96be2adc6445fbe2106631436ebba7d3867d1a35c3fc85eca4df00a934551470` |
| exercise/article/article/default | reviewer | 18579 | `28418fb454637d5ed072d4105e3bf2ad0a2ca66cc26e2a8ca22fcd1cc8309a70` |
| exercise/article/article/default | rewrite | 20464 | `605decaaff35d2fbc9be38897af0c6172c36050ab2f8e9ded7a1119fa0a8c38f` |
| exercise/article/article/default | pre_review_correction | 19996 | `150889bccfcc026abea08bf39cfcb0e680b15c0659d7f2d8863fc5064e04e378` |
| investment/article/article/default | writer | 20737 | `d747a90d713691b1fbb5c5652a0e8504191cbb8d5fe5b075e6a423f42ea65477` |
| investment/article/article/default | reviewer | 19728 | `a4b1a9453afe17f9bf77fbb7aa606641c76af4592234db44028b3fe23322a688` |
| investment/article/article/default | rewrite | 21613 | `459c730fcd3c34884d83d59c8740986db76edb8b26ca2582c14266ac0239815e` |
| investment/article/article/default | pre_review_correction | 21148 | `194cf137d0592ca2deb34d9e7ef2eaf04bca4d79eef6dc888e70b95d71eb4dd6` |
| investment/article/article/life-economics-v1 | writer | 18623 | `f9d92ac111aee993adb4d65b13d47c974d4af36267cf139723eae8c423e87785` |
| investment/article/article/life-economics-v1 | reviewer | 17802 | `6b460435703e1ff4090ea8bf0c562b6cffbbb6ee810519bfe628992c79f7e787` |
| investment/article/article/life-economics-v1 | rewrite | 19609 | `956b2e2fb420ac4f948ec863d70ce80af60c9892e052a86f0e0f0d0ecbbde153` |
| investment/article/article/life-economics-v1 | pre_review_correction | 18987 | `eedd8d8ca1ad0376c1c9dc8ffb56b82ca5694d2d91ae3b39ad97ee6b94869c1b` |
| origins/article/article/default | writer | 7500 | `dbbe540ebb2b1178491a75f4f8b77654fe1bc663bfa05cfb22c78da8b7067589` |
| origins/article/article/default | reviewer | 8484 | `2874dc8b6eaa5a5e993755d67652eab24e2451124456d38f555afbb536bd3619` |
| origins/article/article/default | rewrite | 8413 | `77fff86081339a4b022ee8c0dc47596a031ae8d7038e2a35628481f8325d6f30` |
| origins/article/article/default | pre_review_correction | 7880 | `34cfecfa7814e60aef38b143d01a803ed199e49f7a56358b8e847d6f6328841e` |
| cooking/article/article/default | writer | 19477 | `f2e6ddd02c2a05a32981441eb8cac65ce5ccebe6d9bd4cf4195d297bb6982f60` |
| cooking/article/article/default | reviewer | 18378 | `0d9ad12c9d4d6ed7268996f2c8dd404534a72a0dde4e63b23acd56487d5dfd31` |
| cooking/article/article/default | rewrite | 20263 | `f5986cb72b0b9f9c5b4bc0be13123aad0a00a2e51632951d80a0ebb27edaeed3` |
| cooking/article/article/default | pre_review_correction | 19843 | `f64229b24fca7f11f8639de45670a0cfaf0d917ec1f9932f19b00d1666113316` |
| parenting/ai_tutorial/article/default | writer | 22249 | `7538c6f8e4b1bce1f597f97dfcd5696611bc62f540909f8035aaddda35e33754` |
| parenting/ai_tutorial/article/default | reviewer | 21411 | `d9280459d92616d9f4bab39dec72eb2d277658628209b6c501d6bcc1418f7b2a` |
| parenting/ai_tutorial/article/default | rewrite | 23119 | `7e4d6e63ce46ca4b7a2b3885e39c66341eb16fa03046ae09b864814734134d19` |
| parenting/ai_tutorial/article/default | pre_review_correction | 22673 | `7bc6f499a007430369057604c74003a3dff7292653a9adba0bfca1575915ae73` |
| parenting/article/review/default | writer | 22825 | `441b3193f05385d90cb8db34cfedc57663a5c93a7cb659ff4e08c8442d5fab53` |
| parenting/article/review/default | reviewer | 22244 | `ff8e0011cbb7aad93c065abbb341197090649c68d4093d307929cee8ffc740e3` |
| parenting/article/review/default | rewrite | 23717 | `1de129cebb3d89c8a6015cdeef6a367104f483ba01c1fd033b426e473ddfb879` |
| parenting/article/review/default | pre_review_correction | 23249 | `98d6d3a1d580200dd258fd493336cfe55ae604bf3589a8fa953d317d340f3372` |

## 네이버 실물 확인과 한계

- 클라우드의 써누애비 로그인 상태와 실제 작성기 접근 확인. 임시저장 목록0개. 10/19 07:35 작성중으로 표시된 제목 없는 과거 글감 시험 문서를 자동복원해 읽었다. SK하이닉스·뉴스·쇼핑 시험 카드가 있었으며 지정4원고의 저장 증거는 아니었다. 전체 공개 목록을 전수 확인한 것은 아니다.
- 동영상 추가 업로더와 현재 문서 라이브러리는 실제 열어 확인했다. 완성 영상이 해당 문서에 없어 새 영상 업로드·대표 화면 선택·대표 지정·동일 초안 저장은 하지 않았다. 영상 대표 지원 여부와 실제 적용 결과는 미확인이다. 문서의 절차를 지원/저장 완료로 보고하지 않는다.
- 입력/본문 변경·명시적 저장·발행·예약 변경은0회다. 기존 내용 복원 후 편집기의 자동저장09:14 표시는 관측됐으며, 임시저장 완료나 새 원고 저장으로 집계하지 않는다. 자체 확인 탭은 닫았다.
- 로컬 템플릿5종 수정은 네이버 내 템플릿 갱신/불러오기 완료가 아니다. 실제 네이티브 태그 등록·저장 후 검수·영상 재생/대표 설정도 이번 코드 테스트 완료와 구분한다.

## 자체 검토와 적용 상태

전체 diff를 다시 읽고 태그 숫자 일괄 치환 여부, 과거 프로필의 현재 테스트 혼입, 직접 발행 경계, 캐시/한도 유지, 날짜별 중복 방지를 검토했다. 의미적 태그 관련성/동의어 판정 및 실제 네이티브 등록은 원고 실물 검수에서 확인해야 한다. 코드상 허용되던 자동 발행 경로를 수정한 것이며 실제 자동 발행 발생을 확인한 보고가 아니다.

완료 범위는 로컬 수정·오프라인 검증·커밋·원격 브랜치 업로드·PR 생성·자체 검토다. PR 병합·배포·운영 적용·유료 실행·권한/보안 설정 변경·게시글 발행은 이번 작업에 포함하지 않는다. 최종 커밋/PR/CI는 PR 본문과 작업 완료 응답에 기록한다.
