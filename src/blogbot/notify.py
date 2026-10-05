from __future__ import annotations

import json
import os
import urllib.request


def telegram(results: list[dict]) -> None:
    saved = [r for r in results if r.get("status") == "SAVED_NAVER"]
    prepared = [r for r in results if r.get('status') == 'APPROVED']
    lines = [f"[NAVER BLOG] 원고 준비 {len(prepared)}건 / 네이버 임시저장 {len(saved)}건"]
    lines += [f"• #{r['id']} ({r['category']}, {r.get('score', '-')}/30)" for r in saved]
    trend_states = sorted({r['creator_trends_status'] for r in results
                           if r.get('creator_trends_status')})
    if trend_states:
        lines.append('Creator Advisor 주제 선정 참고: ' + ', '.join(trend_states))
    reserved = [r for r in results if r.get('status') == 'EDITORIAL_SLOT_RESERVED']
    if reserved:
        lines.append(f"기존 수동 초안으로 예약된 편집 슬롯 {len(reserved)}건: 당일 자동 생성·저장 생략")
    failed = [r for r in results if r.get("status") not in {
        "SAVED_NAVER", "APPROVED", "EDITORIAL_SLOT_RESERVED"}]
    if failed:
        lines.append(f"보류/실패 {len(failed)}건")
        for item in failed:
            lines.append('• ' + ' / '.join(str(item[k]) for k in
                         ('category', 'stage', 'status', 'error', 'reason', 'missing_categories')
                         if k in item))
    summary = os.getenv('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('\n'.join(lines) + '\n')
    if os.getenv("BLOG_NOTIFY_ENABLED", "false").lower() != "true":
        return
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(json.dumps({'notification': 'NOT_CONFIGURED'}))
        return

    body = json.dumps({"chat_id": chat_id, "text": "\n".join(lines)[:4000]}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10):
        pass
