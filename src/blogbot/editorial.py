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
    'investment': '''투자: 무슨 사건이 어느 단계까지 진행됐는지, 공식 근거와 해당
기업의 사업/실적 연결이 어디까지 확인됐는지, 남은 조건·일정을 분리한다.
발간/공시일과 사건 시점으로 최신성을 확인하며 수집일로 대체하지 않는다.
sector_only는 종목을 억지로 연결하지 않는다. 원문에 없는 수혜·매수 추천을 만들지 않는다.
시각 자료는 실제 사건/사업 구조의 관계를 설명하고 상징적인 칩·화살표를 반복하지 않는다.
숫자와 비교는 검증된 텍스트/표에 남기고 삽화로 통계나 주가 전망을 만들어내지 않는다.''',
    'ai_tutorial': '''AI 튜토리얼: 실제 제공된 환경/버전·설정값·입력·실행 순서와
확인된 결과·실패 조건·다시 따라 할 절차를 연결한다. 실행 기록이 없으면 미검증 제안으로
명시하고 실행 성공·개선률·시간 절감을 지어내지 않는다. 실제사용본과 수정제안본을 구분한다.
화면은 실제 캡처만 증거로 쓰며 장식 삽화나 가짜 화면을 실행 증거로 쓰지 않는다.
개인정보·키·토큰은 가린다. 재현에 필요한 공개 설정만 남긴다.''',
}


def quality_guidance(category: str, editorial_type: str | None = None) -> str:
    """Select only the relevant form; unknown categories retain common safeguards."""
    key = 'ai_tutorial' if category == 'parenting' and editorial_type == 'ai_tutorial' else category
    return COMMON + '\n' + TOPICS.get(key, '')


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


def routed_prompt(prompt: str, category: str, editorial_type: str | None = None) -> str:
    """Only AI-family subtype removes irrelevant age-table requirements; legacy bytes stay exact."""
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


def routed_category_info(info: dict, category: str, editorial_type: str | None = None) -> dict:
    if category != 'parenting' or editorial_type != 'ai_tutorial':
        return info
    rules = [r for r in info.get('rules', []) if not r.startswith(
        ('질문 → 핵심 답변 →', '월령이 입력되지 않았으면'))]
    rules.append('가족 AI 튜토리얼은 육아 슬롯으로 세되 실제 환경·설정·입력·결과·실패·재현 절차를 따른다. '
                 '관련 없는 의료 월령·진료 목차를 강제하지 않는다. 사용자가 고른 실제 질문과 '
                 '제공/확인된 기록만 쓰며 새 영상·개인 경험을 만들지 않는다.')
    return {**info, 'rules': rules}
