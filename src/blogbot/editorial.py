"""Shared editorial contract; hints inform the existing reviewer, never grade facts."""
from __future__ import annotations

import re

VERSION = 'topic-quality-v1'

COMMON = '''주제별 품질 기준 topic-quality-v1:
첫 문단에서 주 질문에 답한다. 자연스럽고 자신 있는 습니다체를 일관되게 쓰되
원문 인용·실제 발언은 보존한다. 짧은 의미 단위 문단과 빈 줄, ## 구역을 유지한다.
단어 중간 줄바꿈·글자 수 맞춤 개행을 하지 않는다. 실제 구분선은 렌더러/Work가 넣는다.
같은 결론·주의를 도입/본문/마무리에 복제하지 않는다. 중복 면책은 관련 근거 구역에
모으되 수유 중단·응급 대응·안전수면 등 서로 다른 안전 행동과 단계별 조건은 남긴다.
일반 자료를 단순 나열하지 말고 독자의 상황에 필요한 조건 차이·자료 비교·적용 판단을
설명한다. 이것이 작성자의 분석이면 '자료를 비교하면'처럼 사실과 해석의 경계를 밝힌다.
경험은 사용자가 제공하거나 실제 확인한 범위만 쓴다. 개인 사용·효과·정량 결과를
창작하지 않는다. 경험이 없으면 출처 비교/분석으로 설명하고 경험 부족 자체를 감점하지 않는다.
주제별 항목은 충실도 기준이지 고정 목차가 아니다. 모든 글에 같은 FAQ·결론·주의 틀을
강제하지 않는다. 메타 설명·상투적 대비·상투적 진행 문구 대신 구체적인 사실과 행동을 쓴다.
기존 출처·안전·길이·매일 심화·이미지 수·AI 표시 규칙과 점수/통과 기준은 유지한다.
'''

TOPICS = {
    'parenting': '''육아: 실제 양육 상황의 질문 → 적용 월령/조건 → 따라 할 순서와
관찰할 신호를 설명한다. 사용자 경험과 연구 결과를 구별하고 해당 조건의 예외를 남긴다.
시각 자료는 설명할 자세·환경·물건의 차이가 보이게 계획한다. 안전하지 않은 행동을
장식 삽화로 정상화하지 않는다. 근거 없는 나이·각도·시간·반응을 채우지 않는다.''',
    'exercise': '''운동: 준비와 수행 순서, 올바른 자세와 흔한 오류의 차이를 설명한다.
발/손 접점·관절 방향·몸통 위치 등 공식 원문에서 확인된 구체적인 관찰 지점을 제시한다.
시각 자료는 같은 동작의 맞는 자세/잘못된 자세를 구별할 단서를 보여주되 검증되지 않은
자세를 생성해 정답으로 제시하지 않는다. 도해가 불확실하면 공식 동작 자료를 사용하고
실제 자세 확인은 Work에서 한다. 통증·저림의 중단 신호와 안전장치 조건을 보존한다.''',
    'investment': '''투자: 금요일 daily_plan.investment_mode=weekly-policy-v1이면 최근 5 calendar days,
D-4~D0의 새 정책 발표/새 단계 1건을 다룬다. KIS policy 적격 원본과 결합된 검증 근거만
사용하며 research·disclosure로 대체하지 않는다. 당일 07:00 KST 이후 최신 export 준비는
별도 확인하고 가격·시세·수급 D-1/D0, 기존 원본 점수·fatal·하루 1편 한도를 유지한다.
무엇이 실제로 바뀌었는지, proposed/announced/adopted/effective 중 어느 단계인지,
공식 참여(official_participation)·이전 사업(previous_project)·사업 관련성(business_relevance)을
나누고 기업의 실적 연결에 남은 조건·일정을 분리한다. 이전 참여나 관련 사업만으로 이번
선정·수주·수혜·매출을 확정하지 않는다. 날짜 미정은 미정으로 두며 미래 일정으로 최신성을
만들지 않는다. 발간/공시일과 사건 시점을 보존하고 수집일로 대체하지 않는다.
sector_only의 게시판 배정 종목은 복원하지 않는다. 별도 기업 연결은 독립된 공식 근거가
있을 때만 쓴다. 근거 JSON·URL 목록만으로 원문을 읽었거나 사실이 검증됐다고 주장하지 않는다.
시각 자료는 실제 사건/사업 구조의 관계를 설명하고 상징적인 칩·화살표를 반복하지 않는다.
숫자와 비교는 검증된 텍스트/표에 남기고 삽화로 통계나 주가 전망을 만들어내지 않는다.''',
    'ai_tutorial': '''AI 튜토리얼: 실제 제공된 환경/버전·설정값·입력·실행 순서와
확인된 결과·실패 조건·다시 따라 할 절차를 연결한다. 실행 기록이 없으면 미검증 제안으로
명시하고 실행 성공·개선률·시간 절감을 지어내지 않는다. 실제사용본과 수정제안본을 구분한다.
화면은 실제 캡처만 증거로 쓰며 장식 삽화나 가짜 화면을 실행 증거로 쓰지 않는다.
개인정보·키·토큰은 가린다. 재현에 필요한 공개 설정만 남긴다.''',
}


