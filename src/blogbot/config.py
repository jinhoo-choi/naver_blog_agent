from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .core import today_kst
from .planning import resolve_plan


@dataclass(frozen=True)
class Settings:
    root: Path
    config: dict
    openai_api_key: str
    openai_model: str
    naver_id: str
    naver_password: str
    naver_blog_id: str
    naver_profile_dir: str
    db_path: Path
    daily_count: int
    headless: bool
    review_model: str
    artifact_dir: Path
    inbox_dir: Path


def load_settings() -> Settings:
    root = Path(__file__).resolve().parents[2]
    config_path = Path(os.getenv("BLOG_CONFIG_PATH", root / "config/blog.toml"))
    if not config_path.is_absolute():
        config_path = root / config_path
    with config_path.open("rb") as f:
        config = tomllib.load(f)

    weekday = today_kst().weekday()
    plan = resolve_plan(config, today_kst())
    if plan:
        config['daily_plan'] = plan
        config['blog']['daily_max'] = plan['target']
        config['community']['enabled'] = (config['community'].get('enabled', False)
                                           and plan['category'] == 'investment')
        for category, info in config['categories'].items():
            info['max_daily'] = int(category == plan['category'])
        info = config['categories'][plan['category']]
        if plan['depth'] == 'deep':
            info['rules'].extend(config.get('editorial', {}).get('deep', {}).get('rules', []))
    elif config["blog"].get("weekend_feature", False) and weekday >= 5:
        config["blog"]["daily_max"] = 1
        config["community"]["enabled"] = False
        for category, info in config["categories"].items():
            info["max_daily"] = int(category == "parenting")
        config["categories"]["parenting"]["rules"].extend(
            config.get("editorial", {}).get("weekend", {}).get("rules", []))

    data_dir = Path(os.getenv("BLOG_DATA_DIR") or Path.home() / ".naver-blog-agent")
    db_path = Path(os.getenv("BLOG_DB_PATH") or data_dir / "blog.db")
    if not db_path.is_absolute():
        db_path = root / db_path

    return Settings(
        root=root,
        config=config,
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_model=os.getenv("OPENAI_MODEL", "gpt-5"),
        naver_id=os.getenv("NAVER_ID", ""),
        naver_password=os.getenv("NAVER_PASSWORD", ""),
        naver_blog_id=os.getenv("NAVER_BLOG_ID", ""),
        naver_profile_dir=os.getenv("NAVER_PROFILE_DIR") or str(data_dir / "chrome-profile"),
        db_path=db_path,
        daily_count=min(int(os.getenv("BLOG_DAILY_COUNT", "3")), config["blog"]["daily_max"]),
        headless=os.getenv("NAVER_HEADLESS", "false").lower() == "true",
        review_model=os.getenv("OPENAI_REVIEW_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5"),
        artifact_dir=Path(os.getenv("BLOG_ARTIFACT_DIR") or data_dir / "drafts"),
        inbox_dir=Path(os.getenv("BLOG_INBOX_DIR") or data_dir / "inbox"),
    )
