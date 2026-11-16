"""Leadership dashboard (issue #23): the numbers, and that the app renders."""

from datetime import date

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from opsreport import daily, history
from opsreport import dashboard_data as dd
from opsreport.config import ROOT
from scripts import generate_daily_data as gd

WINDOW = (date(2026, 9, 28), date(2026, 11, 13))        # 7 weeks, Oct + Nov


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dash")
    gd.generate(tmp / "raw", *WINDOW)
    path = tmp / "h.db"
    daily.backfill(*WINDOW, raw_dir=tmp / "raw", db_path=path, out_dir=tmp / "out",
                   snapshot=tmp / "raw" / gd.SNAPSHOT_NAME)
    return path


@pytest.fixture(scope="module")
def h(db):
    return dd.load(db)


def full(grain="Weekly", **kw):
    return dd.Filters(*WINDOW, grain=grain, **kw)


# --------------------------------------------------------------- KPIs
def test_point_in_time_kpis_match_the_last_day(h):
    k = dd.kpis(h, full())["now"]
    last = h.breaks[h.breaks["run_date"] == WINDOW[1]]
    act = last[last["status"] == "Actionable"]
    assert k["as_of"] == WINDOW[1]
    assert k["open_breaks"] == len(act)
    assert k["exposure_usd"] == pytest.approx(act["exposure_usd"].sum())
    assert k["aged_breaks"] == int((act["days_open"] > 5).sum())
    assert k["expected"] == int((last["status"] == "Expected").sum())
    aum = h.aum[h.aum["run_date"] == WINDOW[1]]["aum"].sum()
    assert k["aum"] == pytest.approx(aum)


def test_sla_counts_only_tickets_resolved_in_the_range(h):
    f = full()
    rate, n = dd._sla(h, f)
    t = h.tickets
    rb = t["resolved_business_date"]
    done = t[rb.notna() & rb.between(*WINDOW)]
    assert n == len(done)
    met = (done["resolved_business_date"] <= done["due_date"]).mean()
    assert rate == pytest.approx(met)


def test_previous_period_is_the_same_length_just_before():
    f = dd.Filters(date(2026, 11, 1), date(2026, 11, 30))
    p = f.previous()
    assert p.end == date(2026, 10, 31) and (p.end - p.start) == (f.end - f.start)


def test_previous_period_without_data_has_no_figures(h):
    k = dd.kpis(h, full())
    assert k["previous"]["as_of"] is None and k["previous"]["open_breaks"] is None


@pytest.mark.parametrize("preset, start", [
    ("Last 30 days", date(2026, 10, 15)), ("Month to date", date(2026, 11, 1)),
    ("Last 6 months", date(2026, 5, 15))])
def test_preset_ranges(preset, start):
    assert dd.preset_range(preset, date(2026, 11, 13)) == (start, date(2026, 11, 13))


# ------------------------------------------------------------- filters
def test_filters_narrow_every_measure(h):
    allk = dd.kpis(h, full())["now"]
    gef = dd.kpis(h, full(funds=("GEF",)))["now"]
    assert gef["open_breaks"] <= allk["open_breaks"]
    assert gef["aum"] < allk["aum"]
    price = dd.breaks_in(h, full(types=("Price Break",)))
    assert set(price["break_type"]) == {"Price Break"}
    team = dd.tickets_in(h, full(teams=("NAV Oversight",)))
    assert set(team["ops_team"]) == {"NAV Oversight"}
    high = dd.breaks_in(h, full(materiality=("High",)))
    assert set(high["materiality"]) <= {"High"}


def test_team_filter_excludes_untriaged_timing_breaks(h):
    b = dd.breaks_in(h, full(teams=("Trade Support",)))
    assert (b["status"] == "Actionable").all()


def test_empty_selection_does_not_crash(h):
    f = full(funds=("NOPE",))
    assert dd.kpis(h, f)["now"]["open_breaks"] == 0
    assert dd.breaks_trend(h, f).empty and dd.recurring_causes(h, f).empty
    assert dd.sla_trend(h, f).empty
    assert dd.teams(h, f)[0].empty


