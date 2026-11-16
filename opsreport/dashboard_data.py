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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

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
    bands = load_yaml("recon.yaml")["materiality"]
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
                           "overdue_tickets": None}
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
    return out


def kpis(h: History, f: Filters) -> dict:
    """Headline figures for the range, and the same for the previous range."""
    now, before = _point(h, f), _point(h, f.previous())
    now["sla_on_time"], now["resolved"] = _sla(h, f)
    before["sla_on_time"], before["resolved"] = _sla(h, f.previous())
    return {"now": now, "previous": before}


# ------------------------------------------------------------------- trends
def _run_days(h: History, f: Filters) -> pd.DataFrame:
    ok = h.runs[(h.runs["status"] == "OK") & h.runs["run_date"].between(f.start, f.end)]
    return ok.assign(period=period(ok["run_date"], f.grain).values) \
        .groupby("period").size().rename("run_days").reset_index()


def breaks_trend(h: History, f: Filters) -> pd.DataFrame:
    """Average open actionable breaks per business day, by period and type."""
    b = breaks_in(h, f)
    act = b[b["status"] == "Actionable"]
    days = _run_days(h, f)
    if act.empty or days.empty:
        return pd.DataFrame(columns=["period", "break_type", "avg_open", "run_days"])
    counts = (act.assign(period=period(act["run_date"], f.grain).values)
              .groupby(["period", "break_type"]).size().rename("break_days").reset_index())
    out = counts.merge(days, on="period")
    out["avg_open"] = out["break_days"] / out["run_days"]
    return out[["period", "break_type", "avg_open", "run_days"]]


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


# ---------------------------------------------------------- causes, repeats
def cause_of(row: pd.Series) -> str:
    t = row["break_type"]
    if t == "Quantity Break":
        return ("Failed settlement" if str(row["explanation"]).startswith("Settlement failed")
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
    """Per fund as of the range end: AUM, open breaks, exposure, aged."""
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
    out = a[["fund_code", "fund_name", "aum"]].merge(per, on="fund_code", how="left")
    fill = {"open_breaks": 0, "exposure_usd": 0.0, "aged_breaks": 0}
    out = out.fillna(fill).astype({"open_breaks": int, "aged_breaks": int})
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
