# 육아 수동·자율 제작 프롬프트 검증 — 2026-10-18

기준 main: `059da25fe20b60cb5667b94d5bf43a5158c93db2`. Python 3.12.14, 오프라인 검증.

## 수정 결과

- 기존 공유 `routed_category_info`에서 요청의 provenance를 읽어 육아 제작 범위를 구분합니다.
- `source=work_autonomous`와 `strategy_version=strategy-reset-v2`가 모두 있으면 승인된 Work 선정 주제를 다룹니다. 사용자 직접 질문이 아니라는 이유로 제외하지 않습니다.
- `owner_input`·출처 미지정은 실제 수동 질문 범위를 유지합니다. 누락·구버전·알 수 없는 출처는 자율 제작 범위를 받지 않습니다. 이 fallback은 입력 승인이 아니며 기존 등록 경로는 잘못된 source/전략/슬롯을 계속 거절합니다.
- Creator Advisor 최신 유효 자료, 네이버 상위 본문3~5개, 기존 글 중복 대조를 선정 조건으로 명시합니다. 선정 근거가 없으면 보류하며 생성·검수 도중 다른 글감을 고르지 않습니다.
- `blog.toml`의 포괄적 자율 선정 금지와 과거 키워드 목록 문구를 정리했습니다. AI 튜토리얼도 등록 질문으로 표현을 통일했습니다.
- 생성·검수·재작성·사전 수정에 같은 출처를 전달합니다. 출처 검수 복구의 입력 해시도 동일한 라우팅을 사용합니다.
- `config.py`의 심화 규칙 추가는 유지합니다. 충돌하는 원본 규칙과 요청별 라우팅을 바로잡아 전체 설정 재작성은 하지 않았습니다.

## 회귀 검증

| 검증 | 결과 |
|---|---|
| 수정 전 기존 전체 테스트 | 1,475 통과 |
| 새 회귀 테스트를 수정 전 코드에 적용 | 21 실패·9 통과: 최종 프롬프트의 과거 제한 재현 |
| 새 회귀 테스트를 수정 후 코드에 적용 | 30 통과 |
| 수정 후 전체 테스트 | 1,505 통과 |
| `ruff check src tests`, `git diff --check` | 통과 |
| 최종 프롬프트 비교 | 수동·자율 × 정보글·AI 튜토리얼·후기 × 4단계 = 24쌍 |
| 설정 불변 대조 | 육아 rules 외 전체 TOML 동일 |
| 편성 불변 대조 | 2026-10-18부터 연속120일의 resolve_plan 결과 동일 |

새 테스트는 실제 `BlogLLM.create_draft`, `review`, `rewrite`, `correct_draft`를 호출하고 최종 `request_json(input=...)`을 캡처합니다. 모델 응답과 원문 조회만 fixture로 대체하며 유료 API·브라우저·외부 발행은 실행하지 않았습니다.

입력 등록은 정상 수동·자율 질문의 출처 보존, 구버전 및 비발행 슬롯 거절을 확인했습니다. 정확성/안전성 4점 미만의 PASS 거절, 원문 미열람 시 REWRITE, 미제공 개인 경험·아이 월령 탐지를 확인했습니다. 의료 진단·치료 지시 금지, 신뢰 가능한 출처, 개인정보·허위 체험담 금지, 단계별 안전 행동은 4단계 최종 프롬프트 모두에 남아 있습니다.

기존 발행 권한·승인·Work 인계·스케줄·주간/일일 한도와 유료 호출 상한은 변경하지 않았습니다. 원문/사진 없는 후기를 적격이라고 판정한 테스트는 아닙니다. 이미 고정된 구 프롬프트/입력 해시는 새 규칙으로 몰래 대체하지 않으며 기존 불일치 시 보류 동작을 유지합니다.

Work 브라우저의 실제 원고 품질·노출 성과에 미친 영향은 이 오프라인 검증의 확인 범위 밖입니다.

## 실제 최종 프롬프트 차이

동일 입력·기준일(2026-10-19)로 수정 전후 최종 프롬프트를 생성했습니다. 아래는 자율 정보글의 실제 diff입니다. 사전 수정은 한 줄 JSON의 rules를 줄 단위로 펼친 차이이며, 아래 해시는 펼치기 전 전체 input 문자열의 UTF-8 SHA-256입니다.

### writer

