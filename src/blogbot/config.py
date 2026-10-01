from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .core import today_kst


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
    if config["blog"].get("weekend_feature", False) and weekday >= 5:
        config["blog"]["daily_max"] = 1
        config["community"]["enabled"] = False
        for category, info in config["categories"].items():
            info["max_daily"] = int(weekday == 5 and category == "parenting")
        config["categories"]["parenting"]["rules"].append(
            "주말 심화 콘텐츠: 하나의 실제 질문에 집중해 공식 근거·적용 조건·실천 단계·"
            "흔한 실수 비교·주의 신호를 충실하게 설명한다. 모바일 짧은 문단과 3열 이하 표, "
            "검증한 내용의 단계별 삽화를 활용하고 분량 채우기나 체험담 창작은 하지 않는다.")

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
