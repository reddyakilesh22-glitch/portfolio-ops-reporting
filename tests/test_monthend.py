"""Month-end sign-off (issue #31): each check's pass and fail path, the pack, the CLI."""

import shutil
from datetime import date
from io import BytesIO

import openpyxl
import pandas as pd
import pytest

from opsreport import dashboard_data as dd
from opsreport import history, monthend
from opsreport.__main__ import main
from opsreport.daily import business_days
from opsreport.report.monthend_report import pack_name, write_monthend

OCT = date(2026, 10, 1)
OCT_END = date(2026, 10, 30)


# ------------------------------------------------- a small hand-made month
def clean_month(path, month=OCT, funds=("AAA", "BBB")):
    """Every business day OK, no breaks, no tickets: everything should pass."""
    conn = history.connect(path)
    days = business_days(month, date(month.year, month.month, 30))
    for d in days:
        aum = pd.DataFrame({"fund_code": list(funds), "fund_name": [f"{f} Fund" for f in funds],
                            "aum": 100e6, "holdings_compared": 20,
                            "shares_outstanding": 4e6})
        history.save_day(conn, {"run_date": d, "status": "OK"}, aum=aum)
    conn.close()
    return days


def add_break(path, day, fund="AAA", **kw):
    conn = history.connect(path)
    row = {"break_id": f"BRK-{fund}-{kw.get('ticker', 'X')}", "fund_code": fund,
           "isin": "X", "ticker": "X", "break_type": "Price Break", "status": "Actionable",
           "materiality": "Low", "exposure_usd": 1000.0, "base_mv_diff": 1000.0,
           "days_open": 1, "first_seen": day, "explanation": "", **kw}
    old = history.read(conn, "breaks", day, day)
    breaks = pd.concat([old, pd.DataFrame([row])], ignore_index=True)
    aum = history.read(conn, "aum_daily", day, day)
    history.save_day(conn, {"run_date": day, "status": "OK"}, breaks, aum)
    conn.close()


def readiness(path, month=OCT, funds=()):
    m = monthend.month_end(dd.load(path), month, funds)
    return m, m.checks.set_index(["fund_code", "check"])["passed"]


def test_a_clean_month_is_ready_to_sign(tmp_path):
    path = tmp_path / "h.db"
    clean_month(path)
    m, passed = readiness(path)
    assert not m.in_progress and m.as_of == OCT_END
    assert passed.all()
    assert set(m.funds["status"]) == {monthend.READY}
    assert set(m.funds["checks_passed"]) == {"5 of 5"}
    assert (m.funds["reasons"] == "").all()


@pytest.mark.parametrize("change, check", [
    (dict(materiality="High"), "No high-materiality break"),
    (dict(base_mv_diff=50_000.0), "NAV impact under 1¢ a share"),         # 1.25¢ a share
    (dict(days_open=11), f"No break older than {monthend.MAX_AGE} days"),
])
def test_each_break_check_fails_only_its_fund(tmp_path, change, check):
    path = tmp_path / "h.db"
    clean_month(path)
    add_break(path, OCT_END, "AAA", **change)
    m, passed = readiness(path)
    assert not passed[("AAA", check)]
    assert passed.drop(("AAA", check)).all()           # nothing else fails
    status = m.funds.set_index("fund_code")["status"]
    assert status["AAA"] == monthend.ATTENTION and status["BBB"] == monthend.READY
    assert monthend.PROBLEM[check] in m.funds.set_index("fund_code")["reasons"]["AAA"]


def test_a_small_break_passes_every_check(tmp_path):
    path = tmp_path / "h.db"
    clean_month(path)
    add_break(path, OCT_END, "AAA")                   # $1,000: 0.025¢ a share, 1 day
    _, passed = readiness(path)
    assert passed.all()


def test_failed_settlement_does_not_fail_the_nav_check(tmp_path):
    path = tmp_path / "h.db"
    clean_month(path)
    add_break(path, OCT_END, "AAA", base_mv_diff=5e6, break_type="Quantity Break",
              explanation="Settlement failed: T1 was due Wed 28 Oct")
    _, passed = readiness(path)
    assert passed[("AAA", "NAV impact under 1¢ a share")]


def test_a_failed_or_missing_day_fails_every_fund(tmp_path):
    path = tmp_path / "h.db"
    days = clean_month(path)
    conn = history.connect(path)
    with conn:
        conn.execute("UPDATE runs SET status='FAILED' WHERE run_date = ?",
                     (days[4].isoformat(),))
        conn.execute("DELETE FROM runs WHERE run_date = ?", (days[5].isoformat(),))
    conn.close()
    m, passed = readiness(path)
    assert m.failed_days == [days[4]] and m.missing_days == [days[5]]
    assert not passed.xs("Every day reconciled", level="check").any()
    assert set(m.funds["status"]) == {monthend.ATTENTION}
    detail = m.checks[m.checks["check"] == "Every day reconciled"]["detail"].iloc[0]
    assert f"failed {days[4]:%d %b}" in detail and f"missing {days[5]:%d %b}" in detail


