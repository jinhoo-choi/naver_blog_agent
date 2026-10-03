# 운영 가이드

기준: 2026-09-27 KST.

## 주제 후보의 검색 추세 조회

원본 후보 수집 → NAVER API HUB 추세/캐시로 후보 정렬 → 기존 카테고리별 예약 →
구조 비교·작성·검수·삽화 → 기존 암호화 인계·Work 임시저장 순서입니다.
키 미등록·조회 장애·불충분한 자료가 있으면 기존 후보 순서로 계속합니다.

1. NCP 콘솔의 NAVER API HUB에서 애플리케이션을 등록하고 검색어 트렌드 사용을 설정합니다.
2. 해당 앱의 Client ID/Client Secret을 저장소의 Actions Secrets에 각각
   `NAVER_API_HUB_CLIENT_ID`, `NAVER_API_HUB_CLIENT_SECRET`으로 등록합니다.
   네이버 로그인 쿠키·GitHub PAT·OpenAI 키·NCP 계정용 Secret Key로 대체하지 않습니다.
3. 기존 `Blog API Prepare` 작업이 다음 실행부터 두 값을 전달합니다. 별도 예약은 필요 없습니다.
4. 복호화된 `topic-selection.json`의 날짜·`api_calls`·`credentials_present`·카테고리별
   `ranked`·후보별 `status`를 확인합니다. 파일이 없거나 날짜가 다르면 당일 조회 성공으로
   간주하지 않습니다. 선택 근거는 원고의 `provenance.topic_selection`에도 전달됩니다.
5. 롤백은 `config/blog.toml`의 `[topics].enabled=false`로 정렬만 끕니다.

키 등록·API 권한·실제 응답은 이번 구현에서 확인하지 않았습니다. `credentials_present`는
환경변수 존재 여부일 뿐 인증 성공을 의미하지 않습니다. 자동화는 카테고리당 기존 앞 5개,
최대 3회 호출하며 당일 캐시를 재사용합니다. 후보가 1개뿐이거나 일일 한도에 도달하면 조회하지 않습니다.
예외 시 최대 7일 이전의 동일 기준일 캐시로 비교하고 없으면 기존 순서를 유지합니다.
`benchmark_query`는 공개용으로 입력한 키워드만 사용합니다. 자동으로 새 주제를 만들지 않습니다.
구글 트렌드·블랙키위는 현재 연결하지 않으며 공식 API 접근이 확인된 후 추가합니다.

데이터랩은 절대 검색량·블로그 경쟁도·검색 노출 가능성을 제공하지 않습니다. 현재 점수는
최근 7일/직전 7일의 상대 추이 변화만 반영하는 초기 우선순위 가설입니다. 특히 종목명 추이는
해당 투자 글의 개별 사건 수요와 다를 수 있습니다. 이를 독자용 인기·추천 근거로 쓰지 않습니다.
신규 의존 패키지와 추가 LLM 호출은 없으며 API 요금·한도는 이용 계정의 현행 약정을 따릅니다.

공식 명세: https://api.ncloud-docs.com/docs/naver-api-hub-search-trend
이관 일정: https://developers.naver.com/notice/article/32530

## API 분리 운영 (활성화 완료)

2026-09-27 사용자 승인 후 두 Secrets와 비공개 인계 파일을 등록했습니다. bootstrap run 36283969527에서 모델 접근·암호화 상태 보관·연결 앱 다운로드·복호화(질문 큐20건)를 확인했습니다. BLOG_API_ENABLED=true, 기존 Work 예약은 검토·저장 전용입니다. 유료 콘텐츠 생성은 이번 초기화에 포함되지 않았습니다.

**2026-09-29 실제 설정 확인**: 07시대 cron-job.org 작업 8527289의 기존 자정 설정을 매일 09:00 Asia/Seoul (`0 9 * * *`)로 변경·저장했습니다. 관리 화면에서 활성화와 다음 실행 2026-09-29 09:00을 확인했습니다. Work 09·10시 예약과 지시문도 갱신했습니다. 변경 후 09시 실행은 아직 예정 상태이며, Actions 실행·원고 준비·네이버 임시저장 성공은 각각 확인해야 합니다. 09시는 준비 확인 시각이며 저장 완료 보장이 아닙니다.

