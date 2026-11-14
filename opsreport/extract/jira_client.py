"""Jira Cloud REST v3 client used by the reporting pipeline.

- Auth: basic auth with email + API token (from .env, never committed).
- Search: uses the enhanced `/rest/api/3/search/jql` endpoint, which pages with
  `nextPageToken`. The old `/rest/api/3/search` (startAt/total) was removed.
- Fields: custom fields are resolved by *display name* from
  config/jira_fields.yaml, so the code never hardcodes customfield ids.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

import pandas as pd
import requests

from opsreport.config import JiraSettings, load_jira_settings, load_yaml

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

    def resolve_custom_fields(self, field_config: dict | None = None) -> dict[str, str]:
        """Map logical names (e.g. 'fund') to Jira ids (e.g. 'customfield_10042').

        Raises FieldMappingError listing every field it could not find, rather
        than silently returning empty columns.
        """
        cfg = field_config or load_yaml("jira_fields.yaml")
        aliases = cfg.get("aliases") or {}
        by_name: dict[str, list[str]] = {}
        for f in self.all_fields():
            if f.get("custom"):
                by_name.setdefault(f["name"], []).append(f["id"])

        resolved, problems = {}, []
        for logical, spec in cfg["custom_fields"].items():
            candidates = [spec["name"], *aliases.get(logical, [])]
            hits = [(n, by_name[n]) for n in candidates if n in by_name]
            if not hits:
                problems.append(f"{logical}: no Jira field named {candidates}")
            elif len(hits[0][1]) > 1:
                problems.append(f"{logical}: '{hits[0][0]}' is ambiguous {hits[0][1]}")
            else:
                resolved[logical] = hits[0][1][0]
        if problems:
            raise FieldMappingError(
                "Custom field mapping failed (check config/jira_fields.yaml):\n  "
                + "\n  ".join(problems)
            )
        return resolved

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


def extract_ops_tickets(
    client: JiraClient | None = None, jql: str | None = None
) -> pd.DataFrame:
    """Pull all ops tickets for the configured project as a flat DataFrame."""
    client = client or JiraClient()
    cfg = load_yaml("jira_fields.yaml")
    field_ids = client.resolve_custom_fields(cfg)
    jql = jql or f"project = {client.settings.project_key} ORDER BY key ASC"
    fields = [*cfg["system_fields"], *field_ids.values()]
    issues = list(client.search(jql, fields))
    log.info("Extracted %d issues from Jira", len(issues))
    return issues_to_frame(issues, field_ids)
