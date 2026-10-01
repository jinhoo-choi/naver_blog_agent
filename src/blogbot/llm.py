from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from openai import OpenAI

from .core import PostDraft, today_kst
from .inputs import ContentRequest
from .responses import DRAFT_SCHEMA, REVIEW_SCHEMA, _get, request_json


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_from_text(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise TypeError("LLM response must be a JSON object")
    return value


def _extract_urls(response) -> list[str]:
    found: list[str] = []
    for item in _get(response, 'output', []) or []:
        action = _get(item, "action")
        if (_get(item, "status") == "completed" and _get(action, "type") in {'open_page', 'find_in_page'}
                and _get(action, "url")):
            found.append(_get(action, "url"))
        for source in _get(action, "sources", []) or []:
            url = _get(source, "url")
            if url and url not in found:
                found.append(url)
        for part in _get(item, "content", []) or []:
            for ann in _get(part, "annotations", []) or []:
                url = _get(ann, "url")
                if url and url not in found:
                    found.append(url)
    return found


def _source_identity(url: str) -> str:
    """Ignore known tracking only; keep document IDs and all other query parameters."""
    parts = urlsplit(url)
    # Search results sometimes encode a tracking query into the final path segment.
    path = re.sub(r'/%3f(?:srsltid|gclid|fbclid|utm_[a-z_]+)%3d[^/]*$', '/',
                  parts.path, flags=re.IGNORECASE)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if not (key.lower().startswith('utm_') or key.lower() in {'gclid', 'fbclid', 'srsltid'}
                     or (not key and value.startswith('___psv__')))]
    # Site-specific display/cache switches; never discard arbitrary document parameters.
    query = [(key, value) for key, value in query if not (
        (parts.hostname == 'www.healthychildren.org' and key == 'form'
         and value == 'HealthyChildren')
        or (parts.hostname == 'www.healthychildren.org' and key == 'keyword')
        or (parts.hostname == 'acsm.org' and key == 'nocache' and value.isdigit())
        or (parts.hostname == 'publications.aap.org' and key == 'autologincheck'
            and value.startswith('redirected'))
        or (parts.hostname == 'www.mayoclinic.org' and key in {'p', 'pg'} and value == '1')
    )]
    # AAP article IDs survive DOI/volume routes and title-slug redirects.
    article = re.fullmatch(r'/pediatrics/article/(?:doi/10\.1542/[^/]+|\d+/\d+/[^/]+)/(\d+)/[^/]+/?', path)
    if parts.hostname == 'publications.aap.org' and article:
        path = '/pediatrics/article/id/' + article[1]
    # Verified Naver redirect on 2026-09-29; never equate different security codes.
    if (parts.scheme == 'https' and parts.netloc == 'finance.naver.com'
            and parts.path == '/item/main.naver' and len(query) == 1
            and query[0][0] == 'code' and re.fullmatch(r'\d{6}', query[0][1])):
        return f'https://stock.naver.com/domestic/stock/{query[0][1]}/price'
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), parts.fragment))


def _source_urls(payload: dict, observed: list[str], required: bool = True) -> list[str]:
    claimed = payload.get("source_urls", [])
    if not isinstance(claimed, list) or (required and not claimed):
        raise ValueError("Writer returned no sources")
    observed_ids = {_source_identity(url) for url in observed}
    if any(not isinstance(url, str) or _source_identity(url) not in observed_ids for url in claimed):
        raise ValueError("Source URL was not present in web-search results")
    body_urls = re.findall(r"https?://[^\s<>\]\)]+", payload.get("body", ""))
    if any(_source_identity(url.rstrip('.,')) not in observed_ids for url in body_urls):
        raise ValueError("Body contains an unverified URL")
    return list(dict.fromkeys(claimed))[:10]


def _checked_review(payload, response, request):
    if request.category == 'cooking' or not payload:
        return payload
    checks = payload.get('source_checks', [])
    evidence = request.provenance.get('primary_evidence', {})
    read_urls = [_get(_get(item, 'action'), 'url')
                 for item in _get(response, 'output', []) or []
                 if _get(item, 'status') == 'completed'
                 and _get(_get(item, 'action'), 'type') in {'open_page', 'find_in_page'}]
    verified = {_source_identity(u) for u in read_urls if u}
    if evidence:
        verified.update(_source_identity(evidence[k]) for k in ['url', 'viewer_url'])
    verified.update(_source_identity(e['url'])
                    for e in request.provenance.get('reference_evidence', []) if e.get('text'))
    if (not checks or any(check.get('status') != 'SUPPORTED'
            or not check.get('evidence', '').strip()
            or _source_identity(check.get('source_url', '')) not in verified for check in checks)):
        payload['decision'] = 'REWRITE'
        payload['blocking_issues'] = payload.get('blocking_issues', []) + [
            '핵심 주장별 원문 열람·근거 대조가 완료되지 않았습니다.']
        payload['rewrite_instructions'] = (payload.get('rewrite_instructions', '')
                                           + '\n실제 원문으로 확인한 핵심 주장만 남기세요.')
    return payload


