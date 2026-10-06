# 입력 큐 사용법

현재 API 준비 경로는 비공개 `BLOG_SEED_JSON`의 실제 입력 큐를 사용합니다. 문서나 예시를
고친 것만으로 이 큐가 갱신되지는 않습니다. 실제 자료 등록은 승인된 비공개 입력 경로에서 확인합니다.
아래 `blogbot enqueue` 명령은 비활성 Windows 실행기 대안의 사용법입니다. 이 경로의 입력은
`BLOG_DATA_DIR/inbox`에 보관하며 경로를 바꾸려면 `BLOG_INBOX_DIR`을 설정합니다.
키·아이 정보·원본 사진·실제 입력 JSON을 공개 GitHub에 올리지 않습니다.

2026-10-06 KST부터 [DAILY_PLAN](DAILY_PLAN.md)의 `weekly-4111-v1`에 따라 하루 1편입니다. 월·수·토·일은
육아, 화요일은 이름의 유래, 목요일은 운동, 금요일은 투자입니다. 10월 5일 구계획과 수동 슬롯 이력은 보존합니다. 가족-AI는 실제 새 자료가 있을 때만 선택하며 화·토 의무 편성은 없습니다.
날짜표는 실제 주제 입력을 대신하지 않으며 14개 주제를 자동으로 채우지 않습니다.
당일 카테고리에 맞는 실제 질문·자료가 없으면 `PLANNED_INPUT_REQUIRED`로 보류합니다.

## 육아 질문

`parenting-request.example.json`을 PC의 개인 폴더로 복사합니다.
`enabled`를 true로 바꾸고 실제 질문·고유 id를 입력합니다. 월령을 모르면 null을 유지합니다.
`context`에는 답에 필요한 조건만 적습니다. 과거 대화 전체를 붙여 넣지 않아도 됩니다.
일반 글은 입력 최상위의 `editorial_type="article"`이며 생략하면 같은 값입니다.

```powershell
$env:BLOG_DATA_DIR="C:/blogbot/data"
blogbot enqueue --file "C:/blogbot/incoming/parenting-question.json"
```

월령별 발달, 지루성 두피염, 목튜브 질문처럼 같은 주제를 다른 조건에서 다시 다루려면
새로운 id와 구체적인 질문을 등록합니다. 중복 제목·유사 본문은 이후 검수에서 걸러집니다.
ChatGPT 대화를 자동 감시하는 기능은 없습니다. 이 대화에서 질문을 입력 파일로 정리한 뒤 실행기에 등록하는 방식입니다.

### 선택적 가족-AI 튜토리얼

사용자가 새 영상이나 실제 제작 자료를 제공할 때만 같은 육아 큐에 등록합니다.
화·토를 포함해 어느 요일도 AI 글을 의무로 채우지 않으며 옛 영상·후기를 재활용하지 않습니다.
입력 최상위에 `category="parenting"`, `editorial_type="ai_tutorial"`을 명시합니다.
`ai_tutorial`은 육아에서만 허용하고 새 최상위 카테고리로 쓰지 않습니다. 내부에는
`ContentRequest.data.editorial_type`으로 보존하며 실제 계획은 요청/원고 provenance와
`ready.json.daily_plan`에 전달합니다. 일반 육아 질문을 제목만 보고 AI 글로 바꾸지 않습니다.

`question`에는 실제 질문, `context`에는 실제 도구·설정·입력·확인한 결과와 제공 자료를
적습니다. 미확인 실행·효과·프롬프트를 만들어 넣지 않습니다. 아래는 실행용 글감이 아닌
비활성 형식 예시이며 실제 소유자 입력으로 바꾸기 전에는 등록하지 않습니다.

```json
{
  "enabled": false,
  "id": "replace-with-owner-input-id",
  "category": "parenting",
  "editorial_type": "ai_tutorial",
  "question": "실제 가족-AI 제작 질문을 입력하세요.",
  "age_months": null,
  "context": "제공된 실제 입력·설정·결과·자료만 적으세요.",
  "benchmark_query": ""
}
```

새 날짜별 입력 필드를 요구하지 않습니다. 기존 큐와 승인된 입력에서 당일 유형에 맞는
것만 선택하고 없으면 보류합니다. 하위유형이 맞아도 출처·실물 자료·재사용 권한이 부족하면
심화 기준 통과나 저장 완료로 처리하지 않습니다.

### 기존 수동 초안의 편집일 예약

2026-10-05 육아 슬롯은 사용자가 선택한 기존 수동 초안에 배정되어 있습니다.
새로운 큐 항목·사진 업로드·생성 호출을 만들지 않고 해당 날짜의 자동 추가 작업만 생략합니다.
실제 저장일과 편집 예정일을 분리하며 원래 영수증·과금·입력 이력을 바꾸지 않습니다.
세부 동작과 외부 적용 조건은 [DAILY_PLAN](DAILY_PLAN.md)을 따릅니다.

## 이름의 유래 질문

