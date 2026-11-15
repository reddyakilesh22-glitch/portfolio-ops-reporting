"""Audit group A (issue #11): cases that produced wrong numbers with no error."""

from datetime import date

import pandas as pd
import pytest
import responses
from openpyxl import load_workbook

from opsreport import pipeline
from opsreport.config import ROOT, JiraSettings
from opsreport.extract import excel_loader as el
from opsreport.extract.jira_client import JiraClient, load_snapshot, snapshot_to_frame
from opsreport.reconcile.positions import reconcile
from opsreport.transform import jira_metrics as jm
from opsreport.validate.checks import ERROR, check_jira, check_positions

AS_OF, PRIOR = date(2026, 11, 13), date(2026, 11, 6)
CUST = ROOT / "data/raw/2026-11-13/custodian_holdings_20261113.xlsx"
SNAP = ROOT / "data/snapshots/jira_2026-11-13.json"


def edit_custodian(tmp_path, fn):
    wb = load_workbook(CUST)
    fn(wb)
    out = tmp_path / CUST.name
    wb.save(out)
    return out


def col(ws, header):
    return next(c.column for c in ws[6] if c.value == header)


def row_of(ws, ticker):
    t = col(ws, "Ticker")
    return next(r for r in range(7, ws.max_row + 1) if ws.cell(r, t).value == ticker)


@pytest.fixture(scope="module")
def tickets():
    return snapshot_to_frame(load_snapshot(SNAP))


# 1 -------------------------------------------------------------------------
@pytest.mark.parametrize("blank", [None, "N/A"])
def test_blank_custodian_quantity_is_an_error_not_zero(tmp_path, blank):
    def f(wb):
        ws = wb.worksheets[0]
        ws.cell(row_of(ws, "MSFT"), col(ws, "Shares/Par Value")).value = blank
    c = el.load_custodian(AS_OF, edit_custodian(tmp_path, f))
    assert pd.isna(c.loc[c["ticker"] == "MSFT", "quantity"].iloc[0])
    errors = [i for i in check_positions(c, el.load_fx(AS_OF))
              if i.severity == ERROR and "MSFT" in i.record]
    assert [i.check for i in errors] == ["required_field"]


# 2 -------------------------------------------------------------------------
def test_duplicate_custodian_row_is_an_error_not_a_second_lot(tmp_path):
    def f(wb):
        ws = wb["4471-MAI-05"]
        src = row_of(ws, "AGG")
        values = [c.value for c in ws[src]]
        ws.insert_rows(src + 1)
        for i, v in enumerate(values, start=1):
            ws.cell(src + 1, i).value = v
        mv = col(ws, "Base Market Value (USD)")
        total = next(r for r in range(7, ws.max_row + 1) if ws.cell(r, 1).value == "Total")
        ws.cell(total, mv).value = round(ws.cell(total, mv).value + float(values[mv - 1]), 2)
    c = el.load_custodian(AS_OF, edit_custodian(tmp_path, f))
    errors = [i for i in check_positions(c, el.load_fx(AS_OF)) if i.severity == ERROR]
    assert [(i.check, "AGG" in i.record) for i in errors] == [("duplicate_lot", True)]


def test_lots_at_different_locations_are_still_fine():
    c = el.load_custodian(AS_OF)
    assert (c["lots"] > 1).sum() == 9 and not c["duplicate_lots"].any()


# 3 -------------------------------------------------------------------------
def test_prior_week_inputs_are_validated(tmp_path, monkeypatch):
    orig = el.load_accounting

    def doubled_prior(as_of, path=None, **kw):
        df = orig(as_of, path, **kw)
        return pd.concat([df, df[df["fund_code"] == "GEF"]], ignore_index=True) \
            if as_of == PRIOR else df
    monkeypatch.setattr(el, "load_accounting", doubled_prior)
    with pytest.raises(pipeline.DataQualityError, match="prior week"):
        pipeline.run(AS_OF, PRIOR, out_dir=tmp_path)