def _historical_source_urls(journal, request_id):
    if journal is None or not journal.exists():
        return []
    response_ids = set()
    for line in journal.read_text().splitlines():
        entry = json.loads(line)
        if (entry.get('request_id') == request_id and entry.get('stage') in {'writer', 'rewrite', 'reviewer'}
                and not entry.get('error') and entry.get('response_id')):
            response_ids.add(entry['response_id'])
    urls = []
    for path in (journal.parent / 'response-cache').glob(f'{today_kst()}-*.json'):
        item = json.loads(path.read_text())
        if item.get('response', {}).get('id') in response_ids:
            urls.extend(_extract_urls(item['response']))
    return list(dict.fromkeys(urls))


class BlogLLM:
    def __init__(self, api_key: str, model: str, root: Path, review_model: str = "", journal: Path | None = None):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        self.client = OpenAI(api_key=api_key, timeout=360, max_retries=0)
        self.journal = journal or root / "private-state" / "usage.jsonl"
        self.model = model
        self.review_model = review_model or model
        self.writer_prompt = _load(root / "prompts/writer.md")
        self.reviewer_prompt = _load(root / "prompts/reviewer.md")

    def create_draft(
        self,
        request: ContentRequest,
        category_info: dict,
        recent_titles: list[str],
    ) -> PostDraft:
        category_key = request.category
        display = category_info["display_name"]
        subcats = ", ".join(category_info["subcategories"])
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        duplicates = "\n".join(f"- {t}" for t in recent_titles[:30]) or "없음"
        today = today_kst().isoformat()

        prompt = f"""
{self.writer_prompt}

오늘 날짜: {today}
카테고리: {display} ({category_key})
허용 하위 카테고리: {subcats}
카테고리 규칙:\n{rules}

최근 작성 제목(중복 금지):\n{duplicates}

아래 원본 입력의 주제만 글로 작성한다. 다른 글감을 선택하거나 빈 정보를 창작하지 않는다.
원본 입력(JSON): {json.dumps(request.prompt_data(), ensure_ascii=False)}
요리는 이 입력의 레시피만 사용한다. 사진은 따로 첨부되므로 캡션 밖의 모습을 추측하지 않는다.
육아는 입력된 실제 질문에 답한다. 선우의 월령·증상·경험을 추정하지 않는다.
투자는 기존 봇의 facts/src/body를 시작점으로 삼고 검색으로 근거를 확인한다.
primary_evidence가 있으면 실제 공시 본문이다. 봇 요약과 충돌하면 원문을 우선한다.
DART 표지·검색 요약만으로 계약 내용이나 정정 사유를 확정하지 않는다.
정정 공시는 정정 전/후, 정정사유, 매출 기준의 회사·연도·별도/연결,
계약 종료일의 변동 조건, 지급 조건을 본문에서 직접 확인하고 핵심 정정 내용을 설명한다.
원본 기준일과 오늘의 작성일을 구분한다. 오래된 수치를 오늘 시세처럼 쓰지 않는다.
투자 카테고리에서는 공시·거래소·기업 IR·공공기관 등 1차 자료를 우선한다.
육아 건강 관련 내용에서는 정부·공공기관·학회·병원 등 신뢰 가능한 자료를 우선한다.
source_urls에는 실제 검색으로 확인한 URL만 넣는다. 요리는 외부 검색 없이 빈 배열로 둔다.
""".strip()

        search = {} if category_key == "cooking" else {
            "tools": [{"type": "web_search"}], "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "max_tool_calls": 3,
        }
        payload, response = request_json(
            self.client, model=self.model, stage="writer", request_id=request.id,
            schema=DRAFT_SCHEMA, journal=self.journal, max_output_tokens=12000,
            retry_output_tokens=16000, reasoning={"effort": "low"}, input=prompt,
            **search,
        )
        evidence = request.provenance.get('primary_evidence', {})
        observed = _extract_urls(response) + [evidence[k] for k in ['url', 'viewer_url'] if k in evidence]
        urls = _source_urls(payload, observed, required=category_key != "cooking")
        return PostDraft(
            category=category_key,
            subcategory=str(payload["subcategory"]),
            topic=str(payload.get("topic") or payload["title"]),
            title=str(payload["title"]).strip(),
            body=str(payload["body"]).strip(),
            tags=[str(x).lstrip("#") for x in payload.get("tags", [])][:8],
            source_urls=urls[:10],
            as_of_date=str(payload.get("as_of_date") or today),
            request_id=request.id,
            photos=request.photos,
            provenance=request.provenance,
        )

    @staticmethod
    def _draft_data(post: PostDraft) -> dict:
        return {k: v for k, v in post.__dict__.items() if k != "photos"}

    def review(self, post: PostDraft, category_info: dict, request: ContentRequest) -> dict:
        from .research import prepare_reference_evidence
        request = prepare_reference_evidence(
            self.journal.parent if self.journal else None, request, post.source_urls)
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        prompt = f"""
{self.reviewer_prompt}

오늘 한국시간 기준일: {today_kst().isoformat()}
작성 기준일은 위 날짜와 대조한다. UTC 날짜나 모델 내부 날짜로 어제/내일을 추정하지 않는다.
카테고리 규칙:\n{rules}

원본 입력 JSON:\n{json.dumps(request.prompt_data(), ensure_ascii=False)}
초안 JSON:\n{json.dumps(self._draft_data(post), ensure_ascii=False)}
""".strip()
        search = {} if post.category == "cooking" else {
            "tools": [{"type": "web_search"}], "tool_choice": "required",
            "max_tool_calls": 3,
        }
        payload, response = request_json(
            self.client, model=self.review_model, stage="reviewer", request_id=request.id,
            schema=REVIEW_SCHEMA, journal=self.journal, max_output_tokens=6000,
            retry_output_tokens=10000, reasoning={"effort": "low"}, input=prompt, **search,
            include=["web_search_call.action.sources"] if search else [],
        )
        return _checked_review(payload, response, request)

    def rewrite(
        self, post: PostDraft, category_info: dict, review: dict, request: ContentRequest,
        *, cache_only: bool = False,
    ) -> PostDraft:
        if not cache_only:
            from .research import prepare_reference_evidence
            request = prepare_reference_evidence(
                self.journal.parent if self.journal else None, request, post.source_urls)
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        prompt = f"""
{self.writer_prompt}

오늘 한국시간 기준일: {today_kst().isoformat()}
기존 초안을 심사 지적사항에 맞게 수정한다. 주제와 핵심 출처는 유지하되 오류·과장·중복을 제거한다.
검증되지 않은 세부 권고·고정 숫자·규칙은 삭제한다. 분량을 채우려고 새 권고를 만들지 않는다.
핵심 주장들은 최대 3개 공식 원문으로 직접 확인할 수 있도록 단순화한다.
수정 전 출처를 유지할 수 있으면 새 URL을 추정해서 추가하지 않는다.
심사자의 날짜·의학 주장도 검증 대상이다. 잘못된 지적을 그대로 복사하지 않는다.
원고 기준일은 오늘 한국시간을 유지한다. 벤치마크 접근 한계·read_count 등 운영 정보는 본문에 넣지 않는다.
카테고리 규칙:\n{rules}
심사 결과:\n{json.dumps(review, ensure_ascii=False)}
원본 입력:\n{json.dumps(request.prompt_data(), ensure_ascii=False)}
기존 초안:\n{json.dumps(self._draft_data(post), ensure_ascii=False)}
""".strip()
        search = {} if post.category == "cooking" else {
            "tools": [{"type": "web_search"}], "tool_choice": "required",
            "include": ["web_search_call.action.sources"], "max_tool_calls": 3,
        }
        payload, response = request_json(
            self.client, model=self.model, stage="rewrite", request_id=request.id,
            schema=DRAFT_SCHEMA, journal=self.journal, max_output_tokens=12000,
            retry_output_tokens=16000, reasoning={"effort": "low"}, input=prompt,
            cache_only=cache_only, **search,
        )
        return replace(
            post,
            subcategory=str(payload.get("subcategory", post.subcategory)),
            topic=post.topic,
            title=str(payload.get("title", post.title)).strip(),
            body=str(payload.get("body", post.body)).strip(),
            tags=[str(x).lstrip("#") for x in payload.get("tags", post.tags)][:8],
            source_urls=_source_urls(payload, post.source_urls + _extract_urls(response)
                                    + _historical_source_urls(self.journal, request.id),
                                    required=post.category != "cooking"),
            as_of_date=post.as_of_date,
        )
