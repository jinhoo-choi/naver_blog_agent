from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Page, expect, sync_playwright

from .core import PostDraft
from .inputs import verify_photos
from .presentation import render_segments


class _ClipboardText(HTMLParser):
    """Plain fallback for our escaped renderer; preserve meaning-unit boundaries."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == 'br':
            self.parts.append('\n')
        elif tag in {'p', 'div', 'h1', 'h2', 'h3', 'hr', 'blockquote', 'ul', 'ol', 'table'}:
            self.parts.append('\n\n')
        elif tag == 'li':
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'td', 'th'}:
            self.parts.append('\t')
        elif tag in {'p', 'div', 'h1', 'h2', 'h3', 'blockquote', 'ul', 'ol', 'table'}:
            self.parts.append('\n\n')
        elif tag in {'li', 'tr'}:
            self.parts.append('\n')

    def handle_data(self, data):
        self.parts.append(data)


def clipboard_plain_text(html_text: str) -> str:
    parser = _ClipboardText()
    parser.feed(html_text)
    parser.close()
    text = ''.join(parser.parts).replace('\xa0', ' ')
    text = re.sub(r'[ \t]+\n', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


class NaverDraftWriter:
    """Category-routed SmartEditor writer; publish controls are never used."""

    def __init__(
        self, blog_id: str, profile_dir: str, naver_id: str = "",
        naver_password: str = "", headless: bool = False, selectors: dict | None = None,
        categories: dict | None = None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", blog_id):
            raise RuntimeError("Set NAVER_BLOG_ID to the blog ID, not a URL")
        if not profile_dir:
            raise RuntimeError("NAVER_PROFILE_DIR is required")
        self.blog_id = blog_id
        self.profile_dir = Path(profile_dir)
        self.naver_id, self.naver_password = naver_id, naver_password
        self.headless = headless
        self.selectors = selectors or {}
        self.categories = categories or {}

    def editor_url(self, category: str | None = None) -> str:
        if category:
            number = self.categories.get(category, {}).get("naver_category_no")
            if type(number) is not int or number < 1:
                raise RuntimeError(f"No verified Naver category number for {category}")
            return f"https://blog.naver.com/{self.blog_id}/postwrite?" + urlencode(
                {"categoryNo": number}
            )
        return "https://blog.naver.com/PostWriteForm.naver?" + urlencode(
            {"blogId": self.blog_id}
        )

    @staticmethod
    def _has_security_challenge(page: Page) -> bool:
        text = page.locator("body").inner_text(timeout=3000)
        markers = ("자동입력 방지", "보안 확인", "보안문자", "CAPTCHA", "캡챠")
        return any(m.lower() in text.lower() for m in markers)

    def _ensure_login(self, page: Page, category: str) -> None:
        url = self.editor_url(category)
        page.goto(url, wait_until="domcontentloaded")
        if "nid.naver.com" not in page.url:
            return
        if self._has_security_challenge(page):
            raise RuntimeError("Security challenge: complete login manually")
        if not self.naver_id or not self.naver_password:
            raise RuntimeError("Session expired: run blogbot login on the runner PC")
        page.locator("#id").fill(self.naver_id)
        page.locator("#pw").fill(self.naver_password)
        page.locator(r"#log\.login").click()
        page.wait_for_timeout(2000)
        if self._has_security_challenge(page) or "nid.naver.com" in page.url:
            raise RuntimeError("Login incomplete: complete security checks manually")
        page.goto(url, wait_until="domcontentloaded")

    @staticmethod
    def _editor_frame(page: Page):
        return page.frame(name="mainFrame") or page

    @staticmethod
    def _write_clipboard(editor, html_text: str) -> None:
        editor.evaluate(
            """async value => navigator.clipboard.write([new ClipboardItem({
              'text/html': new Blob([value.html], {type: 'text/html'}),
              'text/plain': new Blob([value.plain], {type: 'text/plain'})
            })])""", {"html": html_text, "plain": clipboard_plain_text(html_text)},
        )

    def _paste_html(self, page: Page, editor, html_text: str, selector: str = "") -> None:
        if selector:
            target = editor.locator(selector).first
            target.wait_for(state="visible", timeout=20000)
            target.click()
        self._write_clipboard(editor, html_text)
        page.keyboard.press("Control+V")

    def _attach_photo(self, page: Page, editor, photo: dict) -> None:
        images = editor.locator(self.selectors.get(
            "uploaded_image_selector", ".se-component.se-image img"
        ))
        before = images.count()
        button = editor.get_by_role("button", name="사진 추가", exact=True)
        if button.count() != 1 or not button.is_visible():
            raise RuntimeError("Photo upload control is ambiguous")
        with page.expect_file_chooser(timeout=10000) as chooser:
            button.click()
        chooser.value.set_files(photo["file"])
        expect(images).to_have_count(before + 1, timeout=60000)
        expect(images.nth(before)).to_be_visible(timeout=10000)

    @staticmethod
    def _save_draft(page: Page, editor, title: str) -> None:
        save = editor.get_by_role("button", name="저장", exact=True)
        if save.count() != 1 or not save.is_visible():
            raise RuntimeError("Exactly one visible save button is required")
        save.click()
        draft_list = editor.get_by_role(
            "button", name=re.compile(r"^임시저장된 글 보기,\s*\d+개$")
        )
        draft_list.wait_for(state="visible", timeout=20000)
        draft_list.click()
        entry = editor.get_by_role("button", name=re.compile(rf"^{re.escape(title)}\s+\d{{4}}\."))
        expect(entry.first).to_be_visible(timeout=15000)

    def login(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir), channel="chrome", headless=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.editor_url(), wait_until="domcontentloaded")
                input("브라우저에서 로그인/보안 확인을 마친 뒤 Enter를 누르세요: ")
            finally:
                context.close()

    def preflight(self, post: PostDraft) -> None:
        self.editor_url(post.category)
        if post.provenance.get('content_style') == 'review':
            raise RuntimeError('Review requires Work visual privacy/layout checks before saving')
        if not post.photos:
            raise RuntimeError("A reviewed or generated article image is required")
        verify_photos(post.photos)

    def save(self, post: PostDraft) -> None:
        self.preflight(post)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), channel="chrome", headless=self.headless,
                viewport={"width": 1440, "height": 1000},
                permissions=["clipboard-read", "clipboard-write"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._ensure_login(page, post.category)
                page.wait_for_timeout(1500)
                if self._has_security_challenge(page):
                    raise RuntimeError("Security challenge: automation stopped")
                editor = self._editor_frame(page)
                if editor.get_by_text("작성 중인 글이 있습니다.", exact=True).count():
                    raise RuntimeError("Unresolved editor recovery prompt")
                title = editor.locator(self.selectors.get(
                    "title_selector", ".se-documentTitle .se-text-paragraph",
                )).first
                title.wait_for(state="visible", timeout=20000)
                title.click()
                page.keyboard.insert_text(post.title)
                expect(title).to_contain_text(post.title, timeout=5000)
                first = True
                for segment in render_segments(post):
                    if segment.photo:
                        self._attach_photo(page, editor, segment.photo)
                    elif segment.html:
                        selector = self.selectors.get(
                            "body_selector", ".se-main-container .se-component.se-text .se-text-paragraph"
                        ) if first else ""
                        self._paste_html(page, editor, segment.html, selector)
                        first = False
                self._save_draft(page, editor, post.title)
            finally:
                context.close()