# ------------------------------------------------- daily / weekly / monthly
def test_weekly_and_monthly_are_the_same_daily_rows_grouped(h):
    days = dd.breaks_trend(h, full("Daily"))
    weeks = dd.breaks_trend(h, full("Weekly"))
    months = dd.breaks_trend(h, full("Monthly"))
    total = (days["avg_open"]).sum()          # one run per day: avg == count
    assert (weeks["avg_open"] * weeks["run_days"]).sum() == pytest.approx(total)
    assert (months["avg_open"] * months["run_days"]).sum() == pytest.approx(total)
    assert set(pd.to_datetime(weeks["period"]).dt.day_of_week) == {4}     # Fridays
    assert set(pd.to_datetime(months["period"]).dt.day) == {1}


def test_sla_trend_adds_up_to_the_kpi(h):
    tr = dd.sla_trend(h, full("Monthly"))
    _, n = dd._sla(h, full())
    assert tr["resolved"].sum() == n


def test_aum_trend_takes_the_last_day_of_each_period(h):
    tr = dd.aum_trend(h, full("Monthly"))
    oct_ = tr[tr["period"] == pd.Timestamp("2026-10-01")]
    want = h.aum[h.aum["run_date"] == date(2026, 10, 30)]
    assert oct_["aum"].sum() == pytest.approx(want["aum"].sum())


def test_period_rejects_unknown_grain():
    with pytest.raises(ValueError):
        dd.period(pd.Series([date(2026, 1, 2)]), "Hourly")


# ------------------------------------------------------- causes, repeats
def test_recurring_causes_count_breaks_not_days(h):
    rc = dd.recurring_causes(h, full())
    b = dd.breaks_in(h, full())
    act = b[b["status"] == "Actionable"]
    assert rc["breaks"].sum() == act["break_id"].nunique()
    assert rc["break_days"].sum() == len(act)
    assert rc["share"].sum() == pytest.approx(1)
    assert rc.iloc[0]["cause"] == "Price differs: VendorB"   # the planted vendor problem


def test_cause_labels():
    row = {"break_type": "Quantity Break", "explanation": "Settlement failed: T1",
           "pricing_source": None}
    assert dd.cause_of(pd.Series(row)) == "Failed settlement"
    assert dd.cause_of(pd.Series({**row, "explanation": ""})) == \
        "Quantity difference, unexplained"
    assert dd.cause_of(pd.Series({**row, "break_type": "Price Break"})) == \
        "Price differs: unknown vendor"


def test_repeat_offenders_broke_more_than_once(h):
    r = dd.repeat_offenders(h, full())
    assert len(r) and (r["breaks"] >= 2).all()


# ------------------------------------------------------------ per day
def test_funds_table_adds_up_to_kpis(h):
    ft = dd.funds(h, full())
    k = dd.kpis(h, full())["now"]
    assert ft["open_breaks"].sum() == k["open_breaks"]
    assert ft["aum"].sum() == pytest.approx(k["aum"])


def test_run_health_reports_failed_and_missing_days(db, tmp_path):
    import shutil
    copy = tmp_path / "h.db"
    shutil.copy(db, copy)
    conn = history.connect(copy)
    with conn:
        conn.execute("DELETE FROM runs WHERE run_date = '2026-10-06'")
        conn.execute("UPDATE runs SET status = 'FAILED', message = 'x' "
                     "WHERE run_date = '2026-10-07'")
    conn.close()
    rh = dd.run_health(dd.load(copy), full())
    assert rh["missing_days"] == [date(2026, 10, 6)]
    assert list(rh["failed"]["run_date"]) == [date(2026, 10, 7)]
    assert rh["ok_days"] == rh["expected_days"] - 2


# ---------------------------------------------------- industry measures
def test_match_rate_counts_every_holding_and_every_difference(h):
    f = full("Daily")
    mt = dd.match_trend(h, f)
    a = h.aum[h.aum["run_date"].between(*WINDOW)]
    b = h.breaks[h.breaks["run_date"].between(*WINDOW)]
    assert mt["compared"].sum() == a["holdings_compared"].sum()
    assert mt["unmatched"].sum() == len(b)
    assert mt["actionable"].sum() == (b["status"] == "Actionable").sum()
    assert (mt["match_rate"] <= mt["match_rate_ex_timing"]).all()
    monthly = dd.match_trend(h, full("Monthly"))
    assert monthly["compared"].sum() == mt["compared"].sum()