```diff
--- before
+++ after
@@ -181 +181 @@
-- 선우와 관련해 사용자가 실제로 입력한 질문에만 답한다. 자율 주제 선정 금지.
+- 원본 입력에 등록된 핵심 질문에만 답한다. 수동·자율 제작 구분은 입력 출처와 승인 전략을 따르며 원고 생성·검수 중 새 글감을 고르지 않는다.
@@ -190 +190 @@
-- 독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 목록은 자율 주제 선정·일일 배분·확정 일정 변경을 허용하지 않는다.
+- 독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 이 과거 목록은 현재 트렌드나 제작 승인 근거가 아니며 일일 배분·확정 일정을 바꾸지 않는다.
@@ -199,0 +200 @@
+- 자율 제작: 승인된 Work 슬롯에서 Creator Advisor 최신 유효 자료·네이버 검색 상위 본문3~5개·기존 글 중복 대조로 선정·등록한 질문에 답한다. 사용자 직접 질문이 아니라는 이유만으로 배제하지 않는다. 선정 근거가 없으면 보류하며 질문을 사용자 발언·개인 경험으로 꾸미지 않는다.
@@ -207 +208 @@
-육아는 입력된 실제 질문에 답한다. 선우의 월령·증상·경험을 추정하지 않는다.
+육아는 원본 입력에 등록된 핵심 질문에 답한다. 자율 선정 질문을 사용자 실제 발언으로 바꾸지 않는다. 선우의 월령·증상·경험을 추정하지 않는다.
```

### reviewer

```diff
--- before
+++ after
@@ -188 +188 @@
-- 선우와 관련해 사용자가 실제로 입력한 질문에만 답한다. 자율 주제 선정 금지.
+- 원본 입력에 등록된 핵심 질문에만 답한다. 수동·자율 제작 구분은 입력 출처와 승인 전략을 따르며 원고 생성·검수 중 새 글감을 고르지 않는다.
@@ -197 +197 @@
-- 독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 목록은 자율 주제 선정·일일 배분·확정 일정 변경을 허용하지 않는다.
+- 독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 이 과거 목록은 현재 트렌드나 제작 승인 근거가 아니며 일일 배분·확정 일정을 바꾸지 않는다.
@@ -206,0 +207 @@
+- 자율 제작: 승인된 Work 슬롯에서 Creator Advisor 최신 유효 자료·네이버 검색 상위 본문3~5개·기존 글 중복 대조로 선정·등록한 질문에 답한다. 사용자 직접 질문이 아니라는 이유만으로 배제하지 않는다. 선정 근거가 없으면 보류하며 질문을 사용자 발언·개인 경험으로 꾸미지 않는다.
```

### rewrite

```diff
--- before
+++ after
@@ -185 +185 @@
-- 선우와 관련해 사용자가 실제로 입력한 질문에만 답한다. 자율 주제 선정 금지.
+- 원본 입력에 등록된 핵심 질문에만 답한다. 수동·자율 제작 구분은 입력 출처와 승인 전략을 따르며 원고 생성·검수 중 새 글감을 고르지 않는다.
@@ -194 +194 @@
-- 독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 목록은 자율 주제 선정·일일 배분·확정 일정 변경을 허용하지 않는다.
+- 독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 이 과거 목록은 현재 트렌드나 제작 승인 근거가 아니며 일일 배분·확정 일정을 바꾸지 않는다.
@@ -203,0 +204 @@
+- 자율 제작: 승인된 Work 슬롯에서 Creator Advisor 최신 유효 자료·네이버 검색 상위 본문3~5개·기존 글 중복 대조로 선정·등록한 질문에 답한다. 사용자 직접 질문이 아니라는 이유만으로 배제하지 않는다. 선정 근거가 없으면 보류하며 질문을 사용자 발언·개인 경험으로 꾸미지 않는다.
```

### pre_review_correction

