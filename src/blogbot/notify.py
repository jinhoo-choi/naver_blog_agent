from __future__ import annotations

import json
import os
import urllib.request


def telegram(results: list[dict]) -> None:
    if os.getenv("BLOG_NOTIFY_ENABLED", "false").lower() != "true":
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    saved = [r for r in results if r.get("status") == "SAVED_NAVER"]
    lines = [f"[NAVER BLOG] 임시저장 {len(saved)}건 완료"]
    lines += [f"• #{r['id']} ({r['category']}, {r.get('score', '-')}/30)" for r in saved]
    failed = [r for r in results if r.get("status") not in {"SAVED_NAVER", "APPROVED"}]
    if failed:
        lines.append(f"보류/실패 {len(failed)}건")

    body = json.dumps({"chat_id": chat_id, "text": "\n".join(lines)[:4000]}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass
