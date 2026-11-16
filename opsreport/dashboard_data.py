"""Every number on the leadership dashboard, computed from the history store.

The Streamlit app only draws; all figures come from these functions so they
can be tested, and so daily, weekly and monthly views are the same daily rows
grouped differently.

Definitions (also shown in the dashboard's "How to read this"):
- "As of" = the last successful daily run inside the selected range.
- Open breaks, exposure, aged breaks and AUM are *point-in-time*: as of that run.
- Trend of open breaks = average open per business day in each period.
- SLA on-time % = tickets resolved in the period that met their due date.
- AUM trend = value on the last run of each period.
- Match rate = holdings where custodian and accounting agree / holdings compared.
- Roll-forward: opening + new - cleared = closing, per period (exact).
- Days to clear = business days from first seen to the run where it was gone.
- Potential NAV impact = net unexplained difference per fund, as cents per share
  and % of NAV, against the thresholds in config/recon.yaml.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from opsreport.calendar import holidays
from opsreport.config import load_yaml
from opsreport.daily import business_days
from opsreport.history import DB_PATH as DB_PATH  # re-exported for the app
from opsreport.history import connect, read
from opsreport.transform import jira_metrics as jm
from opsreport.transform.aum import fund_names

GRAINS = ("Daily", "Weekly", "Monthly")
BREAK_TYPES = ["Quantity Break", "Price Break", "Cash Break", "Missing Trade",
               "Corporate Action", "FX Rate", "Static Data"]
MATERIALITY = ["High", "Medium", "Low"]
AGED_DAYS = 5
_RECON = load_yaml("recon.yaml")
NAV_PER_SHARE_LIMIT = _RECON["nav_impact"]["per_share_usd"]
NAV_PCT_LIMIT = _RECON["nav_impact"]["pct_of_nav"]
SETTLEMENT_FAILED = "Settlement failed"
NAV_LEVELS = ["Within threshold", "NAV error (1¢+ a share)", "Reprocess (0.5%+ of NAV)"]


def age_labels(bands: list[int]) -> list[str]:
    """[1, 5, 10] -> 0–1 days, 2–5 days, 6–10 days, 11+ days"""
    out, lo = [], 0
    for hi in bands:
        out.append(f"{lo}–{hi} days")
        lo = hi + 1
    return [*out, f"{lo}+ days"]


AGE_BANDS = _RECON["aging_bands"]
AGE_BUCKETS = age_labels(AGE_BANDS)


def age_bucket(days: pd.Series) -> pd.Series:
    return pd.cut(days, [-1, *AGE_BANDS, float("inf")], labels=AGE_BUCKETS).astype(str)


@dataclass
class History:
    runs: pd.DataFrame
    breaks: pd.DataFrame
    aum: pd.DataFrame
    dq: pd.DataFrame
    tickets: pd.DataFrame

    @property
    def first(self) -> date | None:
        ok = self.runs[self.runs["status"] == "OK"]["run_date"]
        return min(ok) if len(ok) else None

    @property
    def last(self) -> date | None:
        ok = self.runs[self.runs["status"] == "OK"]["run_date"]
        return max(ok) if len(ok) else None


def load(path: Path = DB_PATH) -> History:
    conn = connect(path)
    try:
        h = History(*(read(conn, t) for t in
                      ("runs", "breaks", "aum_daily", "data_quality", "tickets")))
    finally:
        conn.close()
    bands = _RECON["materiality"]
    h.breaks["age_bucket"] = age_bucket(h.breaks["days_open"])
    code = {v: k for k, v in fund_names().items()}
    h.tickets["fund_code"] = h.tickets["fund"].map(code)
    h.tickets["materiality"] = pd.cut(
        h.tickets["break_amount_usd"], [-1, bands["medium"], bands["high"], float("inf")],
        labels=["Low", "Medium", "High"]).astype(str)
    return h


@dataclass(frozen=True)
class Filters:
    start: date
    end: date
    grain: str = "Weekly"
    funds: tuple[str, ...] = ()          # fund codes; empty = all
    teams: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    materiality: tuple[str, ...] = ()

    def previous(self) -> Filters:
        """The same-length window immediately before this one."""
        length = self.end - self.start
        end = self.start - timedelta(days=1)
        return Filters(end - length, end, self.grain, self.funds, self.teams, self.types,
                       self.materiality)


def preset_range(preset: str, last: date) -> tuple[date, date]:
    days = {"Last 30 days": 30, "Last 3 months": 91, "Last 6 months": 183}
    if preset == "Month to date":
        return last.replace(day=1), last
    return last - timedelta(days=days[preset] - 1), last


# ---------------------------------------------------------------- filtering
def _in(series: pd.Series, allowed: tuple[str, ...]) -> pd.Series:
    return series.isin(allowed) if allowed else pd.Series(True, index=series.index)


def breaks_in(h: History, f: Filters) -> pd.DataFrame:
    b = h.breaks
    keep = (b["run_date"].between(f.start, f.end) & _in(b["fund_code"], f.funds)
            & _in(b["ops_team"], f.teams) & _in(b["break_type"], f.types)
            & _in(b["materiality"], f.materiality))
    return b[keep]


def tickets_in(h: History, f: Filters) -> pd.DataFrame:
    t = h.tickets
    keep = (_in(t["fund_code"], f.funds) & _in(t["ops_team"], f.teams)
            & _in(t["break_type"], f.types) & _in(t["materiality"], f.materiality))
    return t[keep]


def as_of(h: History, f: Filters) -> date | None:
    ok = h.runs[(h.runs["status"] == "OK") & h.runs["run_date"].between(f.start, f.end)]
    return max(ok["run_date"]) if len(ok) else None


def period(dates: pd.Series, grain: str) -> pd.Series:
    """Period each date belongs to: the day, the week ending Friday, or the month."""
    ts = pd.to_datetime(pd.Series(dates))
    if grain == "Daily":
        return ts.dt.normalize()
    if grain == "Weekly":
        return ts.dt.to_period("W-FRI").dt.end_time.dt.normalize()
    if grain == "Monthly":
        return ts.dt.to_period("M").dt.start_time
    raise ValueError(f"grain must be one of {GRAINS}")


# --------------------------------------------------------------------- KPIs
def _sla(h: History, f: Filters) -> tuple[float | None, int]:
    t = tickets_in(h, f)
    if t.empty:
        return None, 0
    p = jm.prepare(t, f.end)
    rb = p["resolved_business_date"]
    done = p[rb.notna() & rb.between(f.start, f.end)]
    met = done["sla_met"].dropna()
    return (float(met.mean()) if len(met) else None), len(done)


def _point(h: History, f: Filters) -> dict:
    day = as_of(h, f)
    out: dict[str, Any] = {"as_of": day, "open_breaks": None, "exposure_usd": None,
                           "aged_breaks": None, "expected": None, "aum": None,
                           "overdue_tickets": None, "match_rate": None,
                           "funds_over_nav": None, "funds": None}
    if day is None:
        return out
    b = breaks_in(h, Filters(day, day, f.grain, f.funds, f.teams, f.types, f.materiality))
    act = b[b["status"] == "Actionable"]
    a = h.aum[(h.aum["run_date"] == day) & _in(h.aum["fund_code"], f.funds)]
    t = tickets_in(h, f)
    p = jm.prepare(t, day) if len(t) else t
    out.update(open_breaks=len(act), exposure_usd=float(act["exposure_usd"].sum()),
               aged_breaks=int((act["days_open"] > AGED_DAYS).sum()),
               expected=int((b["status"] == "Expected").sum()),
               aum=float(a["aum"].sum()) if len(a) else None,
               overdue_tickets=int((p["open_as_of"] & p["overdue"]).sum()) if len(p) else 0)
    one_day = replace(f, start=day, end=day, grain="Daily")
    mr = match_trend(h, one_day)
    out["match_rate"] = float(mr["match_rate"].iloc[0]) if len(mr) else None
    nav = nav_impact_daily(h, one_day)
    out["funds"] = len(nav)
    out["funds_over_nav"] = int((nav["level"] != NAV_LEVELS[0]).sum())
    return out


def kpis(h: History, f: Filters) -> dict:
    """Headline figures for the range, and the same for the previous range."""
    now, before = _point(h, f), _point(h, f.previous())
    for d, rng in ((now, f), (before, f.previous())):
        d["sla_on_time"], d["resolved"] = _sla(h, rng)
        c = cleared_breaks(h, rng)
        d["cleared"] = len(c)
        d["avg_days_to_clear"] = float(c["days_to_clear"].mean()) if len(c) else None
    return {"now": now, "previous": before}


# ------------------------------------------------------------------- trends
def _run_days(h: History, f: Filters) -> pd.DataFrame:
    ok = h.runs[(h.runs["status"] == "OK") & h.runs["run_date"].between(f.start, f.end)]
    return ok.assign(period=period(ok["run_date"], f.grain).values) \
        .groupby("period").size().rename("run_days").reset_index()


def breaks_trend(h: History, f: Filters, by: str = "break_type") -> pd.DataFrame:
    """Average open actionable breaks per business day, by period and `by`
    (break_type, or age_bucket for the aging profile)."""
    b = breaks_in(h, f)
    act = b[b["status"] == "Actionable"]
    days = _run_days(h, f)
    if act.empty or days.empty:
        return pd.DataFrame(columns=["period", by, "avg_open", "run_days"])
    counts = (act.assign(period=period(act["run_date"], f.grain).values)
              .groupby(["period", by]).size().rename("break_days").reset_index())
    out = counts.merge(days, on="period")
    out["avg_open"] = out["break_days"] / out["run_days"]
    return out[["period", by, "avg_open", "run_days"]]


def exposure_trend(h: History, f: Filters) -> pd.DataFrame:
    """Average actionable exposure per business day, by period."""
    b = breaks_in(h, f)
    act = b[b["status"] == "Actionable"]
    days = _run_days(h, f)
    if days.empty:
        return pd.DataFrame(columns=["period", "avg_exposure_usd"])
    total = (act.assign(period=period(act["run_date"], f.grain).values)
             .groupby("period")["exposure_usd"].sum().rename("exposure").reset_index())
    out = days.merge(total, on="period", how="left").fillna({"exposure": 0.0})
    out["avg_exposure_usd"] = out["exposure"] / out["run_days"]
    return out[["period", "avg_exposure_usd"]]


def sla_trend(h: History, f: Filters) -> pd.DataFrame:
    """Tickets resolved per period and the share that met their due date."""
    t = tickets_in(h, f)
    cols = ["period", "resolved", "on_time", "on_time_pct"]
    if t.empty:
        return pd.DataFrame(columns=cols)
    p = jm.prepare(t, f.end)
    rb = p["resolved_business_date"]
    done = p[rb.notna() & rb.between(f.start, f.end)]
    if done.empty:
        return pd.DataFrame(columns=cols)
    done = done.assign(period=period(done["resolved_business_date"], f.grain).values)
    out = done.groupby("period").agg(resolved=("key", "count"),
                                     on_time=("sla_met", lambda s: int(s.fillna(0).sum())),
                                     measured=("sla_met", lambda s: int(s.notna().sum())))
    out["on_time_pct"] = (out["on_time"] / out["measured"]).where(out["measured"] > 0)
    return out.reset_index()[cols]


# ------------------------------------------------------ industry measures
def _actionable_all_dates(h: History, f: Filters) -> pd.DataFrame:
    """Filtered actionable rows over the whole history. The date range is applied
    afterwards, so a period's opening balance can come from the run before it."""
    b = breaks_in(h, replace(f, start=date.min, end=date.max))
    return b[b["status"] == "Actionable"]


