"""Read-only Naver structure benchmark; no competitor prose is retained."""
from __future__ import annotations

import re
from dataclasses import replace
from urllib.parse import urlencode, urlsplit

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
                    stats = article.evaluate("""node => {
                        const text = node.innerText;
                        const paragraphs = [...node.querySelectorAll('.se-text-paragraph')];
                        return {
                            chars: text.length,
                            images: node.querySelectorAll('.se-component.se-image').length,
                            quote_headings: node.querySelectorAll('.se-component.se-quotation').length,
                            section_titles: node.querySelectorAll('.se-component.se-sectionTitle').length,
                            short_emphasis: paragraphs.filter(p => p.innerText.length < 70 &&
                                p.querySelector('b,strong')).length,
                            tables: node.querySelectorAll('table').length,
                            has_faq: /FAQ|자주.*질문|궁금한.*질문/i.test(text),
                            has_checklist: /체크리스트|체크.*포인트|확인할.*항목/.test(text)
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
            "records": records, "rule": "Structure only; verify claims with primary sources"}


def prepare_request(settings, request):
    if not settings.config.get("editorial", {}).get("enabled", True):
        return request
    query = public_query(request)
    benchmark = collect_benchmark(query, settings.naver_profile_dir)
    return replace(request, provenance={**request.provenance, "benchmark": benchmark})