def test_match_rate_ignores_type_filter_but_follows_fund(h):
    allm = dd.match_trend(h, full())
    assert dd.match_trend(h, full(types=("Price Break",)))["match_rate"].equals(
        allm["match_rate"])
    gef = dd.match_trend(h, full(funds=("GEF",)))
    assert gef["compared"].sum() < allm["compared"].sum()


def test_holdings_compared_counts_a_position_on_either_side_once():
    cust = pd.DataFrame({"fund_code": ["A", "A", "B"], "isin": ["X", "Y", "Z"]})
    acct = pd.DataFrame({"fund_code": ["A", "A"], "isin": ["X", "W"]})
    aum = pd.DataFrame({"fund_code": ["A", "B"]})
    out = daily.with_match_basis(aum, cust, acct)
    assert list(out["holdings_compared"]) == [3, 1]
    assert out["shares_outstanding"].isna().all()       # not funds from funds.yaml


@pytest.mark.parametrize("grain", ["Daily", "Weekly", "Monthly"])
def test_roll_forward_adds_up_and_chains(h, grain):
    rf = dd.roll_forward(h, full(grain))
    assert (rf["opening"] + rf["new"] - rf["cleared"] == rf["closing"]).all()
    assert (rf["opening"].iloc[1:].values == rf["closing"].iloc[:-1].values).all()
    assert rf["closing"].iloc[-1] == dd.kpis(h, full())["now"]["open_breaks"]


def test_roll_forward_respects_filters(h):
    rf = dd.roll_forward(h, full(funds=("GEF",), types=("Price Break",)))
    assert (rf["opening"] + rf["new"] - rf["cleared"] == rf["closing"]).all()
    assert rf["new"].sum() <= dd.roll_forward(h, full())["new"].sum()


def test_cleared_breaks_are_gone_on_the_clear_date(h):
    c = dd.cleared_breaks(h, full())
    assert len(c) and (c["days_to_clear"] >= 1).all()
    seen = set(zip(h.breaks["run_date"], h.breaks["break_id"], strict=True))
    act = h.breaks[h.breaks["status"] == "Actionable"]
    still = set(zip(act["run_date"], act["break_id"], strict=True))
    assert not any((d, i) in still for d, i in zip(c["cleared_on"], c["break_id"],
                                                    strict=True))
    assert seen                                      # sanity: history not empty
    k = dd.kpis(h, full())["now"]
    assert k["cleared"] == len(c)
    assert k["avg_days_to_clear"] == pytest.approx(c["days_to_clear"].mean())


def test_age_bands():
    assert dd.age_labels([1, 5, 10]) == ["0–1 days", "2–5 days", "6–10 days", "11+ days"]
    got = dd.age_bucket(pd.Series([0, 1, 2, 5, 6, 10, 11, 40]))
    assert list(got) == ["0–1 days"] * 2 + ["2–5 days"] * 2 + ["6–10 days"] * 2 + \
        ["11+ days"] * 2


def test_aging_now_adds_up_to_open_breaks(h):
    ag = dd.aging_now(h, full())
    k = dd.kpis(h, full())["now"]
    assert list(ag["age_bucket"]) == dd.AGE_BUCKETS
    assert ag["breaks"].sum() == k["open_breaks"]
    old = ag[ag["age_bucket"].isin(dd.AGE_BUCKETS[2:])]["breaks"].sum()
    assert old == k["aged_breaks"]                   # bands agree with "aged over 5"


def test_aging_trend_adds_up_to_breaks_trend(h):
    by_type = dd.breaks_trend(h, full())
    by_age = dd.breaks_trend(h, full(), by="age_bucket")
    assert by_age["avg_open"].sum() == pytest.approx(by_type["avg_open"].sum())


@pytest.mark.parametrize("per_share, pct, level", [
    (0.009, 0.004, 0), (0.01, 0.004, 1), (-0.02, -0.001, 1), (0.001, 0.005, 2),
    (0.5, -0.006, 2)])
def test_nav_levels(per_share, pct, level):
    got = dd.nav_level(pd.Series([per_share]), pd.Series([pct]))
    assert got.iloc[0] == dd.NAV_LEVELS[level]


