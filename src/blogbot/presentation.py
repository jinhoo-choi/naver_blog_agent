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
    content = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", content)
    content = re.sub(r"__([^_\n]+)__", r"<u>\1</u>", content)
    if bold:
        content = f"<b>{content}</b>"
    color = "#28564f" if bold else "#777777" if size == 12 else "#222222"
    return (f'<p style="font-family:나눔고딕;font-size:{size}px;color:{color};line-height:1.9">'
            f'{content}</p><p><br></p>')


def markdown_html(text: str) -> str:
    """Escaped headings, emphasis, callouts, lists and tables; never execute source HTML."""
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
                callout = re.match(r"^>\s+(.+)$", line)
                if heading or callout:
                    if pending:
                        result.append(paragraph("\n".join(pending)))
                        pending = []
                    if callout:
                        result.append(paragraph(callout[1], 20, True))
                        continue
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
    if re.search(r"<\s*(script|iframe|img)\b|\{\{image:|!\[", post.body, re.IGNORECASE):
        raise ValueError("Images are placed from verified files, not model URLs")
    if post.category == 'origins':
        return  # Fact checks remain mandatory; no length or heading quota for short explanations.
    sections = re.split(r"^## .+$", post.body, flags=re.MULTILINE)[1:]
    if not sections or any(not re.sub(r"^#{3,6} .+$", "", section,
                                      flags=re.MULTILINE).strip() for section in sections):
        raise ValueError("Use a meaningful section heading with supported explanation")


def render_segments(post: PostDraft, *, include_tags: bool = True) -> list[Segment]:
    chunks = re.split(r"(?=^## )", post.body, flags=re.MULTILINE)
    chunks = [c for c in chunks if c.strip()]
    if post.category == 'origins' and len(chunks) == 1:
        chunks = re.split(r'\n\s*\n', chunks[0], maxsplit=1)
    result = []
    if post.category == 'origins':
        from .origins import SERIES_MOTIVE, validate_origin_post
        validate_origin_post(post)
        affiliate = post.provenance['origins'].get('affiliate')
        if affiliate:
            result.append(Segment(html=paragraph(affiliate['disclosure'])))
        result.append(Segment(html=paragraph(SERIES_MOTIVE, 12) + '<hr>'))
    # The opening summary precedes the cover; supporting images follow body sections.
    slots: dict[int, list[dict]] = {}
    review = post.provenance.get('content_style') == 'review'
    headings = {match[1]: i for i, chunk in enumerate(chunks)
                if (match := re.match(r'^## (.+)', chunk))}
    last_owner_slot = 0
    for i, photo in enumerate(post.photos):
        if review and photo.get('role') == 'hero':
            pos = 0
        elif photo.get('section_heading') and (review or photo.get('role') == 'section'):
            if photo['section_heading'] not in headings or sum(
                    bool(re.match(r'^## ' + re.escape(photo['section_heading']) + r'\s*$',
                                  chunk.splitlines()[0])) for chunk in chunks) != 1:
                raise ValueError('Review photo section heading needs reconciliation' if review
                                 else 'Image section heading needs reconciliation')
            pos = headings[photo['section_heading']]
        elif review and photo.get('origin') == 'seller':
            pos = len(chunks) - 1
        elif (review and i == 0) or photo.get("role") == "thumbnail":
            pos = 0
        elif photo.get("role") == "section" and post.photos[0].get("role") == "thumbnail":
            pos = min(len(chunks) - 1, 1 + round(
                (i - 1) * max(0, len(chunks) - 2) / max(1, len(post.photos) - 2)
            ))
        else:
            pos = min(len(chunks) - 1, max(
                0, (i + 1) * len(chunks) // (len(post.photos) + 1) - 1
            ))
        if review:
            if photo.get('origin') == 'seller' and pos < last_owner_slot:
                raise ValueError('Seller screenshot must follow owner photo evidence')
            if photo.get('origin') != 'seller':
                last_owner_slot = max(last_owner_slot, pos)
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
    if post.category == 'origins' and post.provenance['origins'].get('affiliate'):
        affiliate = post.provenance['origins']['affiliate']
        result.append(Segment(html='<hr><p><a href="' + html.escape(affiliate['destination_url'], quote=True)
                              + '">' + html.escape(affiliate['product']) + '</a></p>'))
    references = "<hr>" + paragraph("참고자료와 이미지 출처", 24, True)
    for i, url in enumerate(dict.fromkeys(post.source_urls), 1):
        label = f"참고자료 {i} · {urlsplit(url).hostname}"
        references += ('<p style="font-size:12px;line-height:1.9"><a href="'
                       + html.escape(url, quote=True) + '">' + html.escape(label) + '</a></p>')
    for p in post.photos:
        if review and p.get('origin') == 'seller' and p.get('source_url'):
            references += paragraph('판매자 제공 구성 자료 · ' + p['source_url'], 12)
        elif p.get("source_url"):
            references += paragraph(
                f"{p['author']} · {p['license']} · 편집 없음(표시 크기 조정)\n"
                f"{p['source_url']}\n{p['license_url']}", 12
            )
    if include_tags and post.tags:
        references += paragraph(" ".join(f"#{t}" for t in post.tags))
    result.append(Segment(html=references))
    return result
