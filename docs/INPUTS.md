# 입력 큐 사용법

입력은 Windows 실행기의 `BLOG_DATA_DIR/inbox`에 보관합니다. 경로를 바꾸려면 `BLOG_INBOX_DIR`을 설정합니다.
키·아이 정보·원본 사진·실제 입력 JSON을 공개 GitHub에 올리지 않습니다.

## 육아 질문

`parenting-request.example.json`을 PC의 개인 폴더로 복사합니다.
`enabled`를 true로 바꾸고 실제 질문·고유 id를 입력합니다. 월령을 모르면 null을 유지합니다.
`context`에는 답에 필요한 조건만 적습니다. 과거 대화 전체를 붙여 넣지 않아도 됩니다.

```powershell
$env:BLOG_DATA_DIR="C:/blogbot/data"
blogbot enqueue --file "C:/blogbot/incoming/parenting-question.json"
```

월령별 발달, 지루성 두피염, 목튜브 질문처럼 같은 주제를 다른 조건에서 다시 다루려면
새로운 id와 구체적인 질문을 등록합니다. 중복 제목·유사 본문은 이후 검수에서 걸러집니다.
ChatGPT 대화를 자동 감시하는 기능은 없습니다. 이 대화에서 질문을 입력 파일로 정리한 뒤 실행기에 등록하는 방식입니다.

### 주제 선정용 공개 키워드

육아·운동 입력의 `benchmark_query`에는 실명·생년월일·개인 병력 없이 공개 검색에 쓸
짧은 키워드를 넣습니다. 이 필드는 기존 구조 비교 외에 NAVER API HUB 추세 조회에도
사용됩니다. 비공개 `question`·`context`는 추세 API에 전달하지 않습니다.
미입력 시 자동으로 개인 질문을 검색어로 바꾸지 않고 후보 순서를 유지합니다.
투자는 심사를 통과한 공개 원본의 `stock_name`을 사용하며, 종목 전체의 관심 추이이지
해당 공시·사건 자체의 검색 수요나 투자 매력도를 측정하는 값은 아닙니다.

## 요리 자료

`cooking-request.example.json`을 복사해 실제 `recipe.name`, `ingredients`, `steps`를 입력합니다.
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

등록할 파일은 없습니다. 매 실행 시 `kis-community-bot/data/posts_latest.json`을 읽습니다.
블로그용 문턱과 최신성 조건을 만족하는 새로운 원본만 하루 최대 1건 후보로 받습니다.
원본 봇이 새 결과를 내지 않거나 기준을 통과하는 자료가 없으면 투자 생성을 건너뜁니다.
세부 기준은 [BLOG_SETUP](BLOG_SETUP.md)에 있습니다.

## 공통 처리

입력 없음·이미 처리한 원본·일일 한도 도달 시 유료 모델을 호출하지 않습니다.
실패·검수 탈락도 해당 원본의 생성 시도로 기록합니다. 자동 재생성으로 비용을 반복 지출하지 않습니다.
실패 원인을 해결한 뒤 다시 만들려면 내용 확인 후 **새 id**로 등록합니다.
같은 id의 파일이나 등록 폴더를 덮어쓰지 않습니다.
예시 파일은 `enabled=false`이며 그대로는 등록·실행되지 않습니다.
