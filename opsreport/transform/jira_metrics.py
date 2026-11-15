"""Ops-ticket SLA metrics from the Jira extract, computed point-in-time.

"Open as of D" means raised on/before D and not resolved by D, using the
business-date fields. That keeps a re-run of last week's report identical to
what was reported last week, regardless of what happened to tickets since.

Tickets marked Done with no Resolved Business Date can't be placed in time;
they're excluded here and listed in the Data Quality tab. Tickets with no due
date are counted as open/resolved but left out of SLA and overdue figures.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from opsreport.calendar import busday_count, uncovered_years

AGE_BUCKETS = [(-1, 1, "0-1d"), (1, 5, "2-5d"), (5, 10, "6-10d"), (10, 10_000, "11d+")]


def prepare(tickets: pd.DataFrame, as_of: date) -> pd.DataFrame:
    df = tickets.copy()
    for col in ("fund", "ops_team"):          # keep them visible, don't drop them
        df[col] = df[col].fillna("(blank)")
    undatable = df["status_category"].eq("done") & df["resolved_business_date"].isna()
    df = df[~undatable & df["business_date"].notna()]
    df = df[df["business_date"] <= as_of].copy()

    resolved = df["resolved_business_date"]
    df["open_as_of"] = resolved.isna() | (resolved > as_of)
    df["age_bdays"] = busday_count(df["business_date"], as_of)
    # No due date = no SLA to measure against: neither overdue nor a miss.
    has_due = df["due_date"].notna()
    due = df["due_date"].where(has_due, as_of)
    df["overdue"] = df["open_as_of"] & has_due & (due < as_of)
    df["sla_met"] = np.where(resolved.notna() & (resolved <= as_of) & has_due,
                             resolved <= due, np.nan)
    df["age_bucket"] = pd.cut(df["age_bdays"], [b[0] for b in AGE_BUCKETS] + [10_000],
                              labels=[b[2] for b in AGE_BUCKETS])
    return df


def calendar_gaps(df: pd.DataFrame, as_of: date) -> tuple[list[int], int]:
    """Years missing from the holiday calendar that open tickets reach back
    into, and how many open tickets were raised in those years."""
    open_ = df[df["open_as_of"]]
    if open_.empty:
        return [], 0
    years = uncovered_years(min(open_["business_date"]), as_of)
    n = int(open_["business_date"].map(lambda d: d.year in years).sum())
    return years, n


def week_bounds(as_of: date) -> tuple[date, date]:
    """Monday..as_of of the reporting week."""
    return as_of - timedelta(days=as_of.weekday()), as_of


def kpis(df: pd.DataFrame, as_of: date) -> dict:
    start, end = week_bounds(as_of)
    rb = df["resolved_business_date"]
    in_week = rb.notna() & (rb >= start) & (rb <= end)
    four_wk = rb.notna() & (rb > as_of - timedelta(weeks=4)) & (rb <= end)
    open_ = df[df["open_as_of"]]
    return {
        "open_tickets": int(len(open_)),
        "overdue_tickets": int(open_["overdue"].sum()),
        "open_break_usd": float(open_["break_amount_usd"].sum()),
        "raised_this_week": int(df["business_date"].between(start, end).sum()),
        "resolved_this_week": int(in_week.sum()),
        "sla_met_this_week": _rate(df.loc[in_week, "sla_met"]),
        "sla_met_4wk": _rate(df.loc[four_wk, "sla_met"]),
        "oldest_open_bdays": int(open_["age_bdays"].max()) if len(open_) else 0,
    }


def _rate(s: pd.Series) -> float | None:
    s = s.dropna().astype(bool)
    return float(s.mean()) if len(s) else None


def by_team(df: pd.DataFrame, as_of: date) -> pd.DataFrame:
    rb = df["resolved_business_date"]
    four_wk = rb.notna() & (rb > as_of - timedelta(weeks=4)) & (rb <= as_of)
    open_ = df[df["open_as_of"]]
    out = pd.DataFrame({
        "open": open_.groupby("ops_team").size(),
        "overdue": open_.groupby("ops_team")["overdue"].sum(),
        "open_break_usd": open_.groupby("ops_team")["break_amount_usd"].sum(),
        "oldest_open_bdays": open_.groupby("ops_team")["age_bdays"].max(),
        "resolved_4wk": df[four_wk].groupby("ops_team").size(),
        "sla_met_4wk": df[four_wk].groupby("ops_team")["sla_met"].apply(_rate),
    })
    out.index.name = "ops_team"
    counts = ["open", "overdue", "resolved_4wk", "oldest_open_bdays"]
    out[counts] = out[counts].fillna(0).astype(int)
    out["open_break_usd"] = out["open_break_usd"].fillna(0.0)
    return out.reset_index().sort_values("overdue", ascending=False)


def aging(df: pd.DataFrame) -> pd.DataFrame:
    open_ = df[df["open_as_of"]]
    tbl = pd.crosstab(open_["ops_team"], open_["age_bucket"], dropna=False)
    tbl = tbl.reindex(columns=[b[2] for b in AGE_BUCKETS], fill_value=0)
    tbl["total"] = tbl.sum(axis=1)
    return tbl.reset_index()


def weekly_trend(df: pd.DataFrame, as_of: date, weeks: int = 8) -> pd.DataFrame:
    rows = []
    for i in range(weeks - 1, -1, -1):
        end = as_of - timedelta(weeks=i)
        start, _ = week_bounds(end)
        rb = df["resolved_business_date"]
        res = rb.notna() & (rb >= start) & (rb <= end)
        rows.append({
            "week_ending": end,
            "raised": int(df["business_date"].between(start, end).sum()),
            "resolved": int(res.sum()),
            "sla_met_pct": _rate(df.loc[res, "sla_met"]),
            "open_at_week_end": int((df["business_date"] <= end).where(
                rb.isna() | (rb > end), False).sum()),
        })
    return pd.DataFrame(rows)


def open_overdue_list(df: pd.DataFrame) -> pd.DataFrame:
    cols = ["key", "summary", "ops_team", "fund", "break_type", "priority",
            "break_amount_usd", "business_date", "due_date", "age_bdays", "status"]
    od = df[df["open_as_of"] & df["overdue"]]
    return od[cols].sort_values(["age_bdays", "break_amount_usd"], ascending=False)