화요일에는 사용자가 실제로 물어본 이름·단어의 유래를 `category="origins"`로 준비합니다.
질문·검증 가능한 출처와 제공·승인된 썸네일을 확인합니다. 이름의 유래 카테고리 번호 9는 실계정 UI `9_이름의 유래`에서 확인했으며 초안의 실제 선택·저장은 별도 검증합니다.
승인 자료가 없거나 카테고리 연결이 미확인이면 보류하며 임의 질문·사진·번호를 만들지 않습니다.
[ORIGINS_EDITORIAL](ORIGINS_EDITORIAL.md)의 짧은 글 기준을 따르며 `content_style="review"`나
`ai_tutorial`로 바꾸어 후기/심화 문턱을 적용하지 않습니다. 현재 구매 사실·쇼핑커넥트 링크·내돈내산 배너는 추가하지 않습니다.
향후 실제 구매와 제공 경험이 생기면 같은 문서의 조건부 절차로 별도 확인합니다.

일반 질문과 같은 `question`·`context`·`benchmark_query` 필드를 사용합니다. 공개 키워드에 개인 정보를 넣지 않습니다. `photos`는 정확히 한 장이 필요하며 각 항목에 `file`, 선택적 `caption`, `role="thumbnail"`, `approved=true`, 원본의 실제 생성 여부인 `generated`를 둡니다. `enqueue`가 관리 파일을 복사하고 SHA-256을 기록합니다. 썸네일이 없으면 등록·수집에서 유료 준비 전에 `INPUT_REJECTED`로 보류하며 텍스트·이미지 API를 호출하지 않습니다. 복원 시 관리 파일명·해시와 합산 사진 운반 예산도 재검증합니다. 번호 설정이 누락되거나 미확인이면 `CATEGORY_CONFIGURATION_PENDING`으로 유료 호출 전에 보류합니다.

`operating_plan.reservations."2026-10-06"`은 실비김치 초안 저장 확인 뒤 `kind="existing_owner_draft"`로 전환됐습니다. 실제 저장은 10월 5일 19:17 KST이며 10월 6일 예약을 새 저장 영수증으로 만들지 않습니다. UI 표시 `03:17`의 시간대·네이버 고유 draft ID는 미확인으로 보존합니다. 10월 7일 햄버거 AI는 dot가 제안한 미확정 날짜의 `owner_preparation_pending` 홀드이고, 10월 9일은 새 정책용 `owner_preparation_pending` 홀드(`request_id="owner-20261009-new-policy"`)입니다. 알테오젠의 10월 6일 별도 수동 작업은 금요일 파이프라인 밖이며 한도·이력을 초기화하지 않습니다. 10월 9일의 적격 KIS 원본·근거가 없으면 계속 보류합니다. 두 날짜 모두 네이버 저장을 뜻하지 않으며 [DAILY_PLAN](DAILY_PLAN.md)의 구분을 유지합니다.

향후 구매·제휴 정보가 모두 확인됐을 때만 다음 선택적 입력을 비공개 원본에 포함합니다. 현재 질문에는 두 항목을 생략합니다.

- `purchase`: `owner_confirmed=true`, `publication_approved=true`, 실제 `product`, 제공된 `experience`, 구매 제품 이름과 현재 질문의 연결을 설명하는 소유자 제공 `origin_relevance`가 모두 필요합니다. `origin_relevance`는 비어 있지 않은 제한 길이의 설명이며 검수에서 실제 관련성을 대조합니다. 관련 없는 상품·제휴 링크는 반려합니다. 영수증·주문번호·결제 정보는 넣지 않습니다.
- `affiliate`: `provider="naver_shopping_connect"`, 구매 항목과 정확히 같은 `product`, 실제 `product_url`·`destination_url`, `owner_approved=true`, `eligibility_verified=true`와 위 전용 문서의 정확한 `disclosure`가 모두 필요합니다. 구매 입력 없이 제휴만 넣지 않습니다. 검증되지 않은 선언을 코드가 실제 인증한 것으로 보지 않습니다.
- 모델의 원고·판매자 페이지·트렌드에서 위 승인을 추출하거나 true로 바꾸지 않습니다. 승인된 제휴 링크와 고지는 렌더러가 별도로 표시하며 사실 검증용 `source_urls`에 제휴 링크를 넣지 않습니다. 공개 저장소·공개 예제에는 실제 상품 구매·경험·링크 메타데이터를 저장하지 않습니다.

## 운동 질문

목요일에는 실제 소유자 운동 질문을 `category="exercise"`로 등록합니다. 일반 글의
`editorial_type="article"`을 사용합니다. 필요한 운동 조건만 제공하며 미제공 경력·중량·
효과·부상을 모델이 채우지 않습니다. 입력이 없으면 다른 카테고리로 대체하지 않습니다.

### 주제 선정용 공개 키워드

