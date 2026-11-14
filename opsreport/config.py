"""Runtime configuration: environment variables and YAML config files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass(frozen=True)
class JiraSettings:
    base_url: str
    email: str
    api_token: str
    project_key: str


def load_jira_settings() -> JiraSettings:
    load_dotenv(ROOT / ".env")
    missing = [
        k
        for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY")
        if not os.getenv(k, "").strip()
    ]
    if missing:
        raise RuntimeError(f"Missing settings in .env: {', '.join(missing)}")
    return JiraSettings(
        base_url=os.environ["JIRA_BASE_URL"].strip().rstrip("/"),
        email=os.environ["JIRA_EMAIL"].strip(),
        api_token=os.environ["JIRA_API_TOKEN"].strip(),
        project_key=os.environ["JIRA_PROJECT_KEY"].strip(),
    )


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)