1. cron-job.org → `Blog API Prepare` (Ubuntu, 매일 09:00 KST 요청): 원고·검수·삽화 API → 암호화 결과물.
2. Work 저장 예약(매일 09:15 KST): 당일 결과물 다운로드·복호화·이미지 확인 → 네이버 임시저장 → 저장 완료 표시와 목록의 제목·시각 확인. 저장본 재열기는 하지 않습니다.
3. Work 누락 점검(매일 10:15·11:15 KST): 10:15에 준비 실패를 발견하면 최신 main의 recover를 당일 한 번 요청하고, 11:15에는 복구 결과를 확인해 승인 원고의 저장을 이어갑니다. 진행 중인 복구·저장과 확인되지 않은 저장은 중복 요청하지 않습니다.
4. 준비 실패 시 완료 원고·출처·이미지를 재사용해 추가 복구를 하루 최대 2회 수행합니다. auto-recovery.json에 호출 전에 시도 횟수를 기록하고 기존 attempted=true는 1회로 이관합니다. 심사 반려·URL/날짜·도입/구조 오류·원문 조사·커뮤니티 조회·이미지 준비의 미완료 단계를 재개합니다. 도입 오류는 완료 작성 응답을 재사용해 수정·재검수하며, 타임아웃·연결 오류로 결과가 불확실하거나 인증/권한 오류가 있으면 유료 요청을 바로 재구매하지 않습니다. 복구 한도 이후에도 미완료는 RECOVERY_REQUIRED로 알리고 성공/완료로 닫지 않습니다. 승인된 일부 원고는 먼저 저장하며 발행은 사용자가 직접 합니다.

2026-10-01 출처 검증: HealthyChildren의 `form=HealthyChildren`, ACSM의 숫자 `nocache`만 문서 비교에서 제외합니다. DART 접수번호·그 밖의 문서 쿼리는 유지합니다. DART는 표지에서 실제 보고서 viewer를 찾아 본문을 가져온 뒤 작성·검수에 전달하고 암호화 체크포인트에 보존합니다. 원문을 확보하지 못하거나 핵심 주장별 `source_checks`가 미확인·불일치이면 PASS하지 않습니다. 모델 심사는 사실 정확성을 보장하지 않으며 Work 최종 검수에서도 본문과 1차 출처를 대조합니다.

설정: `OPENAI_API_KEY`(기존), `BLOG_BUNDLE_KEY`(새 키), `BLOG_SEED_JSON`(비공개 질문 큐) Secrets.
준비 예약은 cron-job.org의 활성 스위치로 제어합니다. `BLOG_API_ENABLED`는 외부 dispatch를 중지하지 않습니다. 기존 `BLOG_SCHEDULE_ENABLED=false`를 유지합니다.
최초 `bootstrap`은 모델 접근 확인과 암호화 상태 초기화만 하며 유료 콘텐츠를 생성하지 않습니다.
이후 `prepare`는 최신 `blog-state-<run id>-<attempt>`를 복원합니다. 상태가 유실되면 새 작업으로 간주해 중복 생성하지 않고 중지합니다.

복호화는 비공개 키를 환경변수로 전달하고 `python -m blogbot.cloud unpack --file bundle.enc --destination <private path> --receipts <private receipts.json>`를 사용합니다.
Work는 먼저 최신 비공개 성과 기록과 실제 임시저장/발행 목록을 확인해 `{"verified_date":"오늘 KST YYYY-MM-DD","records":[{"request_id":"원본 입력 ID","status":"SAVED_NAVER"}]}` 형식의 로컬 receipts를 만듭니다. 과거 날짜까지 모든 API 저장 이력을 포함하며 SAVING/SAVE_UNCERTAIN/SAVED_NAVER/PUBLISHED는 모두 재저장 제외입니다. 확인 없이 빈 records를 만들거나 기록 접근 실패를 빈 이력으로 취급하지 않습니다. 기존 수동 글은 제목·핵심 질문·원본 ID로도 대조합니다.
`unpack`은 당일 검증 이력이 없으면 실패하며, 기록된 request_id와 같은 패킷 내 중복을 제외합니다. API DB에는 Work 저장 완료가 역전송되지 않으므로 매 실행 이 검사를 생략하지 않습니다. 저장 직전에도 최신 이력을 재확인하고 기존 SAVING→저장 완료 표시·목록 제목/시각 확인→SAVED_NAVER 절차를 지킵니다. 원본 request_id는 최소 편집 후에도 유지하며 날짜가 바뀌었다고 기록을 지우지 않습니다.
`ready.json.date`는 오늘이어야 하며 APPROVED 원고는 KST D-3~D0만 전달합니다. 이전 날짜 원고는 `requires_fresh_review=true`이고 원래 자료 기준일을 유지합니다. 특히 투자 사실·원본 유효기간을 재확인할 수 없으면 보류합니다. 복구 글부터 검토하되 기존 당일 저장분을 포함해 총3건·카테고리별1건 한도를 유지합니다. 파일 내 실제 제목·본문·segments·이미지 체크섬을 확인하고 저장 결과는 비공개 성과 기록에 남깁니다.
GitHub 결과물은 암호화된 `bundle.enc` 한 개만 허용합니다. 원문·키·입력 큐를 공개 로그나 Git에 넣지 않습니다.
상태는 매 실행 이어받으며 신규 API 결과물 보존기간은 90일, 전달할 최근 원고·이미지는 7일입니다. D-3~D0 TEXT_APPROVED/IMAGES_PENDING은 기존 원고·검수·입력을 재사용해 이미지 단계만 재개합니다. 오래된 투자 글은 자동 저장하지 말고 근거일을 재검토합니다.