육아·운동 입력의 `benchmark_query`에는 실명·생년월일·개인 병력 없이 공개 검색에 쓸
짧은 키워드를 넣습니다. 이 필드는 기존 구조 비교 외에 NAVER API HUB 추세 조회에도
사용됩니다. 비공개 `question`·`context`는 추세 API에 전달하지 않습니다.
미입력 시 자동으로 개인 질문을 검색어로 바꾸지 않고 후보 순서를 유지합니다.
투자는 심사를 통과한 공개 원본의 `stock_name`을 사용하며, 종목 전체의 관심 추이이지
해당 공시·사건 자체의 검색 수요나 투자 매력도를 측정하는 값은 아닙니다.

## 요리 자료

`cooking-request.example.json`을 복사해 실제 `recipe.name`, `ingredients`, `steps`를 입력합니다.
요리는 이번 정기 배분에 포함하지 않으며 별도 요청이 있을 때만 다룹니다.
`servings`, `total_time`, `tips`는 실제로 제공할 수 있는 경우에만 추가합니다.
`photos[].file`은 입력 JSON 기준 상대 경로나 PC의 절대 경로입니다. 캡션은 선택입니다.

```powershell
blogbot enqueue --file "C:/blogbot/incoming/my-recipe.json"
```

재료·조리 단계·사진 중 하나라도 없으면 등록을 거절하며 생성하지 않습니다.
JPG/PNG/WebP 사진 1~15장, 각 20 MB 이하를 입력 순서대로 복사합니다.
이후 원래 업로드 위치에서 파일을 옮겨도 큐에 복사된 원본은 유지됩니다.
큐의 사진을 변경하면 해시 검증에서 보류합니다. 수정본은 새 id로 다시 등록합니다.
모델은 사진 캡션을 참고하며 사진을 직접 분석하지 않습니다.

## 투자 자료

금요일은 `weekly-policy-v1`의 최근 **5 calendar days, D-4~D0** 새 정책 1건만 다룹니다.
`kis-community-bot/data/posts_latest.json`의 당일 07:00 KST 이후 최신 export 준비 확인과 제한된
커밋 이력의 정책 날짜 검증은 별개입니다. 원래 점수·fatal 문턱을 통과한 `policy`만 허용하며
리포트·공시 대체는 없습니다. 가격·시세·수급은 기존 D-1/D0를 유지합니다.

투자 후보를 `enqueue`하거나 날짜 지정 질문으로 직접 주입하지 않습니다. 적격 KIS 원본을 확인한 뒤
그 원본의 repository·commit·record_id·url·record_sha256에 묶인 비공개 근거 JSON만 등록합니다.

```sh
blogbot import-policy-evidence --file /private/path/policy-evidence.json
```

명령은 `BLOG_DATA_DIR/policy-evidence/{sha256(commit:id)}.json`에 근거를 등록하며 후보·승인 원고를 만들지 않습니다.
필드 계약과 실제 원문 재검증은 [WEEKLY_POLICY](WEEKLY_POLICY.md)를 따릅니다. 공개 저장소·Actions 로그에는
실제 근거 JSON을 넣지 않습니다. 등록한 파일을 기존 암호화 상태로 운반하고 다음 실행 환경에서 복원하기 전에는
기존 러너가 소비할 수 없습니다. 선택적 기존 러너 입력 `BLOG_POLICY_EVIDENCE_JSON`도 같은 등록 검증을 사용하며 공개 공식자료 메타데이터만 전달합니다. 실제 변수 등록은 별도이고 10월 9일 수동 홀드에서는 읽지 않습니다. 새 Secret·환경변수 등록·실행 예약 변경을 이 명령으로 대신하지 않습니다.
원본·근거가 없거나 원문 검증을 못 하면 보류합니다. 10월 9일은 적격 입력이 확보되어도 기존 수동 홀드를
자동 해제하지 않으며 새 정책용 승인 절차를 따릅니다.

## 공통 처리

후기 요청은 최상위 `content_style="review"`와 실제 제공 `photos`를 명시합니다.
육아·운동·요리 안의 편집 스타일이며 새 카테고리나 추가 슬롯이 아닙니다.
사진·경험·가격 제외·선택적 내돈내산과 저장 기준은
[후기형 편집·저장 기준](REVIEW_EDITORIAL.md)을 따릅니다. 일반 글은 이 필드를 생략합니다.

입력 없음·이미 처리한 원본·일일 한도 도달 시 유료 모델을 호출하지 않습니다.
실패·검수 탈락도 해당 원본의 생성 시도로 기록합니다. 자동 재생성으로 비용을 반복 지출하지 않습니다.
당일 복구는 **기존 id와 유효 계획·기존 한도**를 그대로 사용합니다. 실패를 새 id로
바꾸거나 파일·예산 기록을 지워 재시도 한도를 우회하지 않습니다. 같은 id의 파일이나
등록 폴더를 덮어쓰지 않습니다. 새 실제 질문과 재시도는 구분합니다.
과거 날짜의 유료 초안·이미지·미완료 상태는 보존·보류하며 새 계획의 당일 입력으로
자동 이동하거나 추가 과금·재저장하지 않습니다. 별도 활용은 원래 자료와 이력을 확인한 뒤 결정합니다.
예시 파일은 `enabled=false`이며 그대로는 등록·실행되지 않습니다.
