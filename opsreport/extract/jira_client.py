"""Jira Cloud REST v3 client used by the reporting pipeline.

- Auth: basic auth with email + API token (from .env, never committed).
- Search: uses the enhanced `/rest/api/3/search/jql` endpoint, which pages with
  `nextPageToken`. The old `/rest/api/3/search` (startAt/total) was removed.
- Fields: custom fields are resolved by *display name* from
  config/jira_fields.yaml, so the code never hardcodes customfield ids.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yaml

from opsreport.config import CONFIG_DIR, JiraSettings, load_jira_settings, load_yaml

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class JiraError(RuntimeError):
    pass


class FieldMappingError(JiraError):
    """A configured custom field could not be found in Jira."""


class JiraClient:
    def __init__(
        self,
        settings: JiraSettings | None = None,
        *,
        max_retries: int = 5,
        timeout: float = 30.0,
    ) -> None:
        self.settings = settings or load_jira_settings()
        self.base_url = self.settings.base_url
        self.max_retries = max_retries
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (self.settings.email, self.settings.api_token)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------ http
    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{self.base_url}{path}"
        for attempt in range(1, self.max_retries + 1):
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                wait = float(resp.headers.get("Retry-After", 2**attempt))
                log.warning("Jira %s %s -> %s; retrying in %.0fs", method, path,
                            resp.status_code, wait)
                time.sleep(wait)
                continue
            if not resp.ok:
                raise JiraError(
                    f"Jira {method} {path} failed: {resp.status_code} {resp.text[:500]}"
                )
            return resp.json() if resp.content else None
        raise JiraError(f"Jira {method} {path}: retries exhausted")  # pragma: no cover

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, body: dict) -> Any:
        return self.request("POST", path, json=body)

    def put(self, path: str, body: dict) -> Any:
        return self.request("PUT", path, json=body)

    # ---------------------------------------------------------------- fields
    def all_fields(self) -> list[dict]:
        return self.get("/rest/api/3/field")

    def site_fingerprint(self) -> str:
        """Stable id for this Jira site without storing its URL."""
        return hashlib.sha256(self.base_url.lower().encode()).hexdigest()[:16]

    def resolve_custom_fields(self, field_config: dict | None = None,
                              lock: dict | None | bool = True) -> dict[str, str]:
        """Map logical names (e.g. 'fund') to Jira ids (e.g. 'customfield_10042').

        Resolution is by display name (portable across Jira sites). If a name is
        missing but the id pinned in config/jira_field_lock.yaml for *this* site
        still exists with the same type, the field was renamed: follow the id
        and record a warning in `self.field_warnings`.

        Raises FieldMappingError listing every field it could not resolve.
        """
        cfg = field_config or load_yaml("jira_fields.yaml")
        if lock is True:
            lock = load_field_lock()
        if lock and lock.get("site_fingerprint") != self.site_fingerprint():
            log.warning("Field lock is for a different Jira site; ignoring it")
            lock = None
        pinned = (lock or {}).get("fields", {})

        aliases = cfg.get("aliases") or {}
        by_name: dict[str, list[str]] = {}
        by_id: dict[str, dict] = {}
        for f in self.all_fields():
            if f.get("custom"):
                by_name.setdefault(f["name"], []).append(f["id"])
                by_id[f["id"]] = f

        self.field_warnings: list[str] = []
        resolved, problems = {}, []
        for logical, spec in cfg["custom_fields"].items():
            candidates = [spec["name"], *aliases.get(logical, [])]
            hits = [(n, by_name[n]) for n in candidates if n in by_name]
            pin = pinned.get(logical)
            if hits:
                name, ids = hits[0]
                if len(ids) > 1:
                    problems.append(f"{logical}: '{name}' is ambiguous {ids}")
                elif pin and pin["id"] != ids[0]:
                    problems.append(
                        f"{logical}: '{name}' is now {ids[0]} but the lock pins "
                        f"{pin['id']}; a new field may have taken the old name")
                else:
                    resolved[logical] = ids[0]
                continue
            current = by_id.get(pin["id"]) if pin else None
            if current and _schema_type(current) == pin.get("type"):
                resolved[logical] = pin["id"]
                msg = (f"Jira field '{spec['name']}' was renamed to '{current['name']}' "
                       f"({pin['id']}); followed by id. Update config/jira_fields.yaml.")
                self.field_warnings.append(msg)
                log.warning(msg)
            else:
                problems.append(f"{logical}: no Jira field named {candidates}"
                                + (f" and pinned id {pin['id']} is gone or changed type"
                                   if pin else ""))
        if problems:
            raise FieldMappingError(
                "Custom field mapping failed (check config/jira_fields.yaml):\n  "
                + "\n  ".join(problems)
            )
        return resolved

    def build_field_lock(self, field_ids: dict[str, str]) -> dict:
        by_id = {f["id"]: f for f in self.all_fields()}
        return {
            "site_fingerprint": self.site_fingerprint(),
            "fields": {k: {"id": v, "name": by_id[v]["name"],
                           "type": _schema_type(by_id[v])}
                       for k, v in field_ids.items()},
        }

    # ---------------------------------------------------------------- search
    def search(
        self, jql: str, fields: list[str], page_size: int = 100
    ) -> Iterator[dict]:
        """Yield every issue matching `jql`, following nextPageToken."""
        token = None
        while True:
            body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": page_size}
            if token:
                body["nextPageToken"] = token
            page = self.post("/rest/api/3/search/jql", body)
            yield from page.get("issues", [])
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return


FIELD_LOCK = CONFIG_DIR / "jira_field_lock.yaml"


def _schema_type(field: dict) -> str | None:
    return (field.get("schema") or {}).get("custom")


def load_field_lock(path: Path = FIELD_LOCK) -> dict | None:
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def save_field_lock(lock: dict, path: Path = FIELD_LOCK) -> None:
    header = ("# Jira custom field ids pinned per site (written by scripts/jira_setup.py).\n"
              "# Lets a renamed field be followed by id instead of stopping the run.\n")
    path.write_text(header + yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")


# ----------------------------------------------------------------- flatten
def _value(raw: Any) -> Any:
    """Flatten a Jira field value into a scalar suitable for a DataFrame."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        for key in ("value", "name", "displayName"):
            if key in raw:
                return raw[key]
        return None
    if isinstance(raw, list):
        return ";".join(str(_value(x)) for x in raw) if raw else None
    return raw


def issues_to_frame(issues: list[dict], field_ids: dict[str, str]) -> pd.DataFrame:
    rows = []
    for issue in issues:
        f = issue.get("fields", {})
        status = f.get("status") or {}
        row = {
            "key": issue["key"],
            "summary": f.get("summary"),
            "issue_type": _value(f.get("issuetype")),
            "status": status.get("name"),
            "status_category": (status.get("statusCategory") or {}).get("key"),
            "priority": _value(f.get("priority")),
            "assignee": _value(f.get("assignee")),
            "labels": _value(f.get("labels")),
            "due_date": f.get("duedate"),
            "created": f.get("created"),
            "updated": f.get("updated"),
            "resolved": f.get("resolutiondate"),
        }
        for logical, fid in field_ids.items():
            row[logical] = _value(f.get(fid))
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in ("created", "updated", "resolved"):
        df[col] = pd.to_datetime(df[col], utc=True, format="ISO8601")
    for col in ("due_date", "business_date", "resolved_business_date"):
        if col in df:
            df[col] = pd.to_datetime(df[col], format="%Y-%m-%d").dt.date
    if "break_amount_usd" in df:
        df["break_amount_usd"] = pd.to_numeric(df["break_amount_usd"])
    return df


def fetch_snapshot(client: JiraClient | None = None, jql: str | None = None) -> dict:
    """Pull raw issues plus the field-id mapping used to read them."""
    client = client or JiraClient()
    cfg = load_yaml("jira_fields.yaml")
    field_ids = client.resolve_custom_fields(cfg)
    jql = jql or f"project = {client.settings.project_key} ORDER BY key ASC"
    fields = [*cfg["system_fields"], *field_ids.values()]
    issues = list(client.search(jql, fields))
    log.info("Extracted %d issues from Jira", len(issues))
    return {
        "extracted_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "jql": jql,
        "field_ids": field_ids,
        "field_warnings": client.field_warnings,
        "issues": issues,
    }


_STRIP_KEYS = {"self", "iconUrl", "avatarUrls", "expand", "accountId", "emailAddress"}


def _sanitize(obj: Any) -> Any:
    """Drop site URLs and account identifiers; keep ticket content."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items() if k not in _STRIP_KEYS}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def save_snapshot(snapshot: dict, path: Path) -> None:
    """Persist the raw pull (sanitized) so a report run can be reproduced offline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_sanitize(snapshot), indent=1), encoding="utf-8")


def load_snapshot(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def snapshot_to_frame(snapshot: dict) -> pd.DataFrame:
    return issues_to_frame(snapshot["issues"], snapshot["field_ids"])


def extract_ops_tickets(
    client: JiraClient | None = None, jql: str | None = None
) -> pd.DataFrame:
    """Pull all ops tickets for the configured project as a flat DataFrame."""
    return snapshot_to_frame(fetch_snapshot(client, jql))