```diff
--- before
+++ after
@@ -1 +1 @@
-선우와 관련해 사용자가 실제로 입력한 질문에만 답한다. 자율 주제 선정 금지.
+원본 입력에 등록된 핵심 질문에만 답한다. 수동·자율 제작 구분은 입력 출처와 승인 전략을 따르며 원고 생성·검수 중 새 글감을 고르지 않는다.
@@ -10 +10 @@
-독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 목록은 자율 주제 선정·일일 배분·확정 일정 변경을 허용하지 않는다.
+독감예방접종·육아휴직 급여는 작성 당시 공식 대상·시기·조건을 확인한다. 원더윅스·초기이유식 식단표·ESL 수유자세·임신 극초기증상·착상혈·제대혈은 정확한 용어와 의료 근거를 확인한다. 브랜드 키워드는 실제 모델·상품 정보가 확인된 비교·정보 글에만 사용하며 구매·사용 후기를 창작하지 않는다. 연예인 임신·인스타와 육아 관련성이 불명확한 귀뚜라미 키워드는 유입 목적으로 자동 선정하지 않는다. 이 과거 목록은 현재 트렌드나 제작 승인 근거가 아니며 일일 배분·확정 일정을 바꾸지 않는다.
@@ -19 +19,2 @@
-모바일 짧은 문단·대소제목·구분선·핵심강조를 유지한다. 글자수보다 질문에 대한 답·재현가능성·시각증거·재사용자료를 우선하며 기존 사실성·안전성·품질 심사는 유지한다. 대표이미지는 전체 핵심을 보여주고 캡처 캡션에 무엇을 확인할지 설명한다. 개인정보는 가리고 외부화면/문서 출처를 남긴다. 캡처·사진은 재사용 권한과 허용 범위를 확인하고 필요한 부분만 사용한다. 권한 또는 필수 근거/캡처/링크가 확인되지 않으면 보류하며 실제 캡처를 생성 이미지로 대체하지 않는다.+모바일 짧은 문단·대소제목·구분선·핵심강조를 유지한다. 글자수보다 질문에 대한 답·재현가능성·시각증거·재사용자료를 우선하며 기존 사실성·안전성·품질 심사는 유지한다. 대표이미지는 전체 핵심을 보여주고 캡처 캡션에 무엇을 확인할지 설명한다. 개인정보는 가리고 외부화면/문서 출처를 남긴다. 캡처·사진은 재사용 권한과 허용 범위를 확인하고 필요한 부분만 사용한다. 권한 또는 필수 근거/캡처/링크가 확인되지 않으면 보류하며 실제 캡처를 생성 이미지로 대체하지 않는다.
+자율 제작: 승인된 Work 슬롯에서 Creator Advisor 최신 유효 자료·네이버 검색 상위 본문3~5개·기존 글 중복 대조로 선정·등록한 질문에 답한다. 사용자 직접 질문이 아니라는 이유만으로 배제하지 않는다. 선정 근거가 없으면 보류하며 질문을 사용자 발언·개인 경험으로 꾸미지 않는다.
```

## 전체 최종 input 해시

