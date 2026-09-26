# 운영 가이드

기준: 2026-09-26 KST. 네이버 로그인 세션·원고는 소유자의 Windows PC에 보관합니다.

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
게시판은 BLOG_SETUP.md를 참고해 직접 만듭니다. 코드는 원고 분류만 기록합니다.
본문 해시태그는 자동 첨부하며 실제 게시판/발행 태그는 최종 발행 때 확인합니다.

## 4. 실행·스케줄

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

## 공식 기술 문서

- https://developers.openai.com/api/docs/guides/tools-web-search
- https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- https://docs.github.com/en/actions/concepts/runners/self-hosted-runners
