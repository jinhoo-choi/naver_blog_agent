"""Small, escaped Markdown subset for SmartEditor HTML clipboard paste."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

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
        if len(lines) >= 3 and "|" in lines[0] and "|" in lines[1] and re.fullmatch(
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
                    if len(heading[1]) == 2:
                        result.append("<hr>")
                    result.append(paragraph(heading[2], 24 if len(heading[1]) == 2 else 19, True))
                else:
                    pending.append(re.sub(r"^[-*] ", "• ", line))
            if pending:
                result.append(paragraph("\n".join(pending)))
    return "".join(result)


def normalize_structure(post: PostDraft) -> PostDraft:
    """Fix presentation only, preserving factual text and existing section titles."""
    body = re.sub(r'^\[(?:도해|삽화)(?:\s*계획)?\][^\n]*\n?', '', post.body, flags=re.MULTILINE)
    heads = list(re.finditer(r'^## .+$', body, re.MULTILINE))
    if len(heads) > 6:
        for heading in reversed(heads[6:]):
            body = body[:heading.start()] + '#' + body[heading.start():]
        heads = heads[:6]
    if len(heads) >= 5 and not re.search(r'^### .+', body, re.MULTILINE):
        last = heads[-1].start()
        body = body[:last] + '#' + body[last:]
    return replace(post, body=body)


def validate_structure(post: PostDraft, required: bool = True) -> None:
    if not required:
        return
    opening = re.split(r"\n\s*\n|(?=^## )", post.body.strip(), maxsplit=1,
                       flags=re.MULTILINE)[0]
    if not opening.strip() or re.search(
        r"https?://|기준일\s*[:：]|작성일\s*[:：]|작성 기준일\s*[:：]", opening
    ):
        raise ValueError("Start with a plain-language preview summary, not dates or URLs")
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
    result = []
    # The opening summary precedes the cover; supporting images follow body sections.
    slots: dict[int, list[dict]] = {}
    for i, photo in enumerate(post.photos):
        if photo.get("role") == "thumbnail":
            pos = 0
        elif photo.get("role") == "section" and post.photos[0].get("role") == "thumbnail":
            pos = min(len(chunks) - 1, 1 + round(
                (i - 1) * max(0, len(chunks) - 2) / max(1, len(post.photos) - 2)
            ))
        else:
            pos = min(len(chunks) - 1, max(
                0, (i + 1) * len(chunks) // (len(post.photos) + 1) - 1
            ))
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
    references = "<hr>" + paragraph("참고자료와 이미지 출처", 24, True)
    for i, url in enumerate(dict.fromkeys(post.source_urls), 1):
        label = f"참고자료 {i} · {urlsplit(url).hostname}"
        references += ('<p style="font-size:12px;line-height:1.9"><a href="'
                       + html.escape(url, quote=True) + '">' + html.escape(label) + '</a></p>')
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