| 입력/형식 | 단계 | 수정 전 SHA-256 | 수정 후 SHA-256 |
|---|---|---|---|
| manual/article | writer | `d641d851e95a30d6d7f8a7874c7588e2e94b2872b307eb9494809476a690ec42` | `a79f0766f03b0e92ebedebfa5465686b34fcc29771ae6035cf9aef1590c85408` |
| manual/article | reviewer | `ee43beb2733ff3f138ec512ebe71f36b4ddd2cfb90ae3737d60bd9e314810e05` | `1a51c78f342a56f4e6a87067501a6f2019b72f1da7c4f16005d10d8a2ac02481` |
| manual/article | rewrite | `1dddf1dc9a7f7596abe2c2fedfb180b5a08dd87d5c476c6cbd45a5d8577ec3a0` | `0c25acc1885a5c64f45c3d6b7dc1c1830abc400c473a207a6eb4ba240eb28c42` |
| manual/article | pre_review_correction | `0f289963667709a410a46a9988649e40838cbaa6dcd1f0638f3458aca28e7e91` | `121f54520335791df374fd68775521b7b14f0964440fb488da7ec103334d2394` |
| manual/ai_tutorial | writer | `6eca4bd18025579ca27def79a7abbb5330294152a8e37dae5073c396fe572b7d` | `751254c651d6dcb6ce589ade89f6e7b114170cd8b68ae024f224d5cc3578fc8b` |
| manual/ai_tutorial | reviewer | `5b2401a05a26a3dec76601bf4ecd6f6d7debfb3851e4c498b9efee5c004eba6d` | `b0504b8ef69797dac8bdc2ee92d3aee1d6671e98807d92f90df232de86418e25` |
| manual/ai_tutorial | rewrite | `06c0ac99bda4f538c76c72c8426b5d482314a64542254535af03981be72a4dae` | `c3e6166362ecb92f5bf79a827ab745ec7bbb9b93050117f502e6e2e780ff1b6f` |
| manual/ai_tutorial | pre_review_correction | `f0c3bb8bd04e27c89d138fc1ffedb7477207cb043d0bc174093b8b51d5c4fd4f` | `e26a0fc4c2df90e3924b97d14076aa31e52581d1065524854c60f080c0c571ad` |
| manual/review | writer | `2a1d2e8f87756a17b385fceadd17a1c80eda94b24909652f6be2d6cf565e5b89` | `6747f6be41b77bd7889fca983f9594a06baf92c31302469caafdf4eb375c7937` |
| manual/review | reviewer | `5d637a24d9071c9c2da2828dfe7c6848a0be46f6193f92a90550b97446d65e87` | `9b2e4e5c2b1baf2a52ad14fb852e5d7635f94b73ca7d90630d2f68a2ab1c4dbc` |
| manual/review | rewrite | `54488bedc2346d03c8de281debe161e4f7e9cf8e4d2162eb0d5a78d36fe5e26c` | `5252b45fdf43002c8c8973621bd58d14c2f8650942de8a421c293055d37d56af` |
| manual/review | pre_review_correction | `ffa47e22d36d2277b77d7bbca6816912b7277e6492426afbed7142ff52ca1902` | `e675abb3282cfec8c0d235085acadab8f890ff5140d1694aa8f3b342f2914f1e` |
| autonomous/article | writer | `f779b245ffa677360323cc1b3d6cf8b35ec447cb439d2a5bc3968bc7b52f3f64` | `09f5974be1f78fc4d6b6bf55a78f792265ad9d0f39420806ca95145338f60012` |
| autonomous/article | reviewer | `947a2e026e482d359b858901575833f5bf8f1002c21b34fccb9309456f2eb5f2` | `db08a9053933a4bc795e34fdaa812634b9c91ea3d0d1285fb9013d9e670714b8` |
| autonomous/article | rewrite | `07fdbe480965d9b8c14f973e249076ab1da09615c8d81ff97027d7b2dbb3f332` | `38348ea442c2591420b804dace7d556c628e529b6394423eaf54922d21c7d595` |
| autonomous/article | pre_review_correction | `fcbedd905bdc89a8d599aae2c0d23aa4a95be01265154aac9672fbcc4a1dbb99` | `3be09c22041780233d1e58b2c6203dcc42463d0f7e39ae8ae48add3a354f8399` |
| autonomous/ai_tutorial | writer | `78d54da7d053c8da96ce09e6fd01c0bddc7074c5cf7f5b2075f12a23f6169845` | `1a7ac236afd93e8f7d00c20009af29a79ee38166e95cb3d47de99c13cdcb46f1` |
| autonomous/ai_tutorial | reviewer | `1637ae49dd59dbb3c1d519e52fc28b173c236b60c88a2cbdd26146f1b5cdd24b` | `a5a99c89c9ecdf7ba677db721241c0a993c0decbd681ba59eedcb039fb12dcf1` |
| autonomous/ai_tutorial | rewrite | `9876a8b7c218270fb4dcde1bf43e614de06aead273f5b35275ffbf117e44a949` | `5acfe910922fa3e2ace17d8134a1062d4779608622b41097072d29292bfc1bd0` |
| autonomous/ai_tutorial | pre_review_correction | `975b0ff3b03474365a9dd30abbabace142a3eb2992feeef4abfc974540dae6ec` | `91a957958a7e684e3801fa777e0f0ea49e04775890686908f9e42605fdfd8f9a` |
| autonomous/review | writer | `b6bea9e73eec0c1e547a76193de5a8badf0241b7a22871a36c00c2b80d635d56` | `c3c3b5716ab60dc5399c265a918df9e63cba9b68afae1323822beb4cbcdc8708` |
| autonomous/review | reviewer | `6c5ce47e134a4ed322c039cdafb8eb550932d1697a747198b2f2bc8e2987c94f` | `839faf7cc9e4d3cb84bf04ec526fe9bec86a515606e2cd42ed60786c58ed8725` |
| autonomous/review | rewrite | `46ee2213172c50be63c412a1245fdb9875c039aa1999abe7f4baa8a4d6362777` | `798c6e92df5d456be71bafddac5f732b94c5d5aaef2798b60eb43baf7b4cac0b` |
| autonomous/review | pre_review_correction | `08f835082e6c2915938afe9560fe99f18d6ee65719cc66dbf974135ce58366a8` | `64d52a2e0b8ff0dc8a5753359e5295eb77083f19d5d601e2d21f9d49be4ebfcc` |

## 재현

```bash
pip install -e ".[dev]"
ruff check src tests
pytest -q tests/test_parenting_prompt_routing.py
pytest -q
```

최종 input 원문은 테스트의 `capture_prompts(monkeypatch, settings, provenance, kind)` 반환값 중 각 call의 `input`입니다. 합성 질문·출처만 사용하며 실제 아동 정보·인증정보·개인 경험은 포함하지 않습니다.


## PR #50 최신 main 병합 검증 — 2026-10-18 22:49 KST 재검증

