"""Small, escaped Markdown subset for SmartEditor HTML clipboard paste."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass

from .core import PostDraft


@dataclass(frozen=True)
class Segment:
    html: str = ""
    photo: dict | None = None


def paragraph(text: str, size: int = 16, bold: bool = False) -> str:
    content = html.escape(text).replace("\n", "<br>")
    if bold:
        content = f"<b>{content}</b>"
    color = "#28564f" if bold else "#777777" if size == 12 else "#222222"
    return (f'<p style="font-family:나눔고딕;font-size:{size}px;color:{color};line-height:1.9">'
            f'{content}</p><p><br></p>')


def markdown_html(text: str) -> str:
    """Only headings, paragraphs, lists and pipe tables. Never execute source HTML."""
    blocks = re.split(r"\n\s*\n", text.strip())
    result = []
    for block in blocks:
        lines = block.splitlines()
        if len(lines) >= 3 and lines[0].startswith("|") and re.fullmatch(
            r"[| :\-]+", lines[1]
        ):
            rows = [lines[0], *lines[2:]]
            table = '<table style="border-collapse:collapse;width:100%"><tbody>'
            for i, row in enumerate(rows):
                cells = row.strip().strip("|").split("|")
                table += "<tr>" + "".join(
                    '<td style="border:1px solid #cccccc;padding:12px">'
                    + paragraph(cell.strip(), 15, i == 0) + "</td>" for cell in cells
                ) + "</tr>"
            result.append(table + "</tbody></table><p><br></p>")
        else:
            # Headings can be adjacent to text without blank lines.
            pending = []
            for line in lines:
                heading = re.match(r"^(#{2,3})\s+(.+)$", line)
                if heading:
                    if pending:
                        result.append(paragraph("\n".join(pending)))
                        pending = []
                    result.append(paragraph(heading[2], 24 if len(heading[1]) == 2 else 19, True))
                else:
                    pending.append(re.sub(r"^[-*] ", "• ", line))
            if pending:
                result.append(paragraph("\n".join(pending)))
    return "".join(result)


def validate_structure(post: PostDraft, required: bool = True) -> None:
    if not required:
        return
    heads = re.findall(r"^## (.+)$", post.body, re.MULTILINE)
    if len(heads) < 4 or not re.search(r"^### .+", post.body, re.MULTILINE):
        raise ValueError("Use at least four major sections and a subsection")
    if post.category in {"parenting", "exercise"} and len(post.body) < 1800:
        raise ValueError("Parenting draft is too short; add supported explanation, not filler")
    if re.search(r"<\s*(script|iframe|img)\b|\{\{image:|!\[", post.body, re.IGNORECASE):
        raise ValueError("Images are placed from verified files, not model URLs")


def render_segments(post: PostDraft) -> list[Segment]:
    chunks = re.split(r"(?=^## )", post.body, flags=re.MULTILINE)
    chunks = [c for c in chunks if c.strip()]
    dated = f"작성일: {post.as_of_date}"
    if post.provenance.get("source_date"):
        dated += f" · 원본 자료 기준일: {post.provenance['source_date']}"
    result = [Segment(html=paragraph(dated, 12))]
    # Distribute actual files between sections. One photo is placed near the midpoint.
    slots: dict[int, list[dict]] = {}
    for i, photo in enumerate(post.photos):
        pos = min(len(chunks) - 1, max(0, (i + 1) * len(chunks) // (len(post.photos) + 1) - 1))
        slots.setdefault(pos, []).append(photo)
    for i, chunk in enumerate(chunks):
        result.append(Segment(html=markdown_html(chunk)))
        for photo in slots.get(i, []):
            result.append(Segment(photo=photo))
            caption = photo.get("caption", "")
            if photo.get("author") and not photo.get("generated"):
                caption += f" / {photo['author']} · {photo['license']} (자료사진)"
            if caption:
                result.append(Segment(html=paragraph(caption, 12)))
    references = paragraph("참고자료와 이미지 출처", 24, True)
    references += paragraph("\n".join(dict.fromkeys(post.source_urls)))
    for p in post.photos:
        if p.get("source_url"):
            references += paragraph(
                f"{p['author']} · {p['license']} · 편집 없음(표시 크기 조정)\n"
                f"{p['source_url']}\n{p['license_url']}", 12
            )
    if post.tags:
        references += paragraph(" ".join(f"#{t}" for t in post.tags))
    result.append(Segment(html=references))
    return result
