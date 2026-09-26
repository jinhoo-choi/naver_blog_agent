# 운영 가이드

기준: 2026-09-27 KST.

## 준비된 API 분리 운영 (활성화 대기)

자동 승인 검토가 새 복호화 키와 개인화 입력 큐의 GitHub Secrets 등록을 차단했습니다. 사용자 승인 후 아래 순서로 활성화합니다. 현재 Work 예약은 기존 경로를 유지합니다.

1. `Blog API Prepare` (Ubuntu, 04:15 KST): 원고·검수·삽화 API → 암호화 결과물.
2. 기존 Work 예약 (05시 전후): 당일 결과물 다운로드·복호화·이미지 확인 → 네이버 임시저장 → 재열기 확인.
3. 오류 시 완성된 결과물을 재사용하며 저장 단계만 이어갑니다. 발행은 사용자가 직접 합니다.

설정: `OPENAI_API_KEY`(기존), `BLOG_BUNDLE_KEY`(새 키), `BLOG_SEED_JSON`(비공개 질문 큐) Secrets.
`BLOG_API_ENABLED`만 새 API 준비 일정을 제어합니다. 기존 `BLOG_SCHEDULE_ENABLED=false`를 유지합니다.
최초 `bootstrap`은 모델 접근 확인과 암호화 상태 초기화만 하며 유료 콘텐츠를 생성하지 않습니다.
이후 `prepare`는 최신 `blog-state-<run id>-<attempt>`를 복원합니다. 상태가 유실되면 새 작업으로 간주해 중복 생성하지 않고 중지합니다.

복호화는 비공개 키를 환경변수로 전달하고 `python -m blogbot.cloud unpack --file bundle.enc --destination <private path>`를 사용합니다.
`ready.json`의 당일 APPROVED 글만 저장합니다. 파일 내 실제 제목·본문·segments·이미지 체크섬을 확인하고, 저장 결과는 비공개 성과 기록에 남깁니다.
GitHub 결과물은 암호화된 `bundle.enc` 한 개만 허용합니다. 원문·키·입력 큐를 공개 로그나 Git에 넣지 않습니다.
상태는 매 실행 이어받으며 API 결과물 보존기간은 30일, 전달할 최근 원고·이미지는 7일입니다. 오래된 투자 글은 자동 저장하지 말고 근거일을 재검토합니다.

이미지 TIMEOUT/UNCERTAIN은 자동 재호출하지 않습니다. 429는 최대1회 재시도합니다. 완료 이미지는 재사용하고, 품질 문제는 Work 최종 확인에서 보류합니다.
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
카테고리는 육아 1, 요리 6, 투자 7로 이미 연결되어 있습니다. 최종 발행은 직접 확인합니다.
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
4. BLOG_API_ENABLED=true 활성화. 기존 BLOG_SCHEDULE_ENABLED=false 유지.
5. 기존 Work 예약을 생성 전용 도구를 호출하지 않는 저장 전용 지침으로 변경.
6. Work는 당일 ready.json, 실제 이미지 파일·해시·카테고리1/8/7을 확인하고 임시저장만 수행. 최신 72시간 통계 피드백에 따른 최소 편집은 저장 전에 적용.
7. 브라우저 장애 시 완성본을 비공개 파일에 보관하고 저장 상태를 불확실로 기록. 목록을 확인한 뒤 저장 단계만 재개.
8. 첫 정규 실행 결과와 생성/이미지/저장 단계별 실제 소요시간 기록.