아래는 최초 검증 이후의 현재 병합 후보 결과다. 비교 기준은 최신 main `4d46fcc5c112831bcc48c926eeb2e259c6aea2c6`, PR 원래 head는 `e509783ffea636f1bc4483fbc04a4d65e5f70dfb`다. 최초 검증 표와 해시는 당시 이력으로 보존한다.

### 충돌 해결과 보존 범위

- 실제 `git merge --no-commit --no-ff origin/main`에서 충돌한 파일은 `docs/RESET_OPERATIONS.md` 하나다. PR의 육아 provenance별 범위 설명과 main의 사용자 직접 발행 문장을 함께 남겼다. 과거 자동 공개 범위 유지 문장은 복원하지 않았다.
- `editorial.py`는 자동 병합 후 diff를 검토했다. main의 `PUBLIC_COPY`(스마트에디터·직접 발행), `VOICE`(간결한 문체), `IMAGES`(일괄 이미지 장수 강제 해제), 각 유형별 `quality_guidance` 전체 문자열은 최신 main과 동일하다.
- 생성·검수·재작성·사전 수정에 출처 분기가 동일하게 전달된다. 수동·누락·구버전·알 수 없는 출처는 자율 권한을 얻지 않는다. 기존 회귀 테스트의 21개 출처/유형 조합에서 4단계 최종 input 모두에 최신 스마트에디터·직접 발행·중복 카드 방지 규칙이 남는지 검증을 추가했다.
- main의 디자인·작성·템플릿·실행계약 문서 및 HTML 템플릿은 그대로 보존했다. 원문/의료 안전·경험/월령 창작 금지, 입력 등록 슬롯, 저장/공개 승인·한도·중복/불확실 제출·예약·브라우저 점유 코드와 workflow는 변경하지 않았다.

### 실행 결과

| 검증 | 결과 |
|---|---|
| 최신 main 기준 전체 회귀 | 1,475 통과, 45.91초 |
| 충돌 해결 후 전체 회귀 | 1,505 통과, 45.91초 |
| `ruff check src tests`, `git diff --check` | 통과 |
| 실제 production 최종 input 비교 | 수동/자율 × 정보글/AI 튜토리얼/후기 × 생성/검수/재작성/사전 수정 = 24쌍 |
| 최신 공통·유형별 guidance / 호출 옵션 | 24쌍 모두 동일, 요청별 육아 범위만 수정 |
| 육아 rules 외 TOML / 연속120일 편성 | 최신 main과 동일 |

실행 환경: Python 3.12.14. 응답·원문 조회만 합성 fixture로 대체했다. 유료 모델/API·브라우저·콘텐츠 제작/임시저장/발행·예약 변경/재실행은 하지 않았다. 프롬프트 비교는 전달 문자열의 일치를 확인한 것이며 실제 모델 산출물 품질이나 Work 작업 완료를 입증하지 않는다.

### 최신 main과 해결 후보의 전체 input SHA-256