# 4 -------------------------------------------------------------------------
@responses.activate
def test_paging_continues_when_islast_is_absent():
    url = "https://x.atlassian.net/rest/api/3/search/jql"
    responses.post(url, json={"issues": [{"key": "A-1"}], "nextPageToken": "p2"})
    responses.post(url, json={"issues": [{"key": "A-2"}]})
    client = JiraClient(JiraSettings("https://x.atlassian.net", "a", "b", "X"))
    assert [i["key"] for i in client.search("q", ["key"])] == ["A-1", "A-2"]


@responses.activate
def test_paging_stops_if_jira_repeats_a_page_token():
    url = "https://x.atlassian.net/rest/api/3/search/jql"
    for _ in range(3):
        responses.post(url, json={"issues": [{"key": "A-1"}], "nextPageToken": "same"})
    client = JiraClient(JiraSettings("https://x.atlassian.net", "a", "b", "X"))
    with pytest.raises(RuntimeError, match="repeated"):
        list(client.search("q", ["key"]))


# 5 -------------------------------------------------------------------------
def test_ticket_without_due_date_is_excluded_from_sla_not_failed(tickets):
    x = tickets.copy()
    met = jm.prepare(x, AS_OF)
    key = met.loc[met["sla_met"] == 1.0, "key"].iloc[0]
    x.loc[x["key"] == key, "due_date"] = None
    after = jm.prepare(x, AS_OF).set_index("key").loc[key]
    assert pd.isna(after["sla_met"]) and not after["overdue"]
    assert "due_date_blank" in {i.check for i in check_jira(x)}


# 6 -------------------------------------------------------------------------
def test_tickets_without_team_stay_in_team_table(tickets):
    x = tickets.copy()
    idx = x[x["status_category"] != "done"].index[:3]
    x.loc[idx, "ops_team"] = None
    p = jm.prepare(x, AS_OF)
    assert jm.by_team(p, AS_OF)["open"].sum() == jm.kpis(p, AS_OF)["open_tickets"] == 19
    assert "(blank)" in set(jm.by_team(p, AS_OF)["ops_team"])


# 7 -------------------------------------------------------------------------
def test_live_pull_never_overwrites_an_existing_snapshot(tmp_path, monkeypatch):
    snap_dir = tmp_path / "snapshots"
    snap_dir.mkdir()
    existing = snap_dir / "jira_2026-11-13.json"
    existing.write_text(SNAP.read_text())
    monkeypatch.setattr(pipeline, "SNAPSHOT_DIR", snap_dir)
    monkeypatch.setattr(pipeline, "fetch_snapshot",
                        lambda: {**load_snapshot(SNAP), "extracted_at": "NEW"})

    with pytest.raises(pipeline.SnapshotExistsError, match="--refresh-snapshot"):
        pipeline.run(AS_OF, jira="live", out_dir=tmp_path)
    assert load_snapshot(existing)["extracted_at"] != "NEW"

    pipeline.run(AS_OF, jira="live", out_dir=tmp_path, refresh_snapshot=True)
    assert load_snapshot(existing)["extracted_at"] == "NEW"


# 8 -------------------------------------------------------------------------
def test_unprocessed_split_is_ranked_by_position_at_risk():
    x = el.load_all(AS_OF)
    b = reconcile(x["custodian"], x["accounting"])
    asml = b[b["ticker"] == "ASML"].iloc[0]
    assert asml["abs_base_mv_diff"] < 25            # the $ difference is tiny
    assert asml["exposure_usd"] > 6_000_000         # the whole position is at risk
    assert asml["materiality"] == "High"
    assert b["exposure_usd"].is_monotonic_decreasing
    assert list(b["ticker"]).index("ASML") < 4      # no longer last of 11


# 9 -------------------------------------------------------------------------
def test_second_difference_is_mentioned():
    base = dict(fund_code="F1", isin="US0378331005", ticker="T", description="d",
                asset_class="Equity", currency="USD", quantity=100.0, price=10.0,
                local_mv=1000.0, fx_rate=1.0, base_mv=1000.0)
    cust = {**base, "quantity": 120.0, "price": 11.0, "local_mv": 1320.0, "base_mv": 1320.0}
    b = reconcile(pd.DataFrame([cust]), pd.DataFrame([base]))
    assert b.loc[0, "break_type"] == "Quantity Break"
    assert "price also differs" in b.loc[0, "likely_cause"].lower()
