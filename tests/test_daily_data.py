"""The daily synthetic history (issue #21) is realistic and internally consistent.

The full six months are simulated in memory (fast). An eight-week window,
which covers the October slowdown, is written to disk and pushed through the
real loaders and reconciliation.
"""

from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

from opsreport.config import ROOT
from opsreport.extract import excel_loader as el
from opsreport.extract.jira_client import load_snapshot, snapshot_to_frame
from opsreport.reconcile.positions import reconcile
from opsreport.validate.checks import ERROR, check_jira, check_positions
from scripts import generate_daily_data as gd
from scripts import generate_excel_data as weekly

WINDOW = (date(2026, 9, 21), date(2026, 11, 13))


@pytest.fixture(scope="module")
def full():
    sim = gd.Simulator()
    return sim, sim.run(write=False)


@pytest.fixture(scope="module")
def window(tmp_path_factory):
    out = tmp_path_factory.mktemp("daily")
    return out, gd.generate(out, *WINDOW)


def keyset(df: pd.DataFrame, fund: str = "fund") -> set:
    return set(zip(df[fund], df["isin"], df["break_type"], strict=True))


# ------------------------------------------------------------ the books
def test_every_day_loads_cleanly_and_reconciles_to_the_answer_key(window):
    out, days = window
    assert len(days) == 40          # 8 weeks, no holidays
    for day in days:
        x = el.load_all(day.as_of, out)
        issues = check_positions(x["custodian"], x["fx"]) \
            + check_positions(x["accounting"], x["fx"])
        assert [i for i in issues if i.severity == ERROR] == [], day.as_of
        got = keyset(reconcile(x["custodian"], x["accounting"]), "fund_code")
        want = keyset(pd.read_csv(out / "expected" / f"breaks_{day.as_of}.csv"))
        assert got == want, day.as_of


def test_generation_is_deterministic(full):
    _, a = full
    b = gd.Simulator().run(write=False)
    assert [d.key for d in a] == [d.key for d in b]


def test_every_break_type_occurs_over_six_months(full):
    _, days = full
    seen = {(k["break_type"], k["status"]) for d in days for k in d.key}
    assert {t for t, _ in seen} == set(gd.TEAM)
    assert ("Quantity Break", "Expected") in seen        # pending trades
    assert ("Quantity Break", "Actionable") in seen      # failed settlements


def test_month_end_holidays_and_weekends_are_skipped(full):
    _, days = full
    dates = [d.as_of for d in days]
    assert all(d.weekday() < 5 for d in dates)
    assert date(2026, 7, 3) not in dates and date(2026, 9, 7) not in dates
    assert dates[0] == gd.START and dates[-1] == gd.END


# ------------------------------------------------------ pending trades
def test_trade_break_is_expected_until_its_settlement_date(full):
    _, days = full
    for d in days:
        for k in d.key:
            if k["break_type"] == "Quantity Break":
                assert (k["status"] == "Expected") == (d.as_of < k["actionable_from"])


def test_pending_file_lists_exactly_the_open_trades(window):
    out, days = window
    day = days[-1]
    df = pd.read_excel(out / day.as_of.isoformat()
                       / f"pending_trades_{day.as_of:%Y%m%d}.xlsx", sheet_name="Open Trades")
    trade_breaks = {(k["fund"], k["isin"]) for k in day.key
                    if k["break_type"] == "Quantity Break"}
    assert set(zip(df["Fund Code"], df["Security ID"], strict=True)) == trade_breaks
    assert (pd.to_datetime(df["Trade Date"]).dt.date <= day.as_of).all()


def test_a_position_has_one_break_at_a_time_with_a_gap_between(full):
    _, days = full
    last_id: dict[tuple, str] = {}
    for d in days:
        today = {(k["fund"], k["isin"]): k["break_id"] for k in d.key}
        assert len(today) == len(d.key)
        for pos, bid in today.items():
            if pos in last_id:      # broke yesterday too: must be the same break
                assert last_id[pos] == bid
        last_id = today


# ------------------------------------------------------------- tickets
def test_each_actionable_break_has_exactly_one_linked_ticket(window):
    out, days = window
    t = snapshot_to_frame(load_snapshot(out / gd.SNAPSHOT_NAME))
    ids = t["labels"].str.extract(r"break-(BRK-[^;]+)")[0]
    assert ids.notna().all() and not ids.duplicated().any()
    first_actionable: dict[str, date] = {}
    for d in days:
        for k in d.key:
            if k["status"] == "Actionable":
                first_actionable.setdefault(k["break_id"], d.as_of)
    assert set(ids) == set(first_actionable)
    biz = dict(zip(ids, t["business_date"], strict=True))
    assert all(biz[i] == first_actionable[i] for i in first_actionable)


def test_ticket_data_has_only_the_planted_quality_problems(window):
    out, _ = window
    t = snapshot_to_frame(load_snapshot(out / gd.SNAPSHOT_NAME))
    checks = {i.check for i in check_jira(t)}
    assert checks <= {"required_field", "done_without_resolved_date"}
    assert "duplicate_key" not in checks


def test_the_story_ops_falls_behind_from_october(full):
    sim, days = full
    per_day = pd.Series({d.as_of: sum(k["status"] == "Actionable" for k in d.key)
                         for d in days})
    before = per_day[per_day.index < gd.STRESS_FROM].mean()
    after = per_day[per_day.index >= gd.STRESS_FROM].mean()
    assert after > 1.5 * before

    t = pd.DataFrame(sim.tickets(days))
    t["met"] = t["resolved"].notna() & (t["resolved"] <= t["due"])
    stressed = t["business_date"] >= gd.STRESS_FROM
    assert t.loc[stressed, "met"].mean() < t.loc[~stressed, "met"].mean()


# ------------------------------------------- the weekly data is untouched
def test_weekly_generator_still_reproduces_the_committed_files(tmp_path):
    weekly.generate(tmp_path / "raw", tmp_path / "expected")
    for d in weekly.DATES:
        new = pd.read_csv(tmp_path / "expected" / f"breaks_{d}.csv")
        old = pd.read_csv(ROOT / "data" / "expected" / f"breaks_{d}.csv")
        pd.testing.assert_frame_equal(new, old)
        name = f"fund_accounting_positions_{d:%Y%m%d}.xlsx"
        a = load_workbook(tmp_path / "raw" / d.isoformat() / name).active
        b = load_workbook(ROOT / "data" / "raw" / d.isoformat() / name).active
        assert [[c.value for c in r] for r in a.iter_rows()] == \
               [[c.value for c in r] for r in b.iter_rows()]