REVIEW_VERSION = 'review-editorial-v1'
REVIEW = '''후기 전용 편집 기준 review-editorial-v1:
명시적으로 content_style=review인 실제 사용·제품·행사 후기만 이 기준을 적용한다.
기존 출처·안전·검수 기준은 유지하되 관련 없는 월령표·의료 목차를 강제하지 않는다.
사용자가 공개 반영을 확인한 경험만 해당 사진·캡션과 연결한다. 모델은 사진을 직접
본 것이 아니므로 캡션 밖 모습을 관찰했다고 쓰지 않는다. 설치 시간·사용 결과·가족 반응·
가격·단점을 창작하지 않는다. 가격 생략 요청은 구매 카드·사진·캡션에도 적용한다.
짧고 자연스러운 습니다체 도입과 기존 글꼴을 유지한다. 한 문단은 한 요점, 1~3문장으로
나누고 의미별 빈 줄을 둔다. 정보는 왼쪽 정렬, 짧은 개인 소감만 선택적으로 가운데 정렬,
강조색은 한 가지로 절제한다. 실제 기본 실선은 Work/렌더러가 넣고 문자 ---로 대신하지 않는다.
실제 소유자 사진을 먼저 쓴다. 대표·착용 사진은 크게, 관련 포장·소품은 모바일에서 읽힐
때만 네이티브 2열로 묶는다. 판매자 구성표·긴 캡처는 실제 사용 사진 뒤 필요한 부분만
출처와 함께 쓴다. section_heading이 제공되면 일치하는 구역에서 해당 경험을 설명한다.
후기는 제공 사진을 쓰며 생성 삽화를 실사용 증거로 대신하거나 고정 총장수를 채우지 않는다.
내돈내산은 사용자의 자비 구매·리뷰 대가 없음 확인과 네이버에서 실제 선택 가능한 적격
구매/이용 내역이 모두 있을 때만 Work가 네이티브 인증 컴포넌트를 검토한다. 카드의 가격·
개인정보 노출을 먼저 확인한다. 배너를 이미지/문자로 흉내 내거나 네이버가 품질·효과를
검증했다고 주장하지 않는다. 인증 불가/미확인/미첨부만으로 글을 탈락시키지 않는다.
새 구매 연결·개인정보 동의·지속 접근 변경은 하지 않고 필요하면 수동 확인을 요청한다.
본문에는 태그를 쓰지 않는다. 관련 태그는 보통 3~5개·최대 8개인 기존 기본값을 유지하고
네이티브 태그와 본문에 중복 삽입하지 않는다. 명시적 글별 지시는 Work에서 별도 대조한다.
업로드 전에 Work가 실물의 전체 배송 라벨·개인정보 가림을 확인한다. 편집기는 자동 저장될
수 있으므로 임시저장 버튼 직전까지 미루지 않는다. 저장 전에는 로딩·순서·인접 설명·카드·문단·태그 중복을
확인한다. 저장 후에는 완료 표시와 목록만 확인하며 자동 재열기 검증을 추가하지 않는다.
현재 단계는 텍스트 작성/심사다. 네이티브 인증·사진 배치·서식·가림을 이미 확인했다고
말하지 않는다. 위 편집 항목은 기존 가독성/정확성/안전성 심사에서 다루며 추가 호출이나
새 점수 문턱을 만들지 않는다. 일반 정보 글에 후기 형식이나 개인 경험을 강제하지 않는다.
'''


def content_style(category: str, data: dict) -> str:
    """Explicit owner metadata only; never infer reviews from keyword matches."""
    style = data.get('content_style', 'article')
    if style not in ('article', 'review') or (style == 'review' and (
            category not in {'parenting', 'exercise', 'cooking'}
            or data.get('editorial_type', 'article') != 'article')):
        raise ValueError('Unsupported content style')
    return style


def quality_guidance(category: str, editorial_type: str | None = None,
                     style: str | None = None) -> str:
    """Select only the relevant form; unknown categories retain common safeguards."""
    key = 'ai_tutorial' if category == 'parenting' and editorial_type == 'ai_tutorial' else category
    selected = content_style(category, {'content_style': style or 'article',
                                      'editorial_type': editorial_type or 'article'})
    if category == 'origins':
        from .origins import GUIDANCE
        return GUIDANCE
    return COMMON + '\n' + (REVIEW if selected == 'review' else TOPICS.get(key, ''))