이미지 STARTED/UNCERTAIN/FAILED 및 소진된 429는 체크포인트 기록 시각(updated_at, UTC Unix 초)부터 6시간 후 API 단계에서만 1회 복구 호출합니다. 복구 예산은 호출 전에 영구 기록하며 다시 실패하면 보류합니다. 시각 없는 기존 manifest는 처음 확인한 시각부터 6시간 기다립니다. 최초 429의 최대1회 재시도는 유지하되 복구 호출에는 추가 429 재시도가 없습니다. 타임아웃된 기존 요청도 과금됐을 수 있습니다. 완료 이미지는 재사용하고, 품질 문제는 Work 최종 확인에서 보류합니다.
통계는 발행72시간 이후 D0~D2 일간 기준이며, Work에서 최신 피드백을 확인하고 필요한 편집만 적용합니다. 기존 발행 글은 수정하지 않습니다.

## PC/API 실행기 대안

아래 절차를 별도 활성화할 때 로그인 세션·원고는 소유자의 Windows PC에 보관합니다.

## 1. PC 준비

Windows x64, Chrome, Python 3.12, Git, GitHub CLI가 필요합니다.

```powershell
git clone https://github.com/jinhoo-choi/naver_blog_agent.git
cd naver_blog_agent
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
gh auth login
.\scripts\bootstrap-secrets.ps1
.\scripts\setup-runner.ps1
```

공식 Actions runner를 `naver-blog` 라벨로 등록합니다. 기존 실행기를 자동 교체하지 않습니다.
로그인한 Windows 데스크톱에서 `C:\blogbot\runner\run.cmd`를 실행하고 창을 유지합니다.
브라우저를 띄워야 하므로 Windows 서비스로 실행하지 않습니다. PC가 꺼지면 작업이 대기합니다.

## 2. Secrets와 Variables