def break_flows(h: History, f: Filters) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(per successful run day: opening, new, cleared, closing; one row per break
    cleared, with the run it was gone on and its business days to clear).

    New = actionable today, not on the previous successful run (including a timing
    break whose settlement failed). Cleared = actionable on the previous run, not
    today. A failed day in between is skipped, so the chain is never broken and
    opening + new - cleared = closing holds exactly.
    """
    runs = sorted(h.runs.loc[h.runs["status"] == "OK", "run_date"])
    act = _actionable_all_dates(h, f)[["run_date", "break_id", "fund_code", "ticker",
                                       "break_type", "first_seen", "exposure_usd"]]
    nxt = dict(zip(runs[:-1], runs[1:], strict=True))
    prv = {v: k for k, v in nxt.items()}
    present = set(zip(act["run_date"], act["break_id"], strict=True))
    is_new = np.array([(prv.get(d), i) not in present
                       for d, i in zip(act["run_date"], act["break_id"], strict=True)],
                      dtype=bool)
    gone_on = act["run_date"].map(nxt)
    gone = np.array([n is not None and not pd.isna(n) and (n, i) not in present
                     for n, i in zip(gone_on, act["break_id"], strict=True)], dtype=bool)
    cleared = act[gone].assign(cleared_on=gone_on[gone])
    cleared["days_to_clear"] = np.busday_count(
        np.asarray(pd.to_datetime(cleared["first_seen"]), dtype="datetime64[D]"),
        np.asarray(pd.to_datetime(cleared["cleared_on"]), dtype="datetime64[D]"),
        holidays=holidays()) if len(cleared) else []

    def per_day(s: pd.Series) -> pd.Series:
        return s.groupby(s).size().reindex(runs, fill_value=0)

    closing = per_day(act["run_date"])
    daily = pd.DataFrame({"run_date": runs,
                          "opening": closing.shift(1, fill_value=0).values,
                          "new": per_day(act["run_date"][is_new]).values,
                          "cleared": per_day(cleared["cleared_on"]).values,
                          "closing": closing.values})
    return daily, cleared.drop(columns="run_date")


def roll_forward(h: History, f: Filters) -> pd.DataFrame:
    """Opening, new, cleared and closing actionable breaks per period."""
    daily, _ = break_flows(h, f)
    d = daily[daily["run_date"].between(f.start, f.end)]
    cols = ["period", "opening", "new", "cleared", "closing"]
    if d.empty:
        return pd.DataFrame(columns=cols)
    d = d.assign(period=period(d["run_date"], f.grain).values)
    return d.groupby("period").agg(opening=("opening", "first"), new=("new", "sum"),
                                   cleared=("cleared", "sum"),
                                   closing=("closing", "last")).reset_index()[cols]


def cleared_breaks(h: History, f: Filters) -> pd.DataFrame:
    """Breaks that cleared inside the range, with business days to clear."""
    _, c = break_flows(h, f)
    return c[c["cleared_on"].between(f.start, f.end)].sort_values("cleared_on")


def match_trend(h: History, f: Filters) -> pd.DataFrame:
    """Match rate per period: holdings where both records agree / holdings compared.

    `match_rate` counts every difference, timing included; `match_rate_ex_timing`
    leaves Expected (timing) breaks out. Only the fund filter applies: the
    measure is about all holdings, not one break type or team. Days stored
    before holdings were counted are left out.
    """
    cols = ["period", "compared", "unmatched", "actionable", "match_rate",
            "match_rate_ex_timing"]
    a = h.aum[h.aum["run_date"].between(f.start, f.end) & _in(h.aum["fund_code"], f.funds)
              & h.aum["holdings_compared"].notna()]
    if a.empty:
        return pd.DataFrame(columns=cols)
    b = h.breaks[h.breaks["run_date"].between(f.start, f.end)
                 & _in(h.breaks["fund_code"], f.funds)]
    b = b[b["run_date"].isin(set(a["run_date"]))]
    per = pd.DataFrame({
        "compared": a.groupby("run_date")["holdings_compared"].sum(),
        "unmatched": b.groupby("run_date").size(),
        "actionable": b[b["status"] == "Actionable"].groupby("run_date").size(),
    }).fillna(0).reset_index()
    per["period"] = period(per["run_date"], f.grain).values
    out = per.groupby("period")[["compared", "unmatched", "actionable"]].sum().reset_index()
    out = out.astype({"compared": int, "unmatched": int, "actionable": int})
    out["match_rate"] = 1 - out["unmatched"] / out["compared"]
    out["match_rate_ex_timing"] = 1 - out["actionable"] / out["compared"]
    return out[cols]


def nav_level(per_share: pd.Series, pct: pd.Series) -> pd.Series:
    level = np.where(pct.abs() >= NAV_PCT_LIMIT, NAV_LEVELS[2],
                     np.where(per_share.abs() >= NAV_PER_SHARE_LIMIT, NAV_LEVELS[1],
                              NAV_LEVELS[0]))
    return pd.Series(level, index=per_share.index)


def nav_impact_daily(h: History, f: Filters) -> pd.DataFrame:
    """Potential NAV impact per fund per run day.

    The net (signed) difference on open actionable breaks: what the fund's value
    would move by if the books turned out to be the wrong side. Left out:
    timing breaks and failed settlements, where trade-date accounting is right
    and the custodian is waiting for delivery (a settlement risk, not a NAV
    error); and the break type, team and materiality filters, because the check
    covers the whole fund.
    """
    a = h.aum[h.aum["run_date"].between(f.start, f.end) & _in(h.aum["fund_code"], f.funds)]
    a = a[["run_date", "fund_code", "fund_name", "aum", "shares_outstanding"]]
    b = h.breaks[h.breaks["run_date"].between(f.start, f.end)
                 & _in(h.breaks["fund_code"], f.funds) & (h.breaks["status"] == "Actionable")]
    b = b[~b["explanation"].fillna("").str.startswith(SETTLEMENT_FAILED)]
    net = b.groupby(["run_date", "fund_code"])["base_mv_diff"].sum().rename("net_diff_usd")
    out = a.merge(net.reset_index(), on=["run_date", "fund_code"], how="left")
    out["net_diff_usd"] = out["net_diff_usd"].fillna(0.0)
    shares = out["shares_outstanding"].astype(float)
    out["nav_per_share"] = out["aum"] / shares
    out["impact_per_share"] = out["net_diff_usd"] / shares
    out["impact_pct_of_nav"] = out["net_diff_usd"] / out["aum"]
    out["level"] = nav_level(out["impact_per_share"].fillna(0), out["impact_pct_of_nav"])
    return out


def nav_impact(h: History, f: Filters) -> pd.DataFrame:
    """Per fund on the as-of day, with how many days in the range were over a line."""
    day = as_of(h, f)
    daily = nav_impact_daily(h, f)
    if day is None or daily.empty:
        return pd.DataFrame()
    over = (daily[daily["level"] != NAV_LEVELS[0]].groupby("fund_code").size()
            .rename("days_over_threshold"))
    out = daily[daily["run_date"] == day].merge(over.reset_index(), on="fund_code",
                                                how="left")
    out["days_over_threshold"] = out["days_over_threshold"].fillna(0).astype(int)
    out["run_days"] = int(daily["run_date"].nunique())
    return out.drop(columns=["run_date"]).sort_values(
        "impact_pct_of_nav", key=lambda s: s.abs(), ascending=False)


def aging_now(h: History, f: Filters) -> pd.DataFrame:
    """Open actionable breaks by age band on the as-of day."""
    b = open_breaks(h, f)
    act = b[b["status"] == "Actionable"]
    out = (act.groupby("age_bucket").agg(breaks=("break_id", "count"),
                                         exposure_usd=("exposure_usd", "sum"))
           .reindex(AGE_BUCKETS).fillna(0).reset_index())
    return out.astype({"breaks": int})


# ---------------------------------------------------------- causes, repeats
def cause_of(row: pd.Series) -> str:
    t = row["break_type"]
    if t == "Quantity Break":
        return ("Failed settlement" if str(row["explanation"]).startswith(SETTLEMENT_FAILED)
                else "Quantity difference, unexplained")
    if t == "Price Break":
        vendor = row["pricing_source"]
        return f"Price differs: {'unknown vendor' if pd.isna(vendor) else vendor}"
    return {"Cash Break": "Cash not booked", "FX Rate": "Stale FX rate",
            "Corporate Action": "Corporate action not processed",
            "Static Data": "Security master wrong",
            "Missing Trade": "Position on one side only"}.get(t, t)


def recurring_causes(h: History, f: Filters) -> pd.DataFrame:
    """Root causes ranked by how many separate breaks they produced.

    Answers "what should we fix upstream?", not "what broke today?".
    """
    b = breaks_in(h, f)
    act = b[b["status"] == "Actionable"]
    cols = ["cause", "breaks", "break_days", "share", "max_exposure_usd"]
    if act.empty:
        return pd.DataFrame(columns=cols)
    act = act.assign(cause=act.apply(cause_of, axis=1))
    per = act.groupby(["cause", "break_id"]).agg(
        days=("run_date", "count"), exposure=("exposure_usd", "max")).reset_index()
    out = per.groupby("cause").agg(breaks=("break_id", "count"), break_days=("days", "sum"),
                                   max_exposure_usd=("exposure", "sum")).reset_index()
    out["share"] = out["breaks"] / out["breaks"].sum()
    return out.sort_values(["breaks", "break_days"], ascending=False)[cols]


def repeat_offenders(h: History, f: Filters, at_least: int = 2) -> pd.DataFrame:
    """Securities that broke more than once in the range."""
    b = breaks_in(h, f)
    act = b[b["status"] == "Actionable"]
    out = (act.groupby(["fund_code", "ticker"])
           .agg(breaks=("break_id", "nunique"), break_days=("run_date", "count"),
                types=("break_type", lambda s: ", ".join(sorted(set(s)))),
                pricing_source=("pricing_source", "first"))
           .reset_index())
    return out[out["breaks"] >= at_least].sort_values(["breaks", "break_days"],
                                                      ascending=False)


# ------------------------------------------------------------- per-day lists
def open_breaks(h: History, f: Filters) -> pd.DataFrame:
    day = as_of(h, f)
    if day is None:
        return h.breaks.iloc[0:0]
    b = breaks_in(h, Filters(day, day, f.grain, f.funds, f.teams, f.types, f.materiality))
    return b.sort_values(["status", "exposure_usd"], ascending=[True, False])


def teams(h: History, f: Filters) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(by team, aging buckets, overdue list) for tickets, as of the range end."""
    t = tickets_in(h, f)
    if t.empty:
        empty = pd.DataFrame()
        return empty, empty, empty
    p = jm.prepare(t, f.end)
    return jm.by_team(p, f.end), jm.aging(p), jm.open_overdue_list(p)


