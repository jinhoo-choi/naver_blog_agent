# 2026-10-07 Actions Node 24 최소 호환 변경

기준 main: `ef6a28040a0d726c5db504831c678708a91f7c41`. 변경은 검토용 PR; 운영 workflow 및 실발송 미실행.

## 현재 workflow / job 목록

| Workflow | Job | 기존 runner → 변경 | 기존 action → 변경 |
|---|---|---|---|
| ci.yml | test | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6 |
| blog-draft.yml | draft | self-hosted, Windows, naver-blog → self-hosted, Windows, naver-blog | actions/checkout@v4 유지<br>actions/setup-python@v5 유지 |
| blog-prepare.yml | prepare | ubuntu-latest → ubuntu-latest | actions/checkout@v4 → actions/checkout@v5<br>actions/setup-python@v5 → actions/setup-python@v6<br>actions/upload-artifact@v4 → actions/upload-artifact@v6 |

## 확인 근거와 선택

- 최신 hosted CI #134 / run 37620796019 / job 112790266085: runner 2.337.0, Ubuntu 24.04; checkout@v4/setup-python@v5 Node 20 경고 실측. CI #132에서도 사용자 보고된 동일 refs 확인.
- CI와 API prepare만 Node 24 action 적용. Python 3.12 명시, 26.04 공식 toolcache에 3.12.14 존재. cryptography/Playwright는 pip wheel 의존성. API prepare는 save_to_naver=False, CI는 브라우저 launch/install 단계가 없어 Linux 브라우저 시스템 패키지 의존성 없음; ubuntu-latest 유지.
- Windows self-hosted draft는 checkout@v4/setup-python@v5 유지. 조회한 최근 100개 실행의 draft 9건은 모두 schedule/skipped, runner version 로그 없음. setup-runner.ps1은 최신 버전을 내려받는 설치 방법일 뿐 현재 설치 버전 증거가 아님. 현재 runner >=2.327.1 미검증이므로 올리지 않음. workflow 주석에 보류 이유 명시.
- 추후 기존 실행 Set up job의 Current runner version 또는 실제 머신 Runner.Listener.exe --version이 >=2.327.1인지 확인한 뒤 checkout@v5/setup-python@v6로 변경 가능. 이 작업에서 runner 등록·업데이트·운영 실행은 하지 않음.
- pytest -q 576 passed, ruff check src tests 전체 통과.

## 불변식 / 검증 범위

트리거, schedule, concurrency, permissions, secrets, if 조건, action 입력, Python 버전, cache key/path, artifact name/path/retention, 실행 명령 및 발송 가드는 유지. YAML 파싱 후 이 항목의 변경 전후 동등성 확인.

3개 저장소 17개 workflow / 18개 job 정적 점검. actionlint 1.7.12 통과(기존 self-hosted 사용자 label naver-blog는 외부 검증 설정에 등록, shellcheck/pyflakes 연동 제외); hosted Bash 블록 69개 bash -n 통과. 공식 action.yml로 전체 44개 action 참조의 입력 키 검증 및 hosted 42개 참조 node24 선언 확인. Windows 2개 참조만 보류.

26.04 실제 실행·브라우저 실행·운영 dispatch·재실행·유료 호출·실발송은 하지 않음. 기존 테스트는 로컬 Python 3.12.14 환경; hosted Python 3.11/3.12 런타임이나 Ubuntu 26.04에서의 실행 성공을 의미하지 않음.

## 공식 자료

- https://github.com/actions/runner-images/issues/14748 — 2026-10-19 시작, 2026-11-19 완료 예정.
- https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2604-Readme.md
- https://github.com/actions/checkout/blob/v5/action.yml
- https://github.com/actions/setup-python/blob/v6/README.md
- https://github.com/actions/cache/blob/v5/README.md
- https://github.com/actions/upload-artifact/blob/v6/README.md
- https://github.com/actions/download-artifact/blob/v7/README.md

## 다음 자연 실행의 확인 / 롤백

병합 후 정상 일정의 Set up job runner 버전, Node 20 경고 제거, Python 설치, pip 설치, artifact 업로드를 확인. 운영 검증 완료 주장 없음. 문제 발생 시 이 PR의 변경 커밋만 revert. Windows self-hosted는 설치 버전 확인 전 기존 action 유지.
