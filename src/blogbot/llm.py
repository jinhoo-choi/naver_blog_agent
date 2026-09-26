from __future__ import annotations

import json
import re
from pathlib import Path

from openai import OpenAI

from .core import PostDraft, today_kst


def _load(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _json_from_text(text: str) -> dict:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise ValueError("LLM response must be a JSON object")
    return value


def _extract_urls(response) -> list[str]:
    found: list[str] = []
    for item in response.output:
        action = getattr(item, "action", None)
        for source in getattr(action, "sources", []) or []:
            url = getattr(source, "url", None)
            if url and url not in found:
                found.append(url)
        for part in getattr(item, "content", []) or []:
            for ann in getattr(part, "annotations", []) or []:
                url = getattr(ann, "url", None)
                if url and url not in found:
                    found.append(url)
    return found


def _source_urls(payload: dict, observed: list[str]) -> list[str]:
    claimed = payload.get("source_urls", [])
    if not isinstance(claimed, list) or not claimed:
        raise ValueError("Writer returned no sources")
    if any(not isinstance(url, str) or url not in observed for url in claimed):
        raise ValueError("Source URL was not present in web-search results")
    body_urls = re.findall(r"https?://[^\s<>\]\)]+", payload.get("body", ""))
    if any(url.rstrip('.,') not in observed for url in body_urls):
        raise ValueError("Body contains an unverified URL")
    return list(dict.fromkeys(claimed))[:10]


class BlogLLM:
    def __init__(self, api_key: str, model: str, root: Path, review_model: str = ""):
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is required")
        self.client = OpenAI(api_key=api_key, timeout=180, max_retries=2)
        self.model = model
        self.review_model = review_model or model
        self.writer_prompt = _load(root / "prompts/writer.md")
        self.reviewer_prompt = _load(root / "prompts/reviewer.md")

    def create_draft(
        self,
        category_key: str,
        category_info: dict,
        recent_titles: list[str],
    ) -> PostDraft:
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

웹 검색을 사용해 현재 시점에도 유효한 주제 하나를 선정하고, 해당 주제로 완성도 높은 초안을 작성한다.
투자 카테고리에서는 공시·거래소·기업 IR·공공기관 등 1차 자료를 우선한다.
육아 건강 관련 내용에서는 정부·공공기관·학회·병원 등 신뢰 가능한 자료를 우선한다.
요리에서는 과도한 건강 효능 주장을 하지 않는다.
source_urls에는 실제 참고한 URL만 넣는다.
""".strip()

        response = self.client.responses.create(
            model=self.model,
            tools=[{"type": "web_search"}],
            tool_choice="required",
            include=["web_search_call.action.sources"],
            max_output_tokens=8000,
            input=prompt,
            store=False,
        )
        payload = _json_from_text(response.output_text)
        urls = _source_urls(payload, _extract_urls(response))
        return PostDraft(
            category=category_key,
            subcategory=str(payload["subcategory"]),
            topic=str(payload.get("topic") or payload["title"]),
            title=str(payload["title"]).strip(),
            body=str(payload["body"]).strip(),
            tags=[str(x).lstrip("#") for x in payload.get("tags", [])][:8],
            source_urls=urls[:10],
            as_of_date=str(payload.get("as_of_date") or today),
        )

    def review(self, post: PostDraft, category_info: dict) -> dict:
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        prompt = f"""
{self.reviewer_prompt}

카테고리 규칙:\n{rules}

초안 JSON:\n{json.dumps(post.__dict__, ensure_ascii=False)}
""".strip()
        response = self.client.responses.create(
            model=self.review_model,
            tools=[{"type": "web_search"}],
            tool_choice="required",
            max_output_tokens=6000,
            input=prompt,
            store=False,
        )
        return _json_from_text(response.output_text)

    def rewrite(self, post: PostDraft, category_info: dict, review: dict) -> PostDraft:
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        prompt = f"""
{self.writer_prompt}

기존 초안을 심사 지적사항에 맞게 수정한다. 주제와 핵심 출처는 유지하되 오류·과장·중복을 제거한다.
카테고리 규칙:\n{rules}
심사 결과:\n{json.dumps(review, ensure_ascii=False)}
기존 초안:\n{json.dumps(post.__dict__, ensure_ascii=False)}
""".strip()
        response = self.client.responses.create(
            model=self.model, input=prompt, store=False, max_output_tokens=8000,
        )
        payload = _json_from_text(response.output_text)
        return PostDraft(
            category=post.category,
            subcategory=str(payload.get("subcategory", post.subcategory)),
            topic=post.topic,
            title=str(payload.get("title", post.title)).strip(),
            body=str(payload.get("body", post.body)).strip(),
            tags=[str(x).lstrip("#") for x in payload.get("tags", post.tags)][:8],
            source_urls=_source_urls(payload, post.source_urls),
            as_of_date=str(payload.get("as_of_date", post.as_of_date)),
        )
