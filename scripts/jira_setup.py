"""One-time (idempotent) Jira setup for the demo site.

Creates a company-managed Kanban project, the custom fields defined in
config/jira_fields.yaml (with select options), and puts those fields on the
project's screens so the API can set them.

    python -m scripts.jira_setup
"""

from __future__ import annotations

import logging

from opsreport.config import load_yaml
from opsreport.extract.jira_client import JiraClient, JiraError

log = logging.getLogger("jira_setup")

PROJECT_NAME = "Portfolio Ops Reconciliation"
KANBAN_CLASSIC = "com.pyxis.greenhopper.jira:gh-simplified-kanban-classic"
CF = "com.atlassian.jira.plugin.system.customfieldtypes:"
FIELD_TYPES = {
    "select": (CF + "select", CF + "multiselectsearcher"),
    "number": (CF + "float", CF + "exactnumber"),
    "date": (CF + "datepicker", CF + "daterange"),
}
EXTRA_SCREEN_FIELDS = ["duedate", "priority", "labels"]


def ensure_project(jira: JiraClient) -> dict:
    key = jira.settings.project_key
    try:
        project = jira.get(f"/rest/api/3/project/{key}")
        log.info("Project %s exists (style=%s)", key, project.get("style"))
        if project.get("style") != "classic":
            raise SystemExit(f"{key} is team-managed; pick another key in .env")
        return project
    except JiraError as exc:
        if "404" not in str(exc):
            raise
    me = jira.get("/rest/api/3/myself")
    jira.post(
        "/rest/api/3/project",
        {
            "key": key,
            "name": PROJECT_NAME,
            "projectTypeKey": "software",
            "projectTemplateKey": KANBAN_CLASSIC,
            "leadAccountId": me["accountId"],
            "assigneeType": "UNASSIGNED",
            "description": "Demo: custodian vs. accounting reconciliation breaks.",
        },
    )
    log.info("Created company-managed project %s", key)
    return jira.get(f"/rest/api/3/project/{key}")


def ensure_field(jira: JiraClient, spec: dict, existing: dict[str, str]) -> str:
    name = spec["name"]
    if name in existing:
        fid = existing[name]
        log.info("Field '%s' exists (%s)", name, fid)
    else:
        ftype, searcher = FIELD_TYPES[spec["type"]]
        fid = jira.post(
            "/rest/api/3/field",
            {"name": name, "type": ftype, "searcherKey": searcher,
             "description": "Portfolio ops reporting demo field"},
        )["id"]
        log.info("Created field '%s' (%s)", name, fid)

    if spec["type"] == "select":
        ensure_options(jira, fid, spec["options"])
    return fid


def ensure_options(jira: JiraClient, fid: str, options: list[str]) -> None:
    contexts = jira.get(f"/rest/api/3/field/{fid}/context")["values"]
    if contexts:
        ctx = contexts[0]["id"]
    else:
        ctx = jira.post(
            f"/rest/api/3/field/{fid}/context",
            {"name": "Default", "projectIds": [], "issueTypeIds": []},
        )["id"]
    have = {
        o["value"]
        for o in jira.get(f"/rest/api/3/field/{fid}/context/{ctx}/option",
                          maxResults=100)["values"]
    }
    missing = [o for o in options if o not in have]
    if missing:
        jira.post(
            f"/rest/api/3/field/{fid}/context/{ctx}/option",
            {"options": [{"value": o} for o in missing]},
        )
        log.info("  added options to %s: %s", fid, missing)


def ensure_on_screens(jira: JiraClient, field_ids: list[str]) -> None:
    key = jira.settings.project_key
    screens = jira.get("/rest/api/3/screens", queryString=key, maxResults=100)["values"]
    screens = [s for s in screens if s["name"].startswith(f"{key}:")]
    if not screens:
        raise SystemExit(f"No screens found for {key}")
    for screen in screens:
        sid = screen["id"]
        tab = jira.get(f"/rest/api/3/screens/{sid}/tabs")[0]["id"]
        on_tab = {f["id"] for f in jira.get(f"/rest/api/3/screens/{sid}/tabs/{tab}/fields")}
        for fid in field_ids:
            if fid not in on_tab:
                jira.post(f"/rest/api/3/screens/{sid}/tabs/{tab}/fields", {"fieldId": fid})
                log.info("  added %s to screen '%s'", fid, screen["name"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    jira = JiraClient()
    cfg = load_yaml("jira_fields.yaml")

    ensure_project(jira)
    existing = {f["name"]: f["id"] for f in jira.all_fields() if f.get("custom")}
    field_ids = [ensure_field(jira, spec, existing) for spec in cfg["custom_fields"].values()]
    ensure_on_screens(jira, field_ids + EXTRA_SCREEN_FIELDS)

    resolved = jira.resolve_custom_fields(cfg)
    log.info("Setup complete. Field mapping: %s", resolved)


if __name__ == "__main__":
    main()