def funds(h: History, f: Filters) -> pd.DataFrame:
    """Per fund as of the range end: AUM, match rate, open breaks, exposure, aged."""
    day = as_of(h, f)
    if day is None:
        return pd.DataFrame()
    a = h.aum[(h.aum["run_date"] == day) & _in(h.aum["fund_code"], f.funds)]
    b = open_breaks(h, f)
    act = b[b["status"] == "Actionable"]
    per = act.groupby("fund_code").agg(open_breaks=("break_id", "count"),
                                       exposure_usd=("exposure_usd", "sum"),
                                       aged_breaks=("days_open",
                                                    lambda s: int((s > AGED_DAYS).sum())))
    out = a[["fund_code", "fund_name", "aum", "holdings_compared"]].merge(
        per, on="fund_code", how="left")
    fill = {"open_breaks": 0, "exposure_usd": 0.0, "aged_breaks": 0}
    out = out.fillna(fill).astype({"open_breaks": int, "aged_breaks": int})
    day_rows = h.breaks[h.breaks["run_date"] == day]     # all holdings: see match_trend
    unmatched = (day_rows.groupby("fund_code").size().reindex(out["fund_code"])
                 .fillna(0).values)
    out["match_rate"] = 1 - unmatched / out["holdings_compared"]
    out = out.drop(columns="holdings_compared")
    out["exposure_pct_of_aum"] = out["exposure_usd"] / out["aum"]
    return out.sort_values("exposure_usd", ascending=False)


