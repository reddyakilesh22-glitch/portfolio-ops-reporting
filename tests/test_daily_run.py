"""Daily run (issue #22): pending-trade matching, break IDs, history, exceptions file."""

import shutil
import sqlite3
from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

from opsreport import daily, history
from opsreport.__main__ import main
from opsreport.extract import excel_loader as el
from opsreport.pipeline import DataQualityError
from opsreport.reconcile.pending import match_pending, unmatched_trades
from opsreport.report.exceptions_report import action_list
from scripts import generate_daily_data as gd

WINDOW = (date(2026, 10, 19), date(2026, 11, 13))       # 20 business days


@pytest.fixture(scope="module")
def raw(tmp_path_factory):
    out = tmp_path_factory.mktemp("daily_raw")
    gd.generate(out, *WINDOW)
    return out


@pytest.fixture(scope="module")
def backfilled(raw, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("daily_run")
    db = tmp / "h.db"
    results = daily.backfill(*WINDOW, raw_dir=raw, db_path=db, out_dir=tmp / "out",
                             snapshot=raw / gd.SNAPSHOT_NAME)
    return db, results


def read(db, table):
    conn = history.connect(db)
    try:
        return history.read(conn, table)
    finally:
        conn.close()


# ------------------------------------------------------------- end to end
def test_ids_and_statuses_match_the_answer_key_every_day(raw, backfilled):
    db, results = backfilled
    assert [r.status for r in results] == ["OK"] * 20
    breaks = read(db, "breaks")
    for d in daily.business_days(*WINDOW):
        got = breaks[breaks["run_date"] == d]
        key = pd.read_csv(raw / "expected" / f"breaks_{d}.csv")
        assert set(zip(got["break_id"], got["status"], strict=True)) == \
               set(zip(key["break_id"], key["status"], strict=True)), d


def test_days_open_counts_business_days_since_first_seen(backfilled):
    db, _ = backfilled
    b = read(db, "breaks")
    first_day = b["run_date"] == WINDOW[0]
    assert (b.loc[first_day, "days_open"] == 0).all()
    one = b.groupby("break_id").filter(lambda g: len(g) >= 3).sort_values("run_date")
    g = one[one["break_id"] == one["break_id"].iloc[0]]
    assert list(g["days_open"]) == list(range(len(g)))


def test_every_actionable_break_is_linked_to_its_ticket(backfilled):
    db, _ = backfilled
    b = read(db, "breaks")
    act, exp = b[b["status"] == "Actionable"], b[b["status"] == "Expected"]
    assert act["ticket_key"].notna().all()
    assert exp["ticket_key"].isna().all()           # timing breaks don't get tickets
    assert set(act["ticket_status"]) <= {"Open", "Overdue", "Resolved",
                                         daily.UNKNOWN_STATUS}


def test_ticket_status_is_point_in_time(backfilled):
    db, _ = backfilled
    b = read(db, "breaks")
    t = read(db, "tickets").set_index("key")
    late = b[(b["ticket_status"] == "Overdue")]
    assert len(late)
    assert (late["run_date"] > late["ticket_key"].map(t["due_date"])).all()
    on_time = b[b["ticket_status"] == "Open"]
    assert (on_time["run_date"] <= on_time["ticket_key"].map(t["due_date"])).all()


def test_rerunning_a_day_replaces_it_and_changes_nothing(raw, backfilled, tmp_path):
    db, _ = backfilled
    copy = tmp_path / "h.db"
    shutil.copy(db, copy)
    day = date(2026, 11, 4)
    before = read(copy, "breaks")
    daily.run_daily(day, raw, copy, tmp_path / "out", raw / gd.SNAPSHOT_NAME,
                    write_excel=False)
    after = read(copy, "breaks")
    pd.testing.assert_frame_equal(
        before.sort_values(["run_date", "break_id"]).reset_index(drop=True),
        after.sort_values(["run_date", "break_id"]).reset_index(drop=True))
    assert len(read(copy, "runs")) == 20


def test_a_bad_day_is_recorded_and_the_backfill_continues(raw, tmp_path):
    bad_raw = tmp_path / "raw"
    shutil.copytree(raw, bad_raw)
    bad = date(2026, 10, 21)
    (bad_raw / bad.isoformat() / f"pending_trades_{bad:%Y%m%d}.xlsx").unlink()
    db = tmp_path / "h.db"
    results = daily.backfill(WINDOW[0], date(2026, 10, 23), bad_raw, db, tmp_path / "out",
                             bad_raw / gd.SNAPSHOT_NAME)
    assert [r.status for r in results] == ["OK", "OK", "FAILED", "OK", "OK"]
    runs = read(db, "runs").set_index("run_date")
    assert runs.loc[bad, "status"] == "FAILED"
    assert "pending_trades file not found" in runs.loc[bad, "message"]
    assert not (read(db, "breaks")["run_date"] == bad).any()
    dq = read(db, "data_quality")
    assert (dq.loc[dq["run_date"] == bad, "severity"] == "ERROR").any()


def test_data_quality_error_records_failed_day(raw, tmp_path, monkeypatch):
    def doubled(as_of, path=None):
        df = orig(as_of, path)
        return pd.concat([df, df.iloc[[0]]], ignore_index=True)
    orig = el.load_accounting
    monkeypatch.setattr(el, "load_accounting", doubled)
    db = tmp_path / "h.db"
    with pytest.raises(DataQualityError, match="nothing published"):
        daily.run_daily(WINDOW[0], raw, db, tmp_path, raw / gd.SNAPSHOT_NAME)
    assert read(db, "runs")["status"].tolist() == ["FAILED"]


def test_missing_snapshot_is_a_warning_not_a_failure(raw, tmp_path):
    res = daily.run_daily(WINDOW[0], raw, tmp_path / "h.db", tmp_path,
                          tmp_path / "nope.json", write_excel=False)
    assert res.status == "OK"
    assert "no_ticket_snapshot" in set(res.issues["check"])
    assert res.breaks["ticket_key"].isna().all()


# ------------------------------------------------------- exceptions file
def test_exceptions_workbook_lists_actionable_breaks_most_urgent_first(backfilled, tmp_path):
    db, results = backfilled
    last = results[-1]
    wb = load_workbook(last.exceptions_path)
    assert wb.sheetnames == ["Action Today", "Expected", "Cleared", "Data Quality"]
    ws = wb["Action Today"]
    header = [c.value for c in ws[7]]
    rows = [[c.value for c in r] for r in ws.iter_rows(min_row=8) if r[1].value]
    act = last.breaks[last.breaks["status"] == "Actionable"]
    assert len(rows) == len(act)
    status = [r[header.index("Ticket Status")] for r in rows]
    assert status == sorted(status, key=lambda s: {"Overdue": 0}.get(s, 1))
    seen = ws.cell(8, header.index("First Seen") + 1)
    assert seen.is_date                                  # a real date, not a serial


def test_action_list_puts_breaks_without_tickets_before_open_ones():
    b = pd.DataFrame({
        "status": ["Actionable"] * 3, "first_seen": [date(2026, 1, 2)] * 3,
        "explanation": ["", "", "x"], "likely_cause": ["a", "b", "c"],
        "ticket_status": ["Open", None, "Overdue"], "exposure_usd": [9e6, 1.0, 5.0],
        "fund_code": ["F"] * 3, "ticker": ["A", "B", "C"]})
    out = action_list(b, date(2026, 1, 2))
    assert list(out["ticker"]) == ["C", "B", "A"]
    assert list(out["what_to_check"]) == ["x", "b", "a"]
    assert list(out["new"]) == ["NEW"] * 3


def test_text_from_inputs_cannot_become_a_formula(backfilled, tmp_path):
    db, results = backfilled
    from opsreport.report.exceptions_report import write_exceptions
    last = results[-1]
    b = last.breaks.copy()
    b.loc[b.index[0], "ticker"] = '=HYPERLINK("http://x","click")'
    b.loc[b.index[0], "status"] = "Actionable"
    path = write_exceptions(last.as_of, None, b, last.cleared, last.issues,
                            tmp_path / "x.xlsx")
    ws = load_workbook(path)["Action Today"]
    cells = [c for r in ws.iter_rows(min_row=8) for c in r if c.value
             and str(c.value).startswith("=")]
    assert cells and all(c.data_type == "s" for c in cells)


# ------------------------------------------------------ pending matching
def brk(qdiff, btype="Quantity Break"):
    return pd.DataFrame({"fund_code": ["F"], "isin": ["X1"], "break_type": [btype],
                         "quantity_diff": [qdiff]})


def trades(*rows):
    return pd.DataFrame(rows, columns=["trade_id", "fund_code", "isin", "signed_quantity",
                                       "settle_date"])


AS_OF = date(2026, 11, 13)


@pytest.mark.parametrize("settle, status, text", [
    (date(2026, 11, 16), "Expected", "settles Mon 16 Nov"),
    (date(2026, 11, 13), "Actionable", "Settlement failed"),
    (date(2026, 11, 10), "Actionable", "Settlement failed"),
])
def test_fully_explained_break_is_expected_only_before_settlement(settle, status, text):
    out = match_pending(brk(-1000), trades(("T1", "F", "X1", 1000, settle)), AS_OF)
    assert out.loc[0, "status"] == status and text in out.loc[0, "explanation"]


def test_partly_explained_break_stays_actionable_with_the_gap():
    out = match_pending(brk(-1000), trades(("T1", "F", "X1", 600, date(2026, 11, 16))),
                        AS_OF)
    assert out.loc[0, "status"] == "Actionable"
    assert "600 of 1,000" in out.loc[0, "explanation"]
    assert "400 unexplained" in out.loc[0, "explanation"]


def test_sell_and_several_trades_net_off():
    t = trades(("T1", "F", "X1", -500, date(2026, 11, 16)),
               ("T2", "F", "X1", -300, date(2026, 11, 17)))
    out = match_pending(brk(800), t, AS_OF)          # custodian 800 above accounting
    assert out.loc[0, "status"] == "Expected"
    assert "T1, T2" in out.loc[0, "explanation"] and "17 Nov" in out.loc[0, "explanation"]


def test_new_position_still_at_broker_is_expected():
    out = match_pending(brk(-1000, "Missing Trade"),
                        trades(("T1", "F", "X1", 1000, date(2026, 11, 16))), AS_OF)
    assert out.loc[0, "status"] == "Expected"


def test_price_break_is_never_explained_by_a_trade():
    out = match_pending(brk(-1000, "Price Break"),
                        trades(("T1", "F", "X1", 1000, date(2026, 11, 16))), AS_OF)
    assert out.loc[0, "status"] == "Actionable" and out.loc[0, "explanation"] == ""


def test_open_trade_with_no_break_is_reported():
    t = trades(("T1", "F", "X1", 1, AS_OF), ("T2", "F", "OTHER", 1, AS_OF))
    assert list(unmatched_trades(t, brk(-1))["trade_id"]) == ["T2"]


# ------------------------------------------------- pending-trades loader
@pytest.fixture
def pending_file(raw, tmp_path):
    src = raw / WINDOW[1].isoformat() / f"pending_trades_{WINDOW[1]:%Y%m%d}.xlsx"
    dst = tmp_path / src.name
    shutil.copy(src, dst)
    return dst


def edit(path, header, value, row=2):
    wb = load_workbook(path)
    ws = wb.active
    col = next(c.column for c in ws[1] if c.value == header)
    ws.cell(row, col).value = value
    wb.save(path)


def test_loader_signs_quantities(pending_file):
    df = el.load_pending(WINDOW[1], pending_file)
    assert len(df) and ((df["signed_quantity"] > 0) == df["side"].eq("Buy")).all()


@pytest.mark.parametrize("header, value, message", [
    ("Side", "Short", "must be Buy or Sell"),
    ("Quantity", -5, "positive number"),
    ("Quantity", "TBC", "non-numeric"),
    ("Trade Date", date(2026, 11, 20), "after the report date"),
    ("Contractual Settle Date", None, "blank or not a date"),
    ("Contractual Settle Date", date(2026, 1, 2), "before the trade date"),
    ("Report Date", date(2026, 11, 12), "report dates"),
    ("Fund Code", "XYZ", "unknown fund code"),
])
def test_loader_rejects_bad_rows(pending_file, header, value, message):
    edit(pending_file, header, value)
    with pytest.raises(el.SourceFileError, match=message):
        el.load_pending(WINDOW[1], pending_file)


def test_loader_rejects_repeated_trade_id(pending_file):
    first = el.load_pending(WINDOW[1], pending_file)["trade_id"].iloc[0]
    edit(pending_file, "Trade ID", first, row=3)
    with pytest.raises(el.SourceFileError, match="repeated"):
        el.load_pending(WINDOW[1], pending_file)


def test_loader_accepts_an_empty_report(pending_file):
    wb = load_workbook(pending_file)
    wb.active.delete_rows(2, wb.active.max_row)
    wb.save(pending_file)
    assert el.load_pending(WINDOW[1], pending_file).empty


# -------------------------------------------------------------- history
def test_previous_breaks_skips_failed_runs(tmp_path):
    conn = history.connect(tmp_path / "h.db")
    b = pd.DataFrame({"break_id": ["B1"], "fund_code": ["F"], "isin": ["X"],
                      "first_seen": [date(2026, 1, 5)]})
    history.save_day(conn, {"run_date": date(2026, 1, 5), "status": "OK"}, b)
    history.save_day(conn, {"run_date": date(2026, 1, 6), "status": "FAILED"})
    prev_date, prev = history.previous_breaks(conn, date(2026, 1, 7))
    assert prev_date == date(2026, 1, 5) and list(prev["break_id"]) == ["B1"]
    assert history.previous_breaks(conn, date(2026, 1, 5))[0] is None
    conn.close()


def test_incompatible_history_file_is_refused(tmp_path):
    path = tmp_path / "h.db"
    sqlite3.connect(path).execute("PRAGMA user_version = 99").connection.close()
    with pytest.raises(history.HistoryError, match="rebuild"):
        history.connect(path)


def test_version_1_history_is_upgraded_in_place(tmp_path):
    path = tmp_path / "h.db"
    old = sqlite3.connect(path)
    old.executescript(history.SCHEMA.replace(
        "    holdings_compared    INTEGER,\n    shares_outstanding   REAL,\n", ""))
    old.execute("INSERT INTO aum_daily (run_date, fund_code, aum) VALUES "
                "('2026-01-05', 'GEF', 1.0)")
    old.execute("PRAGMA user_version = 1")
    old.commit()
    old.close()
    conn = history.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
    a = history.read(conn, "aum_daily")
    assert a["aum"].tolist() == [1.0] and a["holdings_compared"].isna().all()
    conn.close()
    history.connect(path).close()                   # idempotent


def test_failed_status_must_be_ok_or_failed(tmp_path):
    conn = history.connect(tmp_path / "h.db")
    with pytest.raises(sqlite3.IntegrityError):
        history.save_day(conn, {"run_date": date(2026, 1, 5), "status": "maybe"})
    conn.close()


# ------------------------------------------------------------------ CLI
def test_cli_daily_and_backfill_exit_codes(raw, tmp_path, capsys):
    args = ["--raw-dir", str(raw), "--db", str(tmp_path / "h.db"), "--out", str(tmp_path),
            "--snapshot", str(raw / gd.SNAPSHOT_NAME)]
    assert main(["daily", "--as-of", "2026-11-13", *args]) == 0
    assert "Actionable:" in capsys.readouterr().out
    assert main(["backfill", "--from", "2026-11-12", "--to", "2026-11-13", *args]) == 0
    assert main(["daily", "--as-of", "2026-11-14", *args]) == 1      # Saturday: no files
    assert main(["backfill", "--from", "2026-11-13", "--to", "2026-11-16", *args]) == 2
    assert "FAILED 2026-11-16" in capsys.readouterr().out


def test_cli_daily_data_quality_error_exits_2(raw, tmp_path, monkeypatch):
    monkeypatch.setattr(el, "load_fx", lambda *a, **k: pd.Series({"USD": 1.0}))
    assert main(["daily", "--as-of", "2026-11-13", "--raw-dir", str(raw), "--db",
                 str(tmp_path / "h.db"), "--out", str(tmp_path)]) == 2
