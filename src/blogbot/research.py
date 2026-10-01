"""Read-only Naver structure/style benchmark; no competitor prose is retained."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from .core import today_kst


class ResearchRequired(RuntimeError):
    pass


def canonical_blog_url(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"blog.naver.com", "m.blog.naver.com"}:
        return None
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/(\d+)/?", parsed.path)
    return f"https://blog.naver.com/{match[1]}/{match[2]}" if match else None


def public_query(request) -> str:
    query = request.data.get("benchmark_query", "")
    if not query:
        if request.category == "investment":
            query = "주식 거래량 증가 주가 하락" if request.data.get("kind") == "flow" else "주식 공시 분석"
        elif request.category == "cooking":
            query = str(request.data["name"]) + " 레시피"
        else:
            question = request.data.get("question", "")
            for word, safe in [("목튜브", "아기 목튜브 안전 사용시기"),
                               ("호명", "아기 호명반응 시기")]:
                if word in question:
                    query = safe
                    break
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 80:
        raise ResearchRequired("Provide a public benchmark_query without names or medical history")
    return query.strip()


def collect_benchmark(query: str, profile_dir: str, target: int = 7) -> dict:
    target = max(5, min(10, target))
    records = []
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile_dir, channel="chrome", headless=False)
        try:
            search = context.new_page()
            search.goto("https://search.naver.com/search.naver?" + urlencode({
                "where": "blog", "query": query,
            }), wait_until="domcontentloaded")
            search.locator('a[href*="blog.naver.com/"]').first.wait_for(timeout=20000)
            links = search.locator('a[href*="blog.naver.com/"]').evaluate_all(
                "nodes => nodes.map(a => a.href)"
            )
            urls = list(dict.fromkeys(u for link in links if (u := canonical_blog_url(link))))
            for rank, url in enumerate(urls[:10], 1):
                page = context.new_page()
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    editor = page.frame(name="mainFrame") or page
                    article = editor.locator('.se-main-container, #postViewArea').first
                    article.wait_for(timeout=15000)
                    stats = article.evaluate(r"""node => {
                        const text = node.innerText;
                        const paragraphs = [...node.querySelectorAll('.se-text-paragraph')];
                        const blocks = paragraphs.map(p => p.innerText.trim()).filter(Boolean);
                        const bodyBlocks = blocks.length ? blocks : text.split(/\n+/).map(s => s.trim()).filter(Boolean);
                        const sentences = text.split(/[.!?。\n]+/).map(s => s.trim()).filter(Boolean);
                        const meanLength = items => items.length ?
                            Math.round(items.reduce((sum, s) => sum + s.length, 0) / items.length) : 0;
                        return {
                            chars: text.length,
                            images: node.querySelectorAll('.se-component.se-image').length,
                            quote_headings: node.querySelectorAll('.se-component.se-quotation').length,
                            section_titles: node.querySelectorAll('.se-component.se-sectionTitle').length,
                            short_emphasis: paragraphs.filter(p => p.innerText.length < 70 &&
                                p.querySelector('b,strong')).length,
                            tables: node.querySelectorAll('table').length,
                            has_faq: /FAQ|자주.*질문|궁금한.*질문/i.test(text),
                            has_checklist: /체크리스트|체크.*포인트|확인할.*항목/.test(text),
                            style: {
                                basis: 'heuristic_counts_not_prose',
                                paragraph_count: bodyBlocks.length,
                                mean_paragraph_chars: meanLength(bodyBlocks),
                                mean_sentence_chars: meanLength(sentences),
                                yo_ending_count: sentences.filter(s => /요$/.test(s)).length,
                                formal_ending_count: sentences.filter(s => /(?:습니다|입니다|합니다)$/.test(s)).length,
                                first_person_mentions: (text.match(/(?:^|\s)(?:저는|제가|저희는|저희가|나는|내가|우리는|우리가)(?=\s|[,.!?])/g) || []).length,
                                question_mark_count: (text.match(/\?/g) || []).length,
                                opening_has_question: /\?/.test(bodyBlocks.slice(0, 2).join(' ').slice(0, 250))
                            }
                        };
                    }""")
                    if stats["chars"] >= 300:
                        records.append({"search_position": rank, "url": url, **stats})
                except PlaywrightError:
                    pass
                finally:
                    page.close()
                if len(records) >= target:
                    break
        finally:
            context.close()
    if len(records) < 5:
        raise ResearchRequired("Fewer than five readable results; benchmark manually before retry")
    return {"query": query, "date": today_kst().isoformat(), "basis": "search_exposure_not_views",
            "records": records, "rule": "Structure and aggregate style traits only; no copied prose or experiences; verify claims with primary sources"}


def prepare_request(settings, request):
    request = prepare_primary_evidence(settings, request)
    if not settings.config.get("editorial", {}).get("enabled", True):
        return request
    query = public_query(request)
    if settings.config.get("editorial", {}).get("benchmark_mode") == "api":
        from openai import OpenAI

        from .llm import _extract_urls
        from .responses import BENCHMARK_SCHEMA, request_json
        data, response = request_json(
            OpenAI(api_key=settings.openai_api_key, timeout=240, max_retries=0),
            model=settings.openai_model, stage="benchmark", request_id=request.id,
            schema=BENCHMARK_SCHEMA, journal=settings.db_path.parent / "usage.jsonl",
            max_output_tokens=6000, retry_output_tokens=10000, reasoning={"effort": "low"},
            max_tool_calls=5,
            tools=[{"type": "web_search"}], tool_choice="required",
            include=["web_search_call.action.sources"],
            input=f"네이버 블로그 검색어: {query}. 상단 노출 글 5개를 찾아 실제 읽을 수 있는 것만 "
                  "구조·문체·제목·도입을 비교하라. 조회수 순위로 부르지 말라. 원문 인용·개인 경험은 "
                  "보관하지 말라. 열 수 없으면 기록 수 0과 한계를 적어라. JSON만 출력: "
                  '{"records":[{"url":"실제로 연 글 URL","observations":["짧은 구조 관찰"]}],'
                  '"limitations":"접근 한계"}',
        )
        observed = set(_extract_urls(response))
        records = [r for r in data.get("records", []) if isinstance(r, dict)
                   and r.get("url") in observed and canonical_blog_url(r.get("url", ""))]
        benchmark = {"query": query, "date": today_kst().isoformat(), "records": records[:10],
                     "basis": "api_search_observations_not_views",
                     "limitations": data.get("limitations", ""), "read_count": len(records[:10])}
        return replace(request, provenance={**request.provenance, "benchmark": benchmark})
    benchmark = collect_benchmark(query, settings.naver_profile_dir)
    return replace(request, provenance={**request.provenance, "benchmark": benchmark})


class _DisclosureText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'tr', 'p', 'br', 'div'}:
            self.parts.append('\n')
        elif tag in {'td', 'th'}:
            self.parts.append(' | ')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _dart_html(url):
    with urlopen(Request(url, headers={'User-Agent': 'naver-blog-agent/0.5'}), timeout=30) as response:
        if urlsplit(response.url).hostname != 'dart.fss.or.kr':
            raise ResearchRequired('Unexpected disclosure redirect')
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ResearchRequired('Disclosure size limit exceeded')
    # DART uses UTF-8 for the shell and EUC-KR for older HTML report viewers.
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return data.decode('cp949')


def prepare_primary_evidence(settings, request):
    url = request.data.get('src', '')
    parts = urlsplit(url)
    if request.category != 'investment' or parts.hostname != 'dart.fss.or.kr':
        return request
    numbers = parse_qs(parts.query).get('rcpNo', [])
    if (parts.scheme != 'https' or parts.path != '/dsaf001/main.do'
            or len(numbers) != 1 or not re.fullmatch(r'\d{14}', numbers[0])):
        raise ResearchRequired('Invalid disclosure identity')
    number = numbers[0]
    folder = settings.db_path.parent / 'source-evidence'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (hashlib.sha256(url.encode()).hexdigest() + '.json')
    if path.exists():
        evidence = json.loads(path.read_text())
    else:
        shell = _dart_html(url)
        match = re.search(r'viewDoc\(\s*["\']' + number
                          + r'["\']\s*,\s*["\'](\d+)["\']\s*,\s*["\']0["\']', shell)
        if not match:
            raise ResearchRequired('Disclosure report body unavailable')
        viewer = 'https://dart.fss.or.kr/report/viewer.do?' + urlencode({
            'rcpNo': number, 'dcmNo': match[1], 'eleId': '0', 'offset': '0',
            'length': '0', 'dtd': 'HTML',
        })
        parser = _DisclosureText()
        parser.feed(_dart_html(viewer))
        text = '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines()
                         if line.strip())
        if not 300 <= len(text) <= 30000:
            raise ResearchRequired('Disclosure report text incomplete')
        evidence = {'url': url, 'viewer_url': viewer, 'text': text,
                    'sha256': hashlib.sha256(text.encode()).hexdigest(),
                    'retrieved_date': str(today_kst())}
        from .images import atomic_json
        atomic_json(path, evidence)
    return replace(request, provenance={**request.provenance, 'primary_evidence': evidence})