def test_an_unfinished_month_is_in_progress(tmp_path):
    path = tmp_path / "h.db"
    clean_month(path)
    conn = history.connect(path)
    with conn:
        conn.execute("DELETE FROM runs WHERE run_date > '2026-10-20'")
    conn.close()
    m, passed = readiness(path)
    assert m.in_progress and m.as_of == date(2026, 10, 20)
    assert set(m.funds["status"]) == {monthend.IN_PROGRESS}
    assert passed.all()                                # days not yet due aren't missing


def test_overdue_ticket_check(history_db):
    h = dd.load(history_db)
    m = monthend.month_end(h, OCT)
    from opsreport.transform import jira_metrics as jm
    p = jm.prepare(h.tickets, OCT_END)
    overdue = p[p["open_as_of"] & p["overdue"]].groupby("fund_code")["key"].apply(set)
    got = m.checks[m.checks["check"] == "No overdue ticket"].set_index("fund_code")
    for code, row in got.iterrows():
        assert row["passed"] == (code not in overdue)


# ------------------------------------------------- on generated history
@pytest.fixture(scope="module")
def h(history_db):
    return dd.load(history_db)


def test_months_and_default(h):
    assert monthend.months(h) == [date(2026, 11, 1), OCT, date(2026, 9, 1)]
    assert monthend.default_month(h) == OCT           # November isn't over


def test_status_follows_the_checks(h):
    for month in monthend.months(h):
        m = monthend.month_end(h, month)
        for r in m.funds.itertuples():
            fund = m.checks[m.checks["fund_code"] == r.fund_code]
            want = (monthend.IN_PROGRESS if m.in_progress else
                    monthend.READY if fund["passed"].all() else monthend.ATTENTION)
            assert r.status == want


def test_month_figures_match_the_dashboard(h):
    m = monthend.month_end(h, OCT)
    f = dd.Filters(OCT, OCT_END, "Monthly")
    rf = dd.roll_forward(h, f)
    assert m.funds["new_in_month"].sum() == rf["new"].sum()
    assert m.funds["open_breaks"].sum() == dd.kpis(h, f)["now"]["open_breaks"]
    assert list(m.roll_forward["run_date"]) == business_days(OCT, OCT_END)


def test_partial_month_at_start_of_history_is_not_signable(h):
    m = monthend.month_end(h, date(2026, 9, 1))
    assert len(m.missing_days) == 18 and set(m.funds["status"]) == {monthend.ATTENTION}


def test_fund_filter(h):
    m = monthend.month_end(h, OCT, ("GEF",))
    assert list(m.funds["fund_code"]) == ["GEF"]
    assert set(m.open_breaks["fund_code"]) <= {"GEF"}


def test_check_matrix(h):
    m = monthend.month_end(h, OCT)
    cm = monthend.check_matrix(m)
    assert list(cm.columns) == ["fund_code", *monthend.CHECKS]
    ticks = (cm[monthend.CHECKS] == "✓").sum().sum()
    assert ticks == m.checks["passed"].sum()


# ------------------------------------------------------------- pack, CLI
def test_pack_has_every_tab_and_a_row_per_fund(h, tmp_path):
    m = monthend.month_end(h, OCT)
    buf = write_monthend(m, BytesIO())
    wb = openpyxl.load_workbook(BytesIO(buf.getvalue()))
    assert wb.sheetnames == ["Sign-off", "Checks", "Open at Month-end", "Roll-forward",
                             "NAV Impact", "Run Log"]
    ws = wb["Sign-off"]
    head = [c.value for c in ws[7]]
    assert {"Fund Code", "Status", "Signed Off By", "Sign Off Date", "Comments"} <= set(head)
    codes = [ws.cell(8 + i, 1).value for i in range(len(m.funds))]
    assert codes == list(m.funds["fund_code"])
    assert "October 2026" in ws["A1"].value
    path = write_monthend(m, tmp_path / "x" / pack_name(m))
    assert path.exists() and path.name == "monthend_signoff_202610.xlsx"


def test_pack_for_a_month_in_progress(h):
    m = monthend.month_end(h, date(2026, 11, 1))
    wb = openpyxl.load_workbook(BytesIO(write_monthend(m, BytesIO()).getvalue()))
    assert "IN PROGRESS" in wb["Sign-off"]["A2"].value


def test_cli_writes_the_pack(history_db, tmp_path, capsys):
    db = tmp_path / "h.db"
    shutil.copy(history_db, db)
    assert main(["monthend", "--month", "2026-10", "--db", str(db),
                 "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "2026-10" / "monthend_signoff_202610.xlsx").exists()
    assert "Sign-off pack" in capsys.readouterr().out


@pytest.mark.parametrize("month, db", [("2025-01", "copy"), ("2026-10", "none")])
def test_cli_refuses_without_history(history_db, tmp_path, month, db):
    path = tmp_path / "h.db"
    if db == "copy":
        shutil.copy(history_db, path)
    assert main(["monthend", "--month", month, "--db", str(path),
                 "--out", str(tmp_path / "out")]) == 1
    assert db == "copy" or not path.exists()           # never creates an empty history
