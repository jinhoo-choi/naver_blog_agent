from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from openai import OpenAI

from .core import PostDraft, today_kst
from .inputs import ContentRequest


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


def _source_urls(payload: dict, observed: list[str], required: bool = True) -> list[str]:
    claimed = payload.get("source_urls", [])
    if not isinstance(claimed, list) or (required and not claimed):
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
        self.client = OpenAI(api_key=api_key, timeout=180, max_retries=0)
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
원본 기준일과 오늘의 작성일을 구분한다. 오래된 수치를 오늘 시세처럼 쓰지 않는다.
투자 카테고리에서는 공시·거래소·기업 IR·공공기관 등 1차 자료를 우선한다.
육아 건강 관련 내용에서는 정부·공공기관·학회·병원 등 신뢰 가능한 자료를 우선한다.
source_urls에는 실제 검색으로 확인한 URL만 넣는다. 요리는 외부 검색 없이 빈 배열로 둔다.
""".strip()

        search = {} if category_key == "cooking" else {
            "tools": [{"type": "web_search"}], "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
        }
        response = self.client.responses.create(
            model=self.model,
            max_output_tokens=8000,
            input=prompt,
            store=False,
            **search,
        )
        payload = _json_from_text(response.output_text)
        urls = _source_urls(payload, _extract_urls(response), required=category_key != "cooking")
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
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        prompt = f"""
{self.reviewer_prompt}

카테고리 규칙:\n{rules}

원본 입력 JSON:\n{json.dumps(request.prompt_data(), ensure_ascii=False)}
초안 JSON:\n{json.dumps(self._draft_data(post), ensure_ascii=False)}
""".strip()
        search = {} if post.category == "cooking" else {
            "tools": [{"type": "web_search"}], "tool_choice": "required",
        }
        response = self.client.responses.create(
            model=self.review_model,
            max_output_tokens=6000,
            input=prompt,
            store=False,
            **search,
        )
        return _json_from_text(response.output_text)

    def rewrite(
        self, post: PostDraft, category_info: dict, review: dict, request: ContentRequest,
    ) -> PostDraft:
        rules = "\n".join(f"- {r}" for r in category_info.get("rules", []))
        prompt = f"""
{self.writer_prompt}

기존 초안을 심사 지적사항에 맞게 수정한다. 주제와 핵심 출처는 유지하되 오류·과장·중복을 제거한다.
카테고리 규칙:\n{rules}
심사 결과:\n{json.dumps(review, ensure_ascii=False)}
원본 입력:\n{json.dumps(request.prompt_data(), ensure_ascii=False)}
기존 초안:\n{json.dumps(self._draft_data(post), ensure_ascii=False)}
""".strip()
        response = self.client.responses.create(
            model=self.model, input=prompt, store=False, max_output_tokens=8000,
        )
        payload = _json_from_text(response.output_text)
        return replace(
            post,
            subcategory=str(payload.get("subcategory", post.subcategory)),
            topic=post.topic,
            title=str(payload.get("title", post.title)).strip(),
            body=str(payload.get("body", post.body)).strip(),
            tags=[str(x).lstrip("#") for x in payload.get("tags", post.tags)][:8],
            source_urls=_source_urls(payload, post.source_urls, required=post.category != "cooking"),
            as_of_date=str(payload.get("as_of_date", post.as_of_date)),
        )
