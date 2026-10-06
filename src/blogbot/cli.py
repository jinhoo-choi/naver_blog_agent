from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from openai import OpenAIError
from playwright.sync_api import Error as PlaywrightError

from .config import load_settings
from .core import connect_db, set_status
from .creator_trends import import_snapshot
from .inputs import enqueue_file
from .notify import telegram
from .pipeline import make_writer, run_daily, save_pending
from .planning import active_plan, record_verified_save


def main() -> None:
    parser = argparse.ArgumentParser(prog="blogbot")
    sub = parser.add_subparsers(dest="command", required=True)
    daily = sub.add_parser("daily", help="KST 일일 후보 한도 내에서 생성")
    daily.add_argument("--count", type=int, default=None)
    daily.add_argument("--save-to-naver", action="store_true")
    sub.add_parser("login", help="로컬 Chrome에서 직접 로그인")
    sub.add_parser("retry", help="APPROVED 글만 네이버 임시저장")
    sub.add_parser("status", help="본문 없이 상태별 건수 확인")
    enqueue = sub.add_parser("enqueue", help="실제 육아 질문 또는 레시피·사진 입력 등록")
    enqueue.add_argument("--file", type=Path, required=True)
    trends = sub.add_parser("import-creator-trends", help="관찰한 Creator Advisor 근거만 등록")
    trends.add_argument("--file", type=Path, required=True)
    policy = sub.add_parser("import-policy-evidence", help="검증할 정책 근거 등록 (후보/승인 생성 안 함)")
    policy.add_argument("--file", type=Path, required=True)
    doctor = sub.add_parser("doctor", help="비밀값을 출력하지 않고 필수 설정 확인")
    doctor.add_argument("--require-naver", action="store_true")
    resolve = sub.add_parser("resolve", help="네이버 임시저장 목록을 직접 확인한 뒤 상태 확정")
    resolve.add_argument("--id", type=int, required=True)
    resolve.add_argument("--outcome", choices=["saved", "not-saved", "discard"], required=True)
    saved_when = resolve.add_mutually_exclusive_group()
    saved_when.add_argument('--saved-at', help='Verified actual save timestamp with timezone offset')
    saved_when.add_argument('--saved-day', help='Verified actual KST save day YYYY-MM-DD')
    args = parser.parse_args()
    settings = load_settings()

    try:
        if args.command == "import-policy-evidence":
            from .weekly_policy import import_evidence
            print(json.dumps(import_evidence(settings, args.file.resolve())))
            return
        if args.command == "import-creator-trends":
            print(json.dumps(import_snapshot(settings, args.file.resolve())))
            return
        if args.command == "enqueue":
            enqueue_file(settings, args.file.resolve())
            print(json.dumps({"status": "INPUT_QUEUED"}))
            return
        if args.command == "doctor":
            checks = {"OPENAI_API_KEY": bool(settings.openai_api_key)}
            if args.require_naver:
                checks["NAVER_BLOG_ID"] = bool(settings.naver_blog_id)
                checks["NAVER_PROFILE_DIR"] = bool(settings.naver_profile_dir)
            print(json.dumps(checks))
            raise SystemExit(0 if all(checks.values()) else 1)
        if args.command == "login":
            make_writer(settings).login()
            return
        if args.command in {"status", "resolve"}:
            with closing(connect_db(settings.db_path)) as conn:
                if args.command == "status":
                    rows = conn.execute("SELECT status, COUNT(*) FROM posts GROUP BY status")
                    print(json.dumps(dict(rows), ensure_ascii=False))
                    return
                row = conn.execute("SELECT status FROM posts WHERE id=?", (args.id,)).fetchone()
                if row is None or row[0] not in {"SAVING", "SAVE_UNCERTAIN", "STALE_REVIEW_REQUIRED"}:
                    raise ValueError("Only uncertain or stale posts can be resolved")
                if args.outcome == "saved":
                    if not args.saved_at and not args.saved_day:
                        raise ValueError('Saved resolution requires actual --saved-at or --saved-day evidence')
                    record_verified_save(conn, args.id, {'saved_at': args.saved_at, 'day': args.saved_day})
                else:
                    set_status(conn, args.id, "APPROVED" if args.outcome == "not-saved" else "DISCARDED")
                print(json.dumps({"id": args.id, "resolution": args.outcome}))
                return
        if args.command == "daily":
            results = run_daily(settings, count=args.count, save_to_naver=args.save_to_naver)
        else:
            results = save_pending(settings)
        print(json.dumps(results, ensure_ascii=False, indent=2))
        try:
            telegram(results)
        except (OSError, ValueError, RuntimeError) as exc:
            print(json.dumps({"notification": "FAILED", "error": type(exc).__name__}))
        errors = {"ERROR", "SAVE_UNCERTAIN", "MANUAL_CHECK_REQUIRED", "STALE_REVIEW_REQUIRED",
                  "INPUT_REJECTED", "COMMUNITY_SOURCE_UNAVAILABLE", "SETUP_REQUIRED", "RESEARCH_REQUIRED",
                  "PLANNED_INPUT_REQUIRED", "IMAGES_PENDING", "COMMUNITY_SOURCE_PENDING",
                  "NO_ELIGIBLE_INVESTMENT"}
        if active_plan(settings):
            errors |= {"DROP_REVIEW", "DROP_DUPLICATE"}
        raise SystemExit(1 if any(r["status"] in errors for r in results) else 0)
    except (KeyboardInterrupt, SystemExit):
        raise
    except (OpenAIError, PlaywrightError, sqlite3.Error, OSError,
            RuntimeError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "ERROR", "error": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
