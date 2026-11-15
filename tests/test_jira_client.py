import json

import pandas as pd
import pytest
import responses

from opsreport.config import JiraSettings
from opsreport.extract.jira_client import (
    FieldMappingError,
    JiraClient,
    JiraError,
    extract_ops_tickets,
    issues_to_frame,
)

BASE = "https://example.atlassian.net"
SETTINGS = JiraSettings(BASE, "a@b.com", "token", "OPSREC")

FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False},
    {"id": "customfield_1", "name": "Fund", "custom": True},
    {"id": "customfield_2", "name": "Ops Team", "custom": True},
    {"id": "customfield_3", "name": "Break Type", "custom": True},
    {"id": "customfield_4", "name": "Break Amount USD", "custom": True},
    {"id": "customfield_5", "name": "Business Date", "custom": True},
    {"id": "customfield_6", "name": "Resolved Business Date", "custom": True},
]


def issue(key, fund="Global Equity Fund", amount=1000.0, resolved=None):
    return {
        "key": key,
        "fields": {
            "summary": f"Break {key}",
            "status": {"name": "Done", "statusCategory": {"key": "done"}},
            "priority": {"name": "High"},
            "issuetype": {"name": "Task"},
            "assignee": None,
            "labels": ["seed-v1", "custodian-recon"],
            "duedate": "2026-11-10",
            "created": "2026-11-14T20:30:00.000-0500",
            "updated": "2026-11-14T20:30:00.000-0500",
            "resolutiondate": None,
            "customfield_1": {"value": fund} if fund else None,
            "customfield_2": {"value": "Reconciliation"},
            "customfield_3": {"value": "Cash Break"},
            "customfield_4": amount,
            "customfield_5": "2026-11-06",
            "customfield_6": resolved,
        },
    }


@pytest.fixture
def client():
    return JiraClient(SETTINGS, max_retries=2)


@responses.activate
def test_resolve_custom_fields_maps_names_to_ids(client):
    responses.get(f"{BASE}/rest/api/3/field", json=FIELDS)
    ids = client.resolve_custom_fields()
    assert ids["fund"] == "customfield_1"
    assert ids["break_amount_usd"] == "customfield_4"


@responses.activate
def test_renamed_field_fails_loudly(client):
    renamed = [f if f["name"] != "Fund" else {**f, "name": "Portfolio"} for f in FIELDS]
    responses.get(f"{BASE}/rest/api/3/field", json=renamed)
    with pytest.raises(FieldMappingError, match="fund"):
        client.resolve_custom_fields()


@responses.activate
def test_alias_accepts_old_field_name(client):
    renamed = [f if f["name"] != "Fund" else {**f, "name": "Portfolio"} for f in FIELDS]
    responses.get(f"{BASE}/rest/api/3/field", json=renamed)
    from opsreport.config import load_yaml

    cfg = load_yaml("jira_fields.yaml")
    cfg["aliases"] = {"fund": ["Portfolio"]}
    assert client.resolve_custom_fields(cfg)["fund"] == "customfield_1"


@responses.activate
def test_search_follows_next_page_token(client):
    url = f"{BASE}/rest/api/3/search/jql"
    responses.post(url, json={"issues": [issue("OPSREC-1")], "nextPageToken": "p2",
                              "isLast": False})
    responses.post(url, json={"issues": [issue("OPSREC-2")], "isLast": True})
    keys = [i["key"] for i in client.search("project = OPSREC", ["summary"])]
    assert keys == ["OPSREC-1", "OPSREC-2"]
    second = json.loads(responses.calls[1].request.body)
    assert second["nextPageToken"] == "p2"