| 요청/유형 | 단계 | 최신 main | 해결 후보 |
|---|---|---|---|
| manual/article | writer | `fe5c035db7a0907a7874d3f9e2fa46b041b431406db53d9aa458bfcca1d7122d` | `3e36953b3756ca119bc6b321edc324a6e5adab5ef71c239115d89d7838804640` |
| manual/article | reviewer | `cbf80b128f8f7523b53cab205c6036f94e51a2d65045a706fd9bdb35aecc8255` | `eca270d3c1b2855456e33baf5b41dc1735240088ac48d6c49f955a5c8302a9a1` |
| manual/article | rewrite | `b3f7a3a0847cf8ef998cff7d2d4983b8e82b9fe1c0686c3f4be83489fc39c359` | `55c90a64c012853df9e12a769de70242f5ab746149888b3084fa8d54d9ae898d` |
| manual/article | pre_review_correction | `edc8bc601ca9e9cd4baaea9b48f9e7085b81b0d10d6234df106ae6aa93748f46` | `16ae13db603ad3af26fd3a6439c1369b7fcd1310b4e1802b1e6b085923fcaa3f` |
| manual/ai_tutorial | writer | `131b85402bce89219f4c613b2f1a3ee241369cbeee334b4b5acc2b2b4f79c56d` | `51dc504a45036b60cb91cbdc747ada916ac54cd12933fdedfff063c9fc6a79df` |
| manual/ai_tutorial | reviewer | `00c46fe4985ced2d848db2a9598d9d6465836c5ebdb863bd89909e26384256ac` | `743fe6f897abb7073e4cb15d619d1f79c2bc3940c95e054956529c80cc1d8c01` |
| manual/ai_tutorial | rewrite | `366e588bc7855f7846c08ce98824072d184a4accb4acaf2bd149c469efe99cea` | `d844a416cacda23c81cdbb6741d529f423b620bd95ad0b17efa3dfe3afb34da2` |
| manual/ai_tutorial | pre_review_correction | `2d5e112fd02c027e7499b14b0b3a7998ef777d148ceee79758fea802f3ddce2a` | `fd1415ae1cec245b4d72acf742392efb12af2a52016c48f0de944bbf7cfb570f` |
| manual/review | writer | `90ce34558d252d9ad2f842ffe466abefec417a78b6be1306e0b5dabe991dccf5` | `3b254a0f188155530055662d038fe0074f5238a5f1684b4f0158bde91431dea2` |
| manual/review | reviewer | `6856d77e238ff8f831dfd0af38f775f505d2ce4eaa3551c7ced1fd23f4a88b31` | `cf2d86254575b8c6933b8018dfabc8ab36074f60843812072c7b0b48b2c44153` |
| manual/review | rewrite | `2e3a5da829318fe861057fd8fa8fe10033a185edb3b6671f21936a81d234e9aa` | `16a9474280a661a68b381189cf988ea50805e057d068b0b16dba9144d17fa18d` |
| manual/review | pre_review_correction | `81f00d6afdc71303fc3b6ec76a155c72f6f4a5299e59cf73e406ffce91c3a131` | `377c24484f39d9e7cf79aa078fe446d93beb8f9075e51fb68e05169f0e0e087c` |
| autonomous/article | writer | `2c1325902af0def53e6e5516868dba9b996f9a751d2cc37bb10f515a5bece153` | `2f809b46236cbd675ae2b71b88bf172fc27c7df85d44429200262687c50c1011` |
| autonomous/article | reviewer | `cb07db7d56f976fa897f53d765fec4930750bb7b634f9b7075aafc6ccaf8eb41` | `c1291f529353577973fc7033f91ef3043aea17a3eb60215e0d84e79ac056d8d7` |
| autonomous/article | rewrite | `d47d777efa98b4cdd11dfa2e2b7f8b6f2c30b0d60e2aa9edb52d4bb07b6bf9ad` | `5e1d35f2733f40050243783ba931f5e95f578d28690e470d9ba79d95d1734abd` |
| autonomous/article | pre_review_correction | `a8c5d8dd9f71314d6e064590ab6f24e857f6547d9a9a7702401e147202f03af3` | `c34ab455f90561279936dc539db4d2e73f66f6fa29cd5cc39f46045252cd2b42` |
| autonomous/ai_tutorial | writer | `bde5eb46d1c92f0c4021ece5ced7f6583e2fca148bd34c5f23e247cae6d103eb` | `0daed2f912a4ad72dad560cd0191cae90c65285bc146cd329b38fcef00ee7b0f` |
| autonomous/ai_tutorial | reviewer | `c2aa621c083e3c53c743343b3fd583c576ef608f6fd691d65f094c4935b17514` | `0bb76e70736ac25ead2a055973647b1ebfd9ca154ab25b1690146109a02098f9` |
| autonomous/ai_tutorial | rewrite | `d548350a6c01a061a1a280fbe0ce0ac6b4e4e3557e4f93bf08e32226252a415e` | `71a79fa25369209da3cee87cd1a3323e1c7928c8e011c4fdc98c1ecfdde64237` |
| autonomous/ai_tutorial | pre_review_correction | `a43c7596ee2f42c3dfd10aff5fe8046e0f397f508706c35aabebdb35a878bc76` | `ba4bbb734db69b5d32489baf2b6e6ff178d7e20448be896b6b5d9cd126d2acde` |
| autonomous/review | writer | `87fa214baeff222f99eb4a196f0a0d69a502c7b55eccf393f2373f0b4fd14e8a` | `ed6ba51bb148110b1795b9d6ad00a7e6787030346bd084a37a67f5433ccc7b67` |
| autonomous/review | reviewer | `f6e4084326c95285078f98ecd1689158fede232b3faeebc932574a5785f054e3` | `fdcf4764382ed5a7485b59f68ac48b27c9f361d62d9094ab72e169d6a0c1ccdf` |
| autonomous/review | rewrite | `72462c7d4203815ed1d1837eb80a40a58e59307e164d929a444f3a7b7ee5c95f` | `886df0fd27aa03fb4157679f64de333bda7c3d69cf2b0d384b782bb5bec37d6b` |
| autonomous/review | pre_review_correction | `b78f7dbdf7e405059f2b2bc9174d1bcd171967dd23608617f762e8c6c64d9659` | `546780f1924e2ba01abaa589584260d786cf227616b0a7ea6620fb40aded07c5` |

