from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

from opsreport.config import ROOT
from opsreport.extract.jira_client import load_snapshot, snapshot_to_frame
from opsreport.pipeline import run
from opsreport.transform import jira_metrics as jm
from opsreport.validate.checks import check_jira

AS_OF = date(2026, 11, 13)
PRIOR = date(2026, 11, 6)


def ticket(key, biz, due, resolved=None, done=None, team="Reconciliation", amt=1000.0,
           fund="Global Equity Fund"):
    done = resolved is not None if done is None else done
    return {"key": key, "summary": key, "status": "Done" if done else "In Progress",
            "status_category": "done" if done else "indeterminate", "priority": "High",
            "fund": fund, "ops_team": team, "break_type": "Cash Break",
            "break_amount_usd": amt, "business_date": biz, "due_date": due,
            "resolved_business_date": resolved}


@pytest.fixture
def tickets():
    d = date
    return pd.DataFrame([
        ticket("A-1", d(2026, 11, 2), d(2026, 11, 4), d(2026, 11, 3)),   # met
        ticket("A-2", d(2026, 11, 2), d(2026, 11, 4), d(2026, 11, 10)),  # breached
        ticket("A-3", d(2026, 11, 9), d(2026, 11, 11)),                  # open, overdue
        ticket("A-4", d(2026, 11, 12), d(2026, 11, 16)),                 # open, in SLA
        ticket("A-5", d(2026, 11, 16), d(2026, 11, 18)),                 # future: excluded
        ticket("A-6", d(2026, 11, 3), d(2026, 11, 5), None, done=True),  # undatable
    ])


def test_point_in_time_open_and_overdue(tickets):
    t = jm.prepare(tickets, AS_OF)
    assert set(t["key"]) == {"A-1", "A-2", "A-3", "A-4"}
    k = jm.kpis(t, AS_OF)
    assert k["open_tickets"] == 2 and k["overdue_tickets"] == 1
    assert k["resolved_this_week"] == 1 and k["sla_met_this_week"] == 0.0


def test_rerun_of_prior_week_ignores_later_resolutions(tickets):
    t = jm.prepare(tickets, PRIOR)
    k = jm.kpis(t, PRIOR)
    # On Nov 6, A-2 (resolved Nov 10) was still open and past due.
    assert k["open_tickets"] == 1 and k["overdue_tickets"] == 1
    assert k["sla_met_this_week"] == 1.0


def test_age_in_business_days_skips_weekend(tickets):
    t = jm.prepare(tickets, AS_OF).set_index("key")
    assert t.loc["A-3", "age_bdays"] == 4   # Mon 9 -> Fri 13


def test_trend_open_count_matches_kpis(tickets):
    t = jm.prepare(tickets, AS_OF)
    trend = jm.weekly_trend(t, AS_OF, weeks=2)
    assert trend.iloc[-1]["open_at_week_end"] == jm.kpis(t, AS_OF)["open_tickets"]


def test_jira_checks_find_planted_issues():
    df = snapshot_to_frame(load_snapshot(ROOT / "data/snapshots/jira_2026-11-13.json"))
    checks = pd.Series([i.check for i in check_jira(df)]).value_counts().to_dict()
    assert checks == {"required_field": 4, "done_without_resolved_date": 2}


def test_pipeline_end_to_end(tmp_path):
    res = run(AS_OF, PRIOR, out_dir=tmp_path)
    assert res.report_path.exists()
    wb = load_workbook(res.report_path)
    assert wb.sheetnames == ["Summary", "Breaks", "AUM", "Ops SLA", "Overdue Tickets",
                             "Data Quality", "Run Info"]
    assert len(res.breaks) == 11
    assert (res.breaks["materiality"] == "High").sum() == 4   # incl. the ASML split
    total = res.data.aum.set_index("fund_code").loc["TOTAL"]
    funds = res.data.aum[res.data.aum["fund_code"] != "TOTAL"]
    assert total["aum"] == pytest.approx(funds["aum"].sum())
    k = res.data.jira_kpis
    assert (k["open_tickets"], k["overdue_tickets"]) == (19, 12)
    assert (tmp_path / "2026-11-13" / "breaks_20261113.csv").exists()


# ---- INC-004: holidays in ticket aging (issue #7) ---------------------------
from opsreport import calendar as cal  # noqa: E402
from opsreport.config import ConfigError  # noqa: E402


def test_thanksgiving_not_counted_in_age():
    biz = pd.Series([date(2026, 11, 25)])
    assert cal.busday_count(biz, date(2026, 11, 27)).iloc[0] == 1   # Wed -> Fri, Thu closed
    assert cal.busday_count(biz, date(2026, 11, 30)).iloc[0] == 2


def test_day_after_thanksgiving_is_a_business_day():
    assert not cal.is_business_day(date(2026, 11, 26))
    assert cal.is_business_day(date(2026, 11, 27))   # early close, still open


def test_calendar_coverage_is_enforced():
    with pytest.raises(ConfigError, match="no entries for \\[2028\\]"):
        cal.busday_count(pd.Series([date(2027, 12, 20)]), date(2028, 1, 5))


def test_configured_holidays_are_weekdays():
    days = cal.holidays().astype("datetime64[D]").astype(object)
    assert all(d.weekday() < 5 for d in days)


def test_aging_bucket_across_thanksgiving():
    t = jm.prepare(pd.DataFrame([ticket("H-1", date(2026, 11, 12), date(2026, 11, 16))]),
                   date(2026, 11, 27))
    # Nov 12 -> Nov 27 is 11 weekdays but 10 business days (Nov 26 closed).
    assert t.iloc[0]["age_bdays"] == 10 and t.iloc[0]["age_bucket"] == "6-10d"


def test_business_day_as_of_not_flagged(tmp_path):
    res = run(date(2026, 11, 13), out_dir=tmp_path)
    assert "as_of_not_business_day" not in set(res.issues["check"])
