from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import Page, expect, sync_playwright

from .core import PostDraft, render_post_text


class NaverDraftWriter:
    """Only a temporary-save button is actionable; publish controls are never used."""

    def __init__(
        self, blog_id: str, profile_dir: str, naver_id: str = "",
        naver_password: str = "", headless: bool = False, selectors: dict | None = None,
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

    @property
    def editor_url(self) -> str:
        return "https://blog.naver.com/PostWriteForm.naver?" + urlencode({"blogId": self.blog_id})

    @staticmethod
    def _has_security_challenge(page: Page) -> bool:
        text = page.locator("body").inner_text(timeout=3000)
        markers = ("자동입력 방지", "보안 확인", "보안문자", "CAPTCHA", "캡챠")
        return any(m.lower() in text.lower() for m in markers)

    def _ensure_login(self, page: Page) -> None:
        page.goto(self.editor_url, wait_until="domcontentloaded")
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
        page.goto(self.editor_url, wait_until="domcontentloaded")

    @staticmethod
    def _editor_frame(page: Page):
        frame = page.frame(name="mainFrame")
        return frame or page

    @staticmethod
    def _fill(editor, selector: str, text: str) -> None:
        target = editor.locator(selector).first
        target.wait_for(state="visible", timeout=15000)
        target.fill(text)
        expect(target).to_have_text(text, timeout=5000)

    def _save_draft(self, page: Page, editor) -> None:
        locations = [page] if editor is page else [page, editor]
        choices = []
        for location in locations:
            buttons = location.get_by_role("button", name=re.compile(r"^임시저장(?:\s*\d+)?$"))
            choices.extend(buttons.nth(i) for i in range(buttons.count())
                           if buttons.nth(i).is_visible())
        if len(choices) != 1:
            raise RuntimeError("Exactly one visible temporary-save button is required")
        confirmation = self.selectors.get("save_confirmation_selector", "")
        saved = re.compile(r"임시\s*저장(?:이)?\s*(?:완료|되었습니다|하였습니다)")
        # An old success toast must disappear before this save attempt.
        for location in locations:
            before = location.locator(confirmation) if confirmation else location.get_by_text(saved)
            for i in range(before.count()):
                expect(before.nth(i)).not_to_be_visible(timeout=10000)
        choices[0].click()
        # A click is not evidence of persistence. Require a new explicit success message.
        for _ in range(20):
            for location in locations:
                signal = location.locator(confirmation) if confirmation else location.get_by_text(saved)
                if any(signal.nth(i).is_visible() for i in range(signal.count())):
                    return
            page.wait_for_timeout(500)
        raise RuntimeError("Save unconfirmed: inspect Naver draft list before retrying")

    def login(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(self.profile_dir), channel="chrome", headless=False,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self.editor_url, wait_until="domcontentloaded")
                input("브라우저에서 로그인/보안 확인을 마친 뒤 Enter를 누르세요: ")
            finally:
                context.close()

    def save(self, post: PostDraft) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir), channel="chrome", headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._ensure_login(page)
                page.wait_for_timeout(1500)
                if self._has_security_challenge(page):
                    raise RuntimeError("Security challenge: automation stopped")
                editor = self._editor_frame(page)
                # No generic popup confirmation: unknown editor dialogs stop the run.
                self._fill(editor, self.selectors.get(
                    "title_selector", ".se-documentTitle .se-text-paragraph",
                ), post.title)
                self._fill(editor, self.selectors.get(
                    "body_selector", ".se-main-container .se-component.se-text .se-text-paragraph",
                ), render_post_text(post))
                self._save_draft(page, editor)
            finally:
                context.close()
