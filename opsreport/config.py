"""Runtime configuration: environment variables and YAML config files."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class ConfigError(RuntimeError):
    """Required configuration (e.g. .env settings) is missing or invalid."""


@dataclass(frozen=True)
class JiraSettings:
    base_url: str
    email: str
    api_token: str
    project_key: str


def _require_https(url: str) -> None:
    """The API token travels with every request; never send it unencrypted."""
    parsed = urlparse(url)
    local = parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")
    if parsed.scheme != "https" and not local:
        raise ConfigError(f"JIRA_BASE_URL must start with https:// (got '{url}')")


def load_jira_settings() -> JiraSettings:
    env_file = ROOT / ".env"
    if env_file.exists() and os.name == "posix" and env_file.stat().st_mode & 0o077:
        log.warning(".env holds an API token but other users on this machine can read "
                    "it; run: chmod 600 %s", env_file)
    load_dotenv(env_file)
    missing = [
        k
        for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY")
        if not os.getenv(k, "").strip()
    ]
    if missing:
        raise ConfigError(f"Missing settings in .env: {', '.join(missing)}")
    _require_https(os.environ["JIRA_BASE_URL"].strip())
    return JiraSettings(
        base_url=os.environ["JIRA_BASE_URL"].strip().rstrip("/"),
        email=os.environ["JIRA_EMAIL"].strip(),
        api_token=os.environ["JIRA_API_TOKEN"].strip(),
        project_key=os.environ["JIRA_PROJECT_KEY"].strip(),
    )


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)