def editorial_hints(body: str) -> list[str]:
    """Conservative mechanical observations, not medical/factual or quality verdicts."""
    hints = []
    paragraphs = [re.sub(r'\s+', ' ', p).strip() for p in re.split(r'\n\s*\n', body)]
    seen = set()
    for paragraph in paragraphs:
        if len(paragraph) >= 30 and not paragraph.startswith(('#', '>', '|')):
            if paragraph in seen:
                hints.append('동일 문단 반복: ' + paragraph[:120]
                             + ' (단계별 필수 안전 조건인지 확인 후 중복만 정리)')
            seen.add(paragraph)
    if re.search(r'[가-힣]\n[가-힣]', body):
        hints.append('문장 내부 단일 개행이 있습니다. 단어 분리/기계적 개행인지 문맥으로 확인하세요.')
    for phrase in ['살펴보도록 하겠습니다', '구분해 읽어 주세요', '써누애비는', '써누애비가']:
        if phrase in body:
            hints.append(f'상투적/제3자 표현 후보: {phrase} (인용이면 보존)')
    return list(dict.fromkeys(hints))


def routed_prompt(prompt: str, category: str, editorial_type: str | None = None,
                  style: str | None = None, *, role: str = 'writer') -> str:
    """Route explicit styles without changing ordinary article prompt bytes."""
    if category == 'origins':
        from .origins import PLAN, REVIEWER, WRITER
        return PLAN + (REVIEWER if role == 'reviewer' else WRITER)
    if content_style(category, {'content_style': style or 'article',
                               'editorial_type': editorial_type or 'article'}) == 'review':
        return '\n'.join(
            ('후기는 제공된 사진·경험을 사용하며 실제 배치와 내돈내산은 '
             'docs/REVIEW_EDITORIAL.md에 따라 Work가 확인한다. 고정 이미지 총수를 요구하지 않는다.'
             if line.startswith('생성 이미지 기준은') else
             '후기 비교표는 실제 제공된 차이만 2~3열로 정리하며 관련 없는 월령표를 강제하지 않는다.'
             if line.startswith('- 개월수별 특징') else
             '후기는 실제 질문·제공 경험에 답하며 아이 실명·사적 병력은 공개하지 않는다. '
             '건강·안전 주장이 있는 구역에는 기존 공식 근거·적용 조건 검증을 유지한다.'
             if line.startswith(('- 육아는 실제 질문에 먼저', '육아는 정확한 월령이 없을 때'))
             else '운동 동작·사용법을 실제로 다루는 후기 구역에는 다음 안전 검증을 적용한다: ' + line
             if line.startswith(('- 운동은 선택된 운동의', '운동은 의미·주요 근육'))
             else line) for line in prompt.splitlines())
    if category != 'parenting' or editorial_type != 'ai_tutorial':
        return prompt
    output = []
    for line in prompt.splitlines():
        if line.startswith('- 개월수별 특징'):
            output.append('- 설정·입력·결과·실패 조건 비교는 확인된 정보만 2~3열 표로 정리한다. '
                          '자료에 없는 설정·숫자를 채우지 않는다.')
        elif line.startswith(('- 육아는 실제 질문에 먼저', '육아는 정확한 월령이 없을 때')):
            output.append('가족 AI 튜토리얼은 실제 질문과 검증된 제작 조건에 답한다. '
                          '아이 실명·병력·개인정보는 공개하지 않고 관련 없는 월령 표를 강제하지 않는다.')
        else:
            output.append(line)
    return '\n'.join(output)


def routed_category_info(info: dict, category: str, editorial_type: str | None = None,
                         style: str | None = None) -> dict:
    if content_style(category, {'content_style': style or 'article',
                               'editorial_type': editorial_type or 'article'}) == 'review':
        rules = [r for r in info.get('rules', []) if not r.startswith(
            ('질문 → 핵심 답변 →', '월령이 입력되지 않았으면'))]
        if category == 'exercise':
            rules = [('운동 동작·사용법을 실제로 다룰 때: ' + r if r.startswith(
                ('공식 운동기관·학회·병원 출처로 운동의', '보조자와 안전장치')) else r)
                for r in rules]
        rules.append('후기는 제공 경험과 실제 사진을 우선하고 관련 없는 월령/의료/운동 목차를 '
                     '강제하지 않는다. 실제 건강·안전 주장의 근거와 적용 조건은 그대로 검증한다.')
        return {**info, 'rules': rules}
    if category != 'parenting' or editorial_type != 'ai_tutorial':
        return info
    rules = [r for r in info.get('rules', []) if not r.startswith(
        ('질문 → 핵심 답변 →', '월령이 입력되지 않았으면'))]
    rules.append('가족 AI 튜토리얼은 육아 슬롯으로 세되 실제 환경·설정·입력·결과·실패·재현 절차를 따른다. '
                 '관련 없는 의료 월령·진료 목차를 강제하지 않는다. 사용자가 고른 실제 질문과 '
                 '제공/확인된 기록만 쓰며 새 영상·개인 경험을 만들지 않는다.')
    return {**info, 'rules': rules}