def aum_trend(h: History, f: Filters) -> pd.DataFrame:
    """AUM per fund on the last run of each period."""
    a = h.aum[h.aum["run_date"].between(f.start, f.end) & _in(h.aum["fund_code"], f.funds)]
    if a.empty:
        return pd.DataFrame(columns=["period", "fund_name", "aum"])
    a = a.assign(period=period(a["run_date"], f.grain).values).sort_values("run_date")
    return a.groupby(["period", "fund_name"], as_index=False).last()[
        ["period", "fund_name", "aum"]]


# --------------------------------------------------------------- run health
def run_health(h: History, f: Filters) -> dict:
    """Did every business day run, and were the inputs clean?"""
    expected = business_days(f.start, f.end)
    runs = h.runs[h.runs["run_date"].between(f.start, f.end)]
    ran = set(runs["run_date"])
    missing = [d for d in expected if d not in ran and d <= (h.last or f.end)]
    dq = h.dq[h.dq["run_date"].between(f.start, f.end)]
    rules = (dq.groupby(["severity", "rule"]).agg(days=("run_date", "nunique"),
                                                  findings=("rule", "size"))
             .reset_index().sort_values(["severity", "days"], ascending=[True, False]))
    return {
        "expected_days": len(expected), "ok_days": int((runs["status"] == "OK").sum()),
        "failed": runs[runs["status"] == "FAILED"][["run_date", "message"]],
        "missing_days": missing, "rules": rules,
        "runs": runs[["run_date", "status", "actionable", "expected", "actionable_usd",
                      "errors", "warnings", "finished_at", "message"]]
        .sort_values("run_date", ascending=False),
    }
