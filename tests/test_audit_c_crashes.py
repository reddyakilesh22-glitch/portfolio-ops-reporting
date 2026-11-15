"""Audit group C (issue #15): inputs that used to end in a raw traceback."""

import json
from datetime import date

import pandas as pd
import pytest
import requests
import responses
from openpyxl import load_workbook

from opsreport import pipeline
from opsreport.__main__ import main
from opsreport.config import ROOT, JiraSettings
from opsreport.extract import excel_loader as el
from opsreport.extract.jira_client import JiraClient, JiraError, load_snapshot

AS_OF, PRIOR = date(2026, 11, 13), date(2026, 11, 6)
BASE = "https://x.atlassian.net"
SNAP = ROOT / "data/snapshots/jira_2026-11-13.json"
CUST = ROOT / "data/raw/2026-11-13/custodian_holdings_20261113.xlsx"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    return JiraClient(JiraSettings(BASE, "a", "b", "X"), max_retries=3)


# 13 ------------------------------------------------------------------------
@responses.activate
def test_network_error_is_retried_then_reported_as_jira_error(client):
    responses.get(f"{BASE}/rest/api/3/field", body=requests.ConnectionError("dns failure"))
    with pytest.raises(JiraError, match="network error"):
        client.all_fields()
    assert len(responses.calls) == 3


@responses.activate
def test_network_error_then_success(client):
    responses.get(f"{BASE}/rest/api/3/field", body=requests.Timeout("slow"))
    responses.get(f"{BASE}/rest/api/3/field", json=[])
    assert client.all_fields() == []


@responses.activate
def test_retry_after_given_as_a_date_does_not_crash(client):
    responses.get(f"{BASE}/rest/api/3/field", status=429,
                  headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    responses.get(f"{BASE}/rest/api/3/field", json=[])
    assert client.all_fields() == []


@responses.activate
def test_post_that_creates_data_is_not_retried_on_server_error(client):
    responses.post(f"{BASE}/rest/api/3/issue/bulk", status=502)
    with pytest.raises(JiraError, match="502"):
        client.post("/rest/api/3/issue/bulk", {"issueUpdates": []})
    assert len(responses.calls) == 1          # a retry could create duplicates


@responses.activate
def test_search_post_is_retried_because_it_only_reads(client):
    url = f"{BASE}/rest/api/3/search/jql"
    responses.post(url, status=503)
    responses.post(url, json={"issues": [{"key": "A-1"}], "isLast": True})
    assert [i["key"] for i in client.search("q", ["key"])] == ["A-1"]


# 14 ------------------------------------------------------------------------
def test_empty_jira_project_still_produces_a_report(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({**load_snapshot(SNAP), "issues": []}))
    res = pipeline.run(AS_OF, PRIOR, snapshot=empty, out_dir=tmp_path)
    k = res.data.jira_kpis
    assert (k["open_tickets"], k["overdue_tickets"], k["sla_met_4wk"]) == (0, 0, None)
    assert res.report_path.exists() and len(res.breaks) == 11


# 15 ------------------------------------------------------------------------
def test_old_ticket_outside_calendar_warns_instead_of_stopping(tmp_path):
    snap = load_snapshot(SNAP)
    fid = snap["field_ids"]["business_date"]
    open_issue = next(i for i in snap["issues"]
                      if i["fields"]["status"]["statusCategory"]["key"] != "done")
    open_issue["fields"][fid] = "2025-12-30"
    old = tmp_path / "old.json"
    old.write_text(json.dumps(snap))
    res = pipeline.run(AS_OF, snapshot=old, out_dir=tmp_path)
    gap = res.issues[res.issues["check"] == "calendar_gap"]
    assert len(gap) == 1 and "2025" in gap["detail"].iloc[0]
    assert res.data.jira_kpis["oldest_open_bdays"] > 200


# 16 ------------------------------------------------------------------------
def test_allow_errors_survives_a_duplicate_position(tmp_path, monkeypatch):
    orig = el.load_accounting
    monkeypatch.setattr(el, "load_accounting", lambda as_of, path=None: pd.concat(
        [orig(as_of, path), orig(as_of, path).iloc[[0]]], ignore_index=True))
    with pytest.raises(pipeline.DataQualityError):
        pipeline.run(AS_OF, out_dir=tmp_path)
    res = pipeline.run(AS_OF, out_dir=tmp_path, allow_errors=True)
    assert res.report_path.exists()
    assert "forced_run" in set(res.issues["check"])
    assert len(res.breaks) == 11              # duplicate ignored, not double-counted


# 18 ------------------------------------------------------------------------
def test_text_in_a_number_column_names_file_sheet_column_and_value(tmp_path):
    wb = load_workbook(CUST)
    ws = wb.worksheets[0]
    c = next(x.column for x in ws[6] if x.value == "Shares/Par Value")
    ws.cell(8, c).value = "TBC"
    bad = tmp_path / CUST.name
    wb.save(bad)
    with pytest.raises(el.SourceFileError) as exc:
        el.load_custodian(AS_OF, bad)
    msg = str(exc.value)
    assert CUST.name in msg and "4471-GEF-01" in msg
    assert "Shares/Par Value" in msg and "'TBC'" in msg


def test_cli_reports_bad_number_in_one_line(tmp_path, capsys):
    raw = tmp_path / "raw"
    for d in ("2026-11-13",):
        (raw / d).mkdir(parents=True)
        for f in (ROOT / "data/raw" / d).iterdir():
            (raw / d / f.name).write_bytes(f.read_bytes())
    wb = load_workbook(raw / "2026-11-13" / CUST.name)
    ws = wb.worksheets[0]
    ws.cell(8, next(x.column for x in ws[6] if x.value == "Local Price")).value = "n.a."
    wb.save(raw / "2026-11-13" / CUST.name)
    code = main(["run", "--as-of", "2026-11-13", "--raw-dir", str(raw),
                 "--out", str(tmp_path / "out")])
    err = capsys.readouterr().err
    assert code == 1 and "Local Price" in err and "Traceback" not in err
