"""Keep writer, reviewer and storage guidance aligned without paid generation."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('path', [
    'prompts/writer.md', 'prompts/reviewer.md',
    'docs/BLOG_SETUP.md', 'docs/OPERATIONS.md', 'docs/STATE.md',
])
def test_formal_style_keeps_quotations_and_safety(path):
    guidance = (ROOT / path).read_text(encoding='utf-8')
    assert '습니다체' in guidance
    assert '담백한 해요체' not in guidance
    for preserved in ['원문 인용', '실제 발언', '수유 중단·응급 대응·안전수면']:
        assert preserved in guidance


@pytest.mark.parametrize('name', ['writer', 'reviewer'])
def test_prompts_require_results_and_concrete_prose_without_new_certainty(name):
    prompt = (ROOT / 'prompts' / f'{name}.md').read_text(encoding='utf-8')
    for criterion in ['관찰 결과', '비교 대상', '한계를 같은 구역에서 한 번',
                      '방법입니다', '뜻합니다', '명사구', '구분해 읽어 주세요']:
        assert criterion in prompt
    assert '한계만' in prompt
    assert '치료 효과' in prompt