def test_nav_impact_nets_unexplained_breaks_per_fund(h):
    day = WINDOW[1]
    nav = dd.nav_impact_daily(h, full())
    today = nav[nav["run_date"] == day].set_index("fund_code")
    b = h.breaks[(h.breaks["run_date"] == day) & (h.breaks["status"] == "Actionable")]
    counted = b[~b["explanation"].fillna("").str.startswith("Settlement failed")]
    want = counted.groupby("fund_code")["base_mv_diff"].sum()
    for code, row in today.iterrows():
        assert row["net_diff_usd"] == pytest.approx(want.get(code, 0.0))
        assert row["impact_per_share"] == pytest.approx(
            row["net_diff_usd"] / row["shares_outstanding"])
        assert row["impact_pct_of_nav"] == pytest.approx(row["net_diff_usd"] / row["aum"])


def test_failed_settlements_never_count_towards_nav_impact(h):
    fails = h.breaks[h.breaks["explanation"].fillna("").str.startswith("Settlement failed")]
    assert len(fails)                                 # the data has some
    nav = dd.nav_impact_daily(h, full())
    for (d, code), grp in fails.groupby(["run_date", "fund_code"]):
        other = h.breaks[(h.breaks["run_date"] == d) & (h.breaks["fund_code"] == code)
                         & (h.breaks["status"] == "Actionable")
                         & ~h.breaks.index.isin(grp.index)]
        row = nav[(nav["run_date"] == d) & (nav["fund_code"] == code)].iloc[0]
        assert row["net_diff_usd"] == pytest.approx(other["base_mv_diff"].sum())


def test_nav_impact_summary_and_kpi_agree(h):
    nav = dd.nav_impact(h, full())
    k = dd.kpis(h, full())["now"]
    assert len(nav) == k["funds"] == 5
    assert (nav["level"] != dd.NAV_LEVELS[0]).sum() == k["funds_over_nav"]
    assert (nav["days_over_threshold"] <= nav["run_days"]).all()


def test_funds_table_match_rate_matches_the_trend(h):
    ft = dd.funds(h, full())
    day = dd.match_trend(h, dd.Filters(WINDOW[1], WINDOW[1], "Daily"))
    a = h.aum[h.aum["run_date"] == WINDOW[1]].set_index("fund_code")
    weighted = (ft.set_index("fund_code")["match_rate"] * a["holdings_compared"]).sum() \
        / a["holdings_compared"].sum()
    assert weighted == pytest.approx(day["match_rate"].iloc[0])


# ----------------------------------------------------------- the app
def run_app(db, monkeypatch):
    """Every later .run() re-executes the script, so the variable must stay set
    for the whole test, or reruns silently read data/history.db instead."""
    monkeypatch.setenv("OPSREPORT_DB", str(db))
    return AppTest.from_file(str(ROOT / "dashboard" / "app.py"), default_timeout=60).run()


def test_app_renders_every_tab_for_every_grain_and_filter(db, monkeypatch):
    at = run_app(db, monkeypatch)
    assert "h.db" in at.caption[-1].value          # really reading the test history
    assert not at.exception
    assert [t.label for t in at.tabs] == ["Overview", "Breaks", "Tickets & SLA", "Funds",
                                          "Run health"]
    assert [m.label for m in at.metric[:10]] == [
        "Match rate", "Open breaks", "Exposure at risk", "Aged over 5 days",
        "Avg days to clear", "Funds over NAV line", "SLA on time", "Overdue tickets",
        "Expected (timing)", "AUM"]
    assert all(m.value != "n/a" for m in at.metric[:10])    # every figure computed
    for grain in ("Daily", "Monthly"):
        at.segmented_control[0].set_value(grain).run()
        assert not at.exception, grain
    for preset in ("Last 30 days", "Month to date", "Custom"):
        at.selectbox[0].set_value(preset).run()
        assert not at.exception, preset
    at.multiselect[0].set_value(["GEF"]).run()
    at.multiselect[2].set_value(["Price Break"]).run()
    assert not at.exception
    assert "h.db" in at.caption[-1].value


def test_app_explains_how_to_build_a_missing_history(tmp_path, monkeypatch):
    at = run_app(tmp_path / "none.db", monkeypatch)
    assert at.error and "backfill" in at.code[0].value