### 운영 적용 범위

이 검증은 병합 후보 코드/지침과 프롬프트까지다. 병합 후 main을 새로 checkout하는 API 경로가 변경을 소비하며, Work의 실제 지침 재조회·실행 코드 pin 갱신/소비와 10/19 콘텐츠의 실제 저장·평가 결과는 별도 운영 증거가 필요하다. 이미 생성/검수된 packet의 입력 해시를 변경하거나 기존 작업·예약을 재실행하지 않는다. 현재 종료점은 임시저장→평가→수정→보고, 실제 공개는 사용자 직접 발행이다.


## 정적 프롬프트 잔여 충돌 후속 검증 — 2026-10-18 22:55 KST

PR #50은 `a4fa4b0047523dd230be627dbc19510b3a7bd215`로 병합됐으며 [PR CI](https://github.com/jinhoo-choi/naver_blog_agent/actions/runs/39133317830)와 [main CI](https://github.com/jinhoo-choi/naver_blog_agent/actions/runs/39133444944) 모두 1,505건·린트가 통과했다.

병합 후 전체 문장 의미를 추가 대조하면서, 최신 main의 `RESET_DESIGN`·`TEMPLATES`·동적 guidance가 가변 수량을 지정하는 반면 정적 `prompts/writer.md`·`reviewer.md`에는 본문삽화 최소2장·태그 정확히5개가 남아 있음을 확인했다. 후속 변경은 이 두 문장의 최신 기준 정렬과 기존 최종input 테스트의 검증 추가뿐이다.

- 일반 정보글 자료 수는 설명 역할에 따라 정한다. 개별 요청의 최소2장 등 명시 조건, 유래 썸네일, 후기/AI 원본 증거, 생성 상한·캐시·예산은 유지한다.
- 태그는 실제 주제에 맞는 소수(보통3~5개)다. 개수만으로 감점·재작성·유료 호출을 요구하지 않으며 무관/중복 태그 보완과 본문/JSON 분리는 유지한다.
- 새 회귀 assertion을 수정 전 코드에서 실행: 8실패·1통과(9개 유형). 수정 후 같은 9개 유형이 통과하며 각 유형의 생성·검수·재작성·사전수정 최종input을 검사한다.
- 수정 후 전체 1,505건 통과(36.26초), `ruff check src tests`·`git diff --check` 통과.
- 아래 24쌍 최종input을 다시 비교했다. 동적 guidance·전체 TOML·120일 편성·호출 옵션은 그대로다. 코드·예약·승인·중복방지·진행 중 작업/고정 packet은 변경하지 않았다. 실제 콘텐츠/모델/API/브라우저 실행은 없다.