[GitHub 설정](https://github.com/jinhoo-choi/naver_blog_agent/settings/secrets/actions)

| 종류 | 이름 | 설정 |
|---|---|---|
| 필수 Secret | OPENAI_API_KEY | API 사용 가능한 키 |
| 필수 Secret | NAVER_BLOG_ID | 블로그 ID, 전체 URL 아님 |
| 선택 Secret | NAVER_ID, NAVER_PASSWORD | 세션 만료 시 보조 로그인 |
| 선택 Secret | TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID | 알림 사용 시 |
| Variable | BLOG_DATA_DIR | C:\blogbot\data |
| Variable | BLOG_INBOX_DIR | 선택. 기본 BLOG_DATA_DIR 아래 inbox |
| Variable | NAVER_PROFILE_DIR | C:\blogbot\chrome-profile |
| Variable | OPENAI_MODEL | 기존 gpt-5 유지, 변경 가능 |
| Variable | OPENAI_REVIEW_MODEL | 미설정 시 작성 모델과 같음 |
| Variable | BLOG_DAILY_COUNT | 1~5, 기본 3 |
| Variable | BLOG_MODE | generate 또는 save, 기본 generate |
| Variable | BLOG_SCHEDULE_ENABLED | GitHub 스케줄 사용 시 true |
| Variable | BLOG_NOTIFY_ENABLED | Telegram 사용 시 true, 기본 false |

bootstrap은 필수 Secrets 2개와 경로·모델·건수 Variables를 설정합니다.
보조 로그인은 `-IncludeLogin`, Telegram은 `-IncludeTelegram`을 추가합니다.
키·쿠키·프로필·실제 원고는 저장소에 넣지 않습니다. `.env.example`은 자동 로드하지 않습니다.

## 3. 네이버 최초 로그인

위 가상환경을 켠 PowerShell에서:

```powershell
$env:NAVER_BLOG_ID="본인_블로그_ID"
$env:NAVER_PROFILE_DIR="C:\blogbot\chrome-profile"
blogbot login
```

열린 Chrome에서 로그인·보안 확인을 직접 마치고 글쓰기 도움말 팝업을 닫은 뒤 Enter를 누릅니다.
운영 중 이 프로필을 다른 Chrome에서 동시에 열지 않습니다.
카테고리는 육아 1, 운동 8, 요리 6, 투자 7로 연결되어 있습니다. 최종 발행은 직접 확인합니다.
본문 해시태그는 자동 첨부하며 실제 게시판/발행 태그는 최종 발행 때 확인합니다.

## 4. 실행·스케줄

먼저 [입력 가이드](INPUTS.md)에 따라 실제 육아 질문 또는 레시피·사진을 등록합니다.
투자는 `kis-community-bot`의 최신 심사 결과에서 자동으로 후보를 받습니다.
원본이 없는 카테고리는 건너뛰며 주제를 임의 생성하지 않습니다.

Actions → Naver Blog Draft → Run workflow:

| mode | 동작 |
|---|---|
| generate | 생성·검수·로컬 파일 저장 |
| save | 생성·검수·네이버 임시저장 |
| retry | API 재호출 없이 당일 APPROVED 원고 저장 |

count=3은 호출마다 추가 3건이 아니라 **KST 해당 날짜의 생성 후보 총 한도 3건**입니다.
실패·탈락도 호출 예산에 포함됩니다. 저장 성공 건수는 품질 심사에 따라 줄어듭니다.
같은 날 수동과 n8n이 겹쳐도 일일 한도를 넘지 않습니다.

### n8n

`n8n/daily-blog-dispatch.json`을 가져옵니다.
HTTP Request 노드에 Header Auth credential을 만들어 이름 `Authorization`, 값 `Bearer <GitHub PAT>`를 설정합니다.
PAT는 n8n credential에 보관하며 JSON에 넣지 않습니다.
저장소 소유자 계정의 토큰을 사용합니다. classic PAT는 public_repo, fine-grained PAT는 해당 repo Contents 쓰기 권한을 확인합니다.
GitHub 자체 스케줄은 비활성으로 두고 n8n을 활성화하면 매일 07:00 KST, 후보 3건, mode=save로 실행됩니다.
HTTP body의 count를 1~5로 조절합니다.

### GitHub 스케줄 대안

n8n 대신 `BLOG_SCHEDULE_ENABLED=true`, `BLOG_MODE=save`를 설정하면 매일 07:17 KST에 실행됩니다.
GitHub 상황에 따라 지연되거나 공개 저장소 장기 비활성으로 중지될 수 있습니다.
두 스케줄을 동시에 켤 필요는 없습니다.

## 5. 저장 확인·복구

원고: BLOG_DATA_DIR\drafts / 이력: BLOG_DATA_DIR\blog.db.
Actions 로그·공개 아티팩트에는 원고 제목/본문과 API 응답을 남기지 않습니다.

```powershell
$env:BLOG_DATA_DIR="C:\blogbot\data"
blogbot status
```

| 상태 | 처리 |
|---|---|
| APPROVED | 검수 통과, 저장 대기 |
| SAVED_NAVER | 완료 메시지 확인, 최종 발행은 직접 |
| SAVING / SAVE_UNCERTAIN | 미확인 저장, 자동 재시도 중지 |
| STALE_REVIEW_REQUIRED | 전날 원고, 자료와 내용 재확인 필요 |
| DROP_REVIEW / DROP_DUPLICATE | 심사·중복 보류 |
| ERROR | 생성·자료·파일 오류, 타입만 로그에 기록 |
| SETUP_REQUIRED | 사진 첨부 설정·파일 확인 필요. 승인 원고는 그대로 보관 |
| INPUT_REJECTED | 입력 형식·누락·사진 변경 확인 |
| COMMUNITY_SOURCE_UNAVAILABLE | 원본 GitHub 조회 실패. 사용자 질문·요리는 계속 처리 |
| NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT | 새 입력 없음 또는 일일 한도 도달, 유료 호출 없음 |

네이버 임시저장 목록에서 제목·본문을 직접 확인한 뒤:

```powershell
blogbot resolve --id 12 --outcome saved
# 실제로 저장되지 않았음을 확인한 경우에만:
blogbot resolve --id 12 --outcome not-saved
blogbot retry
# 폐기:
blogbot resolve --id 12 --outcome discard
```

로컬 retry에는 NAVER_BLOG_ID·프로필 설정이 필요합니다. GitHub retry는 Secrets를 주입합니다.
전날 원고는 not-saved로 되돌려도 자동 저장하지 않습니다. 직접 자료를 갱신해 활용하거나 폐기합니다.
화면 구조 변경·보안 확인·알 수 없는 팝업은 작업을 중지합니다.
`config/blog.toml`의 [naver] 셀렉터는 실제 편집기에서 확인한 값으로 조정합니다.
요리는 `photo_button_selector`(파일 선택창을 여는 버튼)와 `uploaded_image_selector`(본문 이미지)를
확인한 뒤 설정합니다. 두 값이 비어 있으면 브라우저를 열기 전에 SETUP_REQUIRED로 보류합니다.
사진 첨부 후 본문 이미지 수 증가를 확인해야 임시저장으로 진행합니다.

## 공식 기술 문서

- https://developers.openai.com/api/docs/guides/tools-web-search
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/actions/concepts/runners/self-hosted-runners


## 전환 체크리스트

1. 사용자 승인 후 BLOG_BUNDLE_KEY·BLOG_SEED_JSON 등록. OPENAI_API_KEY는 기존 Secret 사용, 외부 추출 없음.
2. 비공개 인계 키를 사용자 파일에 보관하고 정확한 파일 ID를 Work 지침에 연결.
3. 초기 bootstrap 성공 및 암호화 상태 결과물 다운로드·복호화 가능 여부 확인.
4. cron-job.org의 준비 예약을 활성화하고 GitHub 기본 schedule은 제거합니다. 기존 BLOG_SCHEDULE_ENABLED=false 유지.
5. 기존 Work 예약을 생성 전용 도구를 호출하지 않는 저장 전용 지침으로 변경.
6. Work는 당일 ready.json, 실제 이미지 파일·해시·카테고리1/8/7을 확인하고 임시저장만 수행. 최신 72시간 통계 피드백에 따른 최소 편집은 저장 전에 적용.
7. 브라우저 장애 시 완성본을 비공개 파일에 보관하고 저장 상태를 불확실로 기록. 목록을 확인한 뒤 저장 단계만 재개.
8. 첫 정규 실행 결과와 생성/이미지/저장 단계별 실제 소요시간 기록.

## 저장 전용 Work 인계 확인

- 당일 main Blog API Prepare 실행을 조회하고 blog-state 결과물을 연결 앱으로 다운로드합니다. PR 전용 실행 조회 함수로 일정 실행을 찾지 않습니다.
- 비공개 키의 위치는 Work 예약 지침에만 기록합니다. 복호화 후 ready.json 날짜, APPROVED 상태, 이미지2/2/1장과 SHA-256을 확인합니다. 초기화 파일의 글0건은 정상입니다.
- 네이버 쓰기 전에 비공개 기록에 SAVING, 저장 목록·재열기 확인 후 SAVED_NAVER를 남깁니다. SAVING/SAVE_UNCERTAIN 재실행은 목록부터 확인합니다.
- API 지연·이미지 실패 시 새 생성으로 대체하지 않습니다. 네이버 장애 시 원고를 보존하고 저장 단계만 재개합니다.
- 최초 무인 예약 실행은 아직 미검증이며 예약 시작 시각은 완료 보장이 아닙니다.

## 검색 품질 운영 기준 — search-quality-v1

적용일: 2026-09-27 KST. 공식 공개 설명에 근거한 편집 개선이며 성과 실측으로 확정한 규칙은 아닙니다. feedback-vN과 별도 버전으로 기록합니다.

- C-Rank: 출처의 주제별 신뢰도에 대응해 기존 카테고리·하위 카테고리에 맞는 콘텐츠를 축적합니다. 육아/운동/투자 각 1건 배분을 유지하며, 카테고리 분리만으로 별개 출처처럼 평가된다고 주장하지 않습니다.
- D.I.A.: 한 글의 핵심 검색 질문, 첫 문단의 답, 조건·근거·실천 항목을 일치시킵니다. 공식 자료의 단순 요약에서 나아가 조건 비교·오해 교정 등 독자에게 필요한 설명을 보강하되 경험은 창작하지 않습니다.
- API는 실행 때 writer/reviewer 최신 파일을 읽습니다. 추가 LLM 호출·이미지 생성·별도 스케줄은 추가하지 않습니다. 기존 작성/검수 호출 안에서 기준을 적용합니다.
- Work는 비공개 성과 기록과 실제 발행/임시저장 목록으로 같은 질문·답의 중복을 확인합니다. 제목만 비슷하면 중복이라고 단정하지 않고 조건·본문·새 사실을 대조합니다. 내용이 같은 글은 제목만 바꿔 저장하지 않습니다.
- Work는 공개 상태·제목·내용·URL을 실제 확인한 관련 발행글이 있을 때만 독자의 다음 질문을 해결하는 링크를 1~2개 이내로 선택합니다. 이 개수는 운영상 상한이며 공식 순위 기준이 아닙니다. 동일 카테고리를 우선하며 무관한 링크·미발행 초안·추측 URL은 넣지 않습니다. 확인할 수 없으면 링크를 생략하고 저장은 계속합니다. 기존 발행글을 역으로 편집하지 않습니다.
- 제목·문단의 키워드 과잉, 근거 없는 경험, 삽화에만 담긴 핵심 정보는 저장 전 최소 편집합니다. 큰 사실 수정은 기존 검수 원칙대로 보류합니다.
- 기존 D0~D2 분석에 주 검색 질문/하위 주제/search-quality 버전을 함께 기록하고, 검색 유입수·유입 검색어를 제공 범위에서 비교합니다. 같은 글의 D0~D6(7일), D0~D27(28일) 후속 구간도 최초 1회씩 완료 일자 이후 기존 예약에서 확인합니다. 첫날은 부분 일자이며 정확한 168/672시간 집계로 부르지 않습니다. 접근 실패는 보류하고 새 예약을 만들지 않습니다.
- 3일 결과는 초기 반응으로만 보고, 주제별 누적 효과는 유사 조건의 7/28일 자료가 쌓인 뒤 가설로 판단합니다. 소표본이나 조회수 변화만으로 알고리즘 효과를 확정하지 않습니다. 누락·미제공은 0으로 채우지 않습니다.

공식 참고자료(2026-09-27 확인):
- 네이버 블로그 C-Rank: https://help.naver.com/service/5626/contents/22927?osType=COMMONOS
- 네이버 블로그 D.I.A.: https://help.naver.com/service/5626/contents/22926?osType=COMMONOS
- 네이버 웹 콘텐츠 권장사항: https://searchadvisor.naver.com/guide/content-basic

마지막 자료는 웹사이트 일반 지침입니다. 콘텐츠 원칙만 참고하며 블로그에서 제어할 수 없는 robots/sitemap/meta 설정이나 웹사이트의 수치 예시를 블로그 공식 랭킹 요건으로 전용하지 않습니다. 공개 자료는 모든 검색 화면의 전체 랭킹 공식이나 최신 가중치를 공개하지 않습니다.


## 2026-09-28 첫 API 실행 실패 방지

- 첫 준비 실행의 3건은 `JSONDecodeError`, 원고/이미지 0건이었다. 당시 원문·응답 상태·토큰 사용량을 보존하지 않아 정확한 발생 단계와 청구액은 소급 확정하지 않는다.
- GPT-5 Structured Outputs를 benchmark/writer/reviewer/rewrite에 적용한다. completed 상태·refusal·빈 출력·JSON 객체·필수 필드를 확인한 뒤만 처리한다. 이유가 명시된 max_output_tokens 미완료만 같은 단계에서 1회 재시도하고, 타임아웃/빈 출력/거절/임의 오류는 자동 재호출하지 않는다.
- benchmark 출력 예산 1800→6000(명시적 토큰 소진 때 10000으로 1회), writer/rewrite 12000→16000, reviewer 6000→10000. reasoning effort=low. 실제 응답과 추론 토큰이 같은 출력 예산을 사용한다. 모델·일일 건수·품질 기준은 유지한다.
- 비공개 데이터 디렉터리의 usage.jsonl에 성공/실패 각 호출의 단계·상태·응답 ID·입출력/캐시/추론 토큰·검색 호출 수·경과 시간을 기록한다. 원문 프롬프트/응답·오류 메시지·키는 기록하지 않는다. 기록과 run-summary.json은 기존 암호화 bundle에만 보존한다.
- ERROR/RESEARCH_REQUIRED/IMAGES_PENDING/입력·원본 접근 실패 등은 Workflow를 실패로 종료한다. 성공 표시로 가리지 않고 finally의 암호화 체크포인트 업로드는 유지한다. 품질 DROP·중복 제외·일일 한도는 정상 판단으로 별도 표시한다.
- GPT-5 표준 단가로 계산되는 텍스트/검색 호출 추정치는 청구액이 아니다. 검색 콘텐츠 토큰·서비스 등급·할인·세금 등을 포함한 실제 금액은 계정 Costs 화면으로 대조한다. 이전 실행은 usage 미보존으로 0원이나 임의 총액을 쓰지 않는다.
- workflow_dispatch의 probe는 공개 검색 1회 이내·6000 출력 토큰 이내의 구조화 응답 진단 1건만 수행한다. 원고·이미지를 생성하거나 네이버를 저장하지 않는다. prepare 재실행으로 진단하지 않는다.
- 모든 새 글의 대제목(##) 앞과 참고자료 앞에 기본 실선 `<hr>`를 렌더링한다. 카시트 발행글의 구역 구분 배치를 반영하며 일반 문장/모든 문단마다 추가하지 않는다. Work 저장 후 실제 separator와 재열기 결과를 확인한다.


## cron-job.org 외부 예약 — 2026-09-29부터

| 설정 | 값 |
|---|---|
| 대상 | `https://api.github.com/repos/jinhoo-choi/naver_blog_agent/actions/workflows/blog-prepare.yml/dispatches` |
| 방식 | POST |
| 시간대 / 시간 | Asia/Seoul / 매일 09:00 (`0 9 * * *`) |
| 본문 | `{"ref":"main","inputs":{"mode":"prepare"}}` |
| 헤더 | Accept: application/vnd.github+json, Content-Type: application/json, X-GitHub-Api-Version: 2022-11-28 |
| 인증 | Authorization: Bearer 토큰. 해당 저장소 Actions 쓰기 권한, 소유자 계정. 값은 cron-job.org에서만 관리 |
| Work | 09:15 승인 원고 임시저장, 10:15 누락 점검과 완료된 준비물 저장 재개. 하루 합산 3건 |

HTTP 204는 실행 요청 접수이며 원고/이미지/임시저장 성공이 아닙니다. Actions 결과와 암호화 ready.json, 실제 네이버 저장을 각각 확인합니다. 외부 호출 실패를 이유로 유료 생성을 반복 호출하지 않습니다. 원고 생성과 무관한 점검은 GET workflow 조회로 인증을 확인할 수 있고, 생성 POST의 최초 실제 성공은 첫 예약 실행에서 확인합니다.

09시 요청 전에 외부 예약을 완성하고 GitHub 기본 schedule을 제거해 중복을 방지합니다. 외부 호출 성공 여부가 확인되지 않은 준비 단계에서는 기존 운영 예약을 보존합니다. 외부 크론을 중단할 때는 cron-job.org에서 Disable job을 적용하며 GitHub의 workflow 자체를 비활성화하면 수동 복구도 막히므로 구분합니다.


## 해시태그 누락 방지 — 2026-09-28

- 새 원고의 tags는 주 검색어와 본문에서 다룬 세부 질문·대상·상황으로 보통 3~5개, 최대8개를 선택한다. 관련 표현이 적으면 억지로 채우지 않는다. #·공백·구두점을 제외하고 중복·동의어 나열·무관한 인기어·맞팔/이웃모집은 제외한다. 이 개수는 편집 기준이며 검색 상위노출을 보장하지 않는다.
- 기존 render_segments/render_post_text는 tags를 참고자료 뒤 마지막 문단에 #태그 형태로 붙인다. Work는 본문만 복사해 태그를 누락하지 말고 준비된 마지막 segment까지 반영한다. 이미 붙은 태그 문단을 중복 추가하지 않는다.
- tags가 비어 있거나 형식이 잘못됐으면 검수된 제목·본문에서 직접 확인되는 표현만 최소 보완한다. 태그만을 위해 추가 LLM 호출·원고 재생성·유료 검색을 하지 않는다. 수정한 태그와 적용일은 비공개 성과 기록에 남긴다.
- 임시저장 후 재열어 마지막 해시태그 문단과 원고 tags가 일치하는지 확인한다. 본문 해시태그 저장과 네이버 전용 태그 등록은 별개로 보고한다. 발행 설정에만 있는 태그 입력란을 처리하려고 발행 버튼을 누르지 않는다. 전용 태그 미등록이면 결과에 최종 발행 시 입력할 태그를 함께 제공한다.
- 기존 발행글은 이 변경으로 수정하지 않는다. 검색 부진은 태그 부족으로 단정하지 않고 실제 공개/검색 허용 설정, 제목 검색 시 색인 여부, 검색 유입어를 구분해 확인한다. 원인 미확인은 그대로 기록한다. 기존 3/7/28일 관찰에 적용 태그를 함께 기록하되 태그 효과의 인과관계를 단정하지 않는다.


## 모바일 미리보기·요약 썸네일 편집

- 핵심 강조(2026-10-01): 본문 `**구절**`은 볼드, `__조건__`은 밑줄, 별도 문단의 `> 핵심 요약`은 20px·볼드로 렌더링한다. 본문 16px·대제목 24px·소제목 19px를 유지한다. 원문 HTML은 실행하지 않는다. Work는 준비된 서식을 붙이고 저장 전 강조가 실제 보이는지 확인한다. 일반 텍스트 입력 시 기호를 노출하지 말고 편집기의 볼드·밑줄·글자 크기로 적용한다. 첫 미리보기 문단은 일반 문장으로, 강조는 핵심 결론·중요 조건·주의사항에만 제한적으로 적용한다. 강조 부족은 최소 서식 편집으로 보완하며 재생성·추가 검수 호출·저장 후 재열기를 요구하지 않는다.
- 공개 글은 본문 첫 1~2문장 요약부터 시작한다. 작성일·기준일 머리말은 삭제하고 내부 as_of_date/source_date 검증과 실제 사건일·통계 기간은 유지한다.
- 문체·근거 편집은 docs/BLOG_SETUP.md의 2026-10-03 기준을 적용한다. 자연스러운 습니다체·짧은 문단·구체적인 제목과 행동을 확인하고 반복 어미·방어적 메타 설명을 정리한다. 연구의 관찰 결과와 범위의 한계를 함께 남기며 수유 중단·응급 대응·안전수면을 삭제하지 않는다. 원문 인용·제공된 실제 발언은 보존한다. 문체 편집만으로 추가 유료 생성·심사 호출을 늘리지 않는다.
- 사용자 편집 형식 확인(2026-10-03): 구분선·줄바꿈은 필수다. 도입 뒤 대제목 구역과 참고자료 앞의 실제 기본 실선 구분선, 의미별 짧은 문단과 빈 줄이 편집기에 남아 있는지 저장 전에 확인한다. `---` 문자나 자동 줄 감김을 실제 구분선·의도한 문단 나눔의 대체로 보지 않는다. 일반 텍스트로 붙여 서식이 사라졌으면 해당 구역만 편집기의 구분선·문단 나눔으로 복원한다. 기존 저장·재열기 확인 때도 해당 형식의 보존을 확인하되 별도 재생성·유료 호출·불필요한 추가 저장을 만들지 않는다. 경험 뒤 중복 면책은 복원하지 않고 연구 구역의 한계는 유지한다. 삽화 안내·링크카드가 인접 본문과 같은 주제를 가리키는지도 확인한다.
- ESL 자세·순서 삽화는 관련 본문 옆에 두고 공식 자료와 실제 그림의 몸 지지·머리/등 정렬·손 위치·휴식/중단을 대조한다. 기존 둥근 인물·두꺼운 윤곽선·세이지/노란색 스타일을 유지하며 그림의 설명과 순서는 인접한 본문·캡션에도 남긴다. 실제 이미지·링크카드 요건과 기존 이미지 예산은 유지한다.
- 평일에는 준비된 segments를 순서대로 붙인다. 도입 요약 → role=thumbnail 첫 이미지 → 본문과 설명 이미지(육아·운동 4장, 투자 2~3장) → 참고자료·태그 순서다. 출처 목록을 맨 앞에 붙이거나 본문 대신 참고자료 segment만 복사하지 않는다.
- 평일에는 육아·운동 각 5장, 투자 기본 3장·본문 2,500자 이상 4장을 생성한다. 하루 신규 생성 총 13~14장이다. 요리는 제공된 실제 사진만 사용한다. 동시 호출 2개와 기존 체크포인트·재시도 한도를 유지한다.
- 첫 이미지를 대표이미지로 선택한다. 별도 선택이 불가능하면 저장을 중단하지 말고 첫 이미지 배치 사실과 대표 설정 미확인을 구분해 기록한다. 단순 업로드만으로 대표 설정 성공을 주장하지 않는다.
- 생성 후 카테고리·분량에 맞는 실제 이미지 수·제목 한글 오탈자·본문과의 일치·잘림·인체/사물 오류·육아 안전을 확인한다. 작은 화면에서 제목과 주제가 보이지 않으면 보류·기존 오류 알림에 사유를 남긴다.
- 참고자료 URL은 유지하되 HTML에서는 도메인과 자료 번호의 링크로 표시한다. 평일에는 링크를 별도 URL 텍스트나 자동 링크 카드로 다시 붙이지 않는다. 주말 심화 글은 docs/BLOG_SETUP.md의 저장 기준을 우선한다. 권한·출처를 확인한 실제 이미지2장 이상과 관련 단계의 공식 직접 링크카드1개 이상, 재사용 자료1개를 확인하며 실제 캡처3~5장은 목표다. 준비된 보조 삽화는 중복 시 생략할 수 있고 최종 총5장 또는 썸네일1+본문4를 강제하지 않는다. 필수 자료 미확보 시 보류하며 AI로 캡처를 위조하거나 추가 유료 생성으로 대체하지 않는다. 플랫폼의 최종 미리보기 선택은 보장하지 않는다.
- 기존 발행글은 이번 코드 변경만으로 수정되지 않는다. 새 생성물부터 적용한다. 과거 승인 원고를 이미지 수 증가만으로 전량 재생성하지 않는다.


2026-10-01 수동 정정 복구: 명시적 사용자 요청으로 확정한 원고 수정은
`editorial/<원래 기준일>/<request_id SHA256>.enc`에 기존 인계 키로 암호화해 둡니다.
`recover`는 동일 입력·기준일의 제목/본문 및 기존 출처의 부분집합만 적용하고
새 API 독립 심사를 통과한 경우에만 승인·이미지 단계로 진행합니다.
정정 원고를 심사 없이 승인하거나 자동 재작성 횟수 제한을 늘리지 않습니다.
이미지 생성 지시에 영아의 등을 대고 눕힌 자세·몸에 맞는 잠옷·맞춤 시트만 있는
빈 침대를 명시하되, 최종 실제 이미지 검사는 계속 필요합니다.

### 주제별 품질 기준 적용 (2026-10-03)
작성·수정·심사에는 `src/blogbot/editorial.py`의 주제별 기준이 실제 주입됩니다.
이미지 생성은 해당 구역의 설명을 함께 사용합니다. 기존 미완료 이미지의 계획이
달라지면 유료 재호출 전에 보류하므로 파일이나 예산 기록을 삭제해 우회하지 않습니다.
적용 경로, 네 유형의 평가 예시, 외부 Work 지시문 최소 수정안과 검증 한계는
[CONTENT_QUALITY.md](CONTENT_QUALITY.md)를 따릅니다. Work 지시문은 별도 확인이 필요합니다.