@responses.activate
def test_retries_on_429_then_succeeds(client, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    responses.get(f"{BASE}/rest/api/3/field", status=429, headers={"Retry-After": "1"})
    responses.get(f"{BASE}/rest/api/3/field", json=FIELDS)
    assert len(client.all_fields()) == len(FIELDS)


@responses.activate
def test_auth_failure_raises(client):
    responses.get(f"{BASE}/rest/api/3/field", status=401, body="Unauthorized")
    with pytest.raises(JiraError, match="401"):
        client.all_fields()


def test_issues_to_frame_flattens_and_types():
    ids = {"fund": "customfield_1", "ops_team": "customfield_2",
           "break_type": "customfield_3", "break_amount_usd": "customfield_4",
           "business_date": "customfield_5", "resolved_business_date": "customfield_6"}
    df = issues_to_frame([issue("OPSREC-1", resolved="2026-11-09"),
                          issue("OPSREC-2", fund=None)], ids)
    assert df.loc[0, "fund"] == "Global Equity Fund"
    assert pd.isna(df.loc[1, "fund"])
    assert df.loc[0, "labels"] == "seed-v1;custodian-recon"
    assert str(df["created"].dt.tz) == "UTC"
    # 20:30 Eastern on Nov 14 is already Nov 15 in UTC: a reporting-week trap.
    assert df.loc[0, "created"].day == 15
    assert df["break_amount_usd"].dtype == "float64"
    assert pd.isna(df.loc[1, "resolved_business_date"])


@responses.activate
def test_extract_end_to_end(client):
    responses.get(f"{BASE}/rest/api/3/field", json=FIELDS)
    responses.post(f"{BASE}/rest/api/3/search/jql",
                   json={"issues": [issue("OPSREC-1"), issue("OPSREC-2")], "isLast": True})
    df = extract_ops_tickets(client)
    assert len(df) == 2
    assert {"fund", "ops_team", "break_amount_usd", "business_date"} <= set(df.columns)


# ---- INC-001: renamed custom field (issue #1) --------------------------------
def _typed(fields):
    types = {"customfield_4": "float"}
    return [{**f, "schema": {"custom": "cf:" + types.get(f["id"], "select")}}
            for f in fields]


def _lock(client, **overrides):
    pins = {"fund": "customfield_1", "ops_team": "customfield_2",
            "break_type": "customfield_3", "break_amount_usd": "customfield_4",
            "business_date": "customfield_5", "resolved_business_date": "customfield_6"}
    fields = {k: {"id": v, "name": "x", "type": "cf:float" if v == "customfield_4"
                  else "cf:select"} for k, v in pins.items()}
    return {"site_fingerprint": client.site_fingerprint(), "fields": fields, **overrides}


def _renamed(old, new, fields=FIELDS):
    return _typed([{**f, "name": new} if f["name"] == old else f for f in fields])


@responses.activate
def test_renamed_field_followed_by_pinned_id_with_warning(client):
    responses.get(f"{BASE}/rest/api/3/field",
                  json=_renamed("Break Amount USD", "Break Amt (USD)"))
    ids = client.resolve_custom_fields(lock=_lock(client))
    assert ids["break_amount_usd"] == "customfield_4"
    assert len(client.field_warnings) == 1
    assert "renamed to 'Break Amt (USD)'" in client.field_warnings[0]


@responses.activate
def test_deleted_field_still_fails(client):
    gone = [f for f in _typed(FIELDS) if f["id"] != "customfield_4"]
    responses.get(f"{BASE}/rest/api/3/field", json=gone)
    with pytest.raises(FieldMappingError, match="pinned id customfield_4 is gone"):
        client.resolve_custom_fields(lock=_lock(client))


@responses.activate
def test_pinned_id_with_changed_type_fails(client):
    retyped = [{**f, "schema": {"custom": "cf:textfield"}} if f["id"] == "customfield_4"
               else f for f in _renamed("Break Amount USD", "Notes")]
    responses.get(f"{BASE}/rest/api/3/field", json=retyped)
    with pytest.raises(FieldMappingError, match="changed type"):
        client.resolve_custom_fields(lock=_lock(client))


@responses.activate
def test_old_name_reused_by_new_field_fails(client):
    # Admin renamed field 4 and created a *new* field with the old name.
    fields = _renamed("Break Amount USD", "Break Amt (USD)") + [
        {"id": "customfield_99", "name": "Break Amount USD", "custom": True,
         "schema": {"custom": "cf:float"}}]
    responses.get(f"{BASE}/rest/api/3/field", json=fields)
    with pytest.raises(FieldMappingError, match="lock pins customfield_4"):
        client.resolve_custom_fields(lock=_lock(client))


@responses.activate
def test_lock_from_another_site_is_ignored(client):
    responses.get(f"{BASE}/rest/api/3/field",
                  json=_renamed("Break Amount USD", "Break Amt (USD)"))
    with pytest.raises(FieldMappingError):
        client.resolve_custom_fields(lock=_lock(client, site_fingerprint="other"))