| 요청/유형 | 단계 | PR #50 병합본 | 후속 해결 후보 |
|---|---|---|---|
| manual/article | writer | `3e36953b3756ca119bc6b321edc324a6e5adab5ef71c239115d89d7838804640` | `0c8c6a6370bbbfff16d03c97d66e76298cf806f2eb9765bd2c54e94ec19bf3d4` |
| manual/article | reviewer | `eca270d3c1b2855456e33baf5b41dc1735240088ac48d6c49f955a5c8302a9a1` | `78fe93b3c9879aaed9f6995753aa4fed594732c05d111ac95cd3b9585f125881` |
| manual/article | rewrite | `55c90a64c012853df9e12a769de70242f5ab746149888b3084fa8d54d9ae898d` | `e674aaf927fd67f6cbda05f711e43e27f8e2aa18319b80e7828f20c6f3b81cd1` |
| manual/article | pre_review_correction | `16ae13db603ad3af26fd3a6439c1369b7fcd1310b4e1802b1e6b085923fcaa3f` | `a48bd7938564680b9bb6d431c3f8756a285ceded2b75605d56b9e96439a07a68` |
| manual/ai_tutorial | writer | `51dc504a45036b60cb91cbdc747ada916ac54cd12933fdedfff063c9fc6a79df` | `3de06d82f72e5ce76169e60e57d5e55e2d0b8264693cfd9f4c5749b7a21faead` |
| manual/ai_tutorial | reviewer | `743fe6f897abb7073e4cb15d619d1f79c2bc3940c95e054956529c80cc1d8c01` | `a199dfd33da605c4d5b12f4d5328efa0b0351f6606e2015f98714df4c10aff79` |
| manual/ai_tutorial | rewrite | `d844a416cacda23c81cdbb6741d529f423b620bd95ad0b17efa3dfe3afb34da2` | `805411dd58162a6fbaa7792822a32984544ecb02ef52d4a85c76aa6e0edb7d84` |
| manual/ai_tutorial | pre_review_correction | `fd1415ae1cec245b4d72acf742392efb12af2a52016c48f0de944bbf7cfb570f` | `47a1ccf4e1bac600992be946cb2875ef05327fb98c5b29be5f2ddf62f4c7c2bb` |
| manual/review | writer | `3b254a0f188155530055662d038fe0074f5238a5f1684b4f0158bde91431dea2` | `d7853aa427a83c38fa19da16417b38eae70b099d7becc97cf80539f55b70e8da` |
| manual/review | reviewer | `cf2d86254575b8c6933b8018dfabc8ab36074f60843812072c7b0b48b2c44153` | `0394a224709888596c6c56682f25b0cc2eb586864dee08f564d491d064b1c094` |
| manual/review | rewrite | `16a9474280a661a68b381189cf988ea50805e057d068b0b16dba9144d17fa18d` | `76ca27398fd4cdfe1f8d19be82b5e5fc2df9739305b686d73e2cf50062a1674c` |
| manual/review | pre_review_correction | `377c24484f39d9e7cf79aa078fe446d93beb8f9075e51fb68e05169f0e0e087c` | `fe0977fe25e6982deb7bc581657b5372d632d6fe06c61a225562a6f1668fe1d8` |
| autonomous/article | writer | `2f809b46236cbd675ae2b71b88bf172fc27c7df85d44429200262687c50c1011` | `69710313d122bae59ffb3a22152e44c8054281a4b5756d00f91cc30dc18fd37c` |
| autonomous/article | reviewer | `c1291f529353577973fc7033f91ef3043aea17a3eb60215e0d84e79ac056d8d7` | `eb5e4114f0c12d93d351df92bd1c442fe745f79aa4b848936a7e745dacc34ad2` |
| autonomous/article | rewrite | `5e1d35f2733f40050243783ba931f5e95f578d28690e470d9ba79d95d1734abd` | `46b64ef9520a54566fcf9ef7105a54ca21e6ebc2dc0a9a9b9061599d3b471bab` |
| autonomous/article | pre_review_correction | `c34ab455f90561279936dc539db4d2e73f66f6fa29cd5cc39f46045252cd2b42` | `8a9c7f32d50bd597d8e9d85d8ce1d95feb0ff327183d0bb84b1a125e2baa7233` |
| autonomous/ai_tutorial | writer | `0daed2f912a4ad72dad560cd0191cae90c65285bc146cd329b38fcef00ee7b0f` | `a801e36f6d0570cf084439db20680dc32ac576df24571af2717688e8c8cbc9e2` |
| autonomous/ai_tutorial | reviewer | `0bb76e70736ac25ead2a055973647b1ebfd9ca154ab25b1690146109a02098f9` | `d70c82120a175d0000e908733a571544a08780b1494d897867885e17f7f155af` |
| autonomous/ai_tutorial | rewrite | `71a79fa25369209da3cee87cd1a3323e1c7928c8e011c4fdc98c1ecfdde64237` | `b740506dd762040db2ad23ad0fcc0e53f234ed9f22dfeae788a573db9244872d` |
| autonomous/ai_tutorial | pre_review_correction | `ba4bbb734db69b5d32489baf2b6e6ff178d7e20448be896b6b5d9cd126d2acde` | `afcc5280675fc4bd80e324d6700d93d2931768d9e9aa7c2d02716d1c28571db9` |
| autonomous/review | writer | `ed6ba51bb148110b1795b9d6ad00a7e6787030346bd084a37a67f5433ccc7b67` | `1bf87729a6dcdc6ec9b3caa96a869526a1b41c3cd1c29b91450ffcdf9c9e35e3` |
| autonomous/review | reviewer | `fdcf4764382ed5a7485b59f68ac48b27c9f361d62d9094ab72e169d6a0c1ccdf` | `1092f58f855a71af16771035597b01bf28085597e363067dda58463802962556` |
| autonomous/review | rewrite | `886df0fd27aa03fb4157679f64de333bda7c3d69cf2b0d384b782bb5bec37d6b` | `e738973a27d95d9dc045393dc5ebb3efb93cd954465dff365c0492ab375189cd` |
| autonomous/review | pre_review_correction | `546780f1924e2ba01abaa589584260d786cf227616b0a7ea6620fb40aded07c5` | `96c23b1999e3e6ef04ec190d9b595044d9d6c4d9ea5890b2d3843e986012a811` |
