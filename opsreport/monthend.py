"""Month-end sign-off: is each fund's reconciliation ready for a controller to sign?

A fund controller signs off each fund's reconciliation once a month. Five
checks, as of the month's last business day:

    1. Every business day in the month reconciled (no failed or missing run)
    2. No High-materiality actionable break open
    3. Potential NAV impact under the 1 cent a share line
    4. No actionable break older than the last age band (10 business days)
    5. No overdue ticket

All pass: Ready to sign. Any fail: Needs attention, with the reasons. A month
whose last business day is still ahead is In progress, and the checks show
where it would stand if the month ended on the latest run.

Everything here reads the history store through dashboard_data, so the
sign-off, the dashboard and the pack can't disagree.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from opsreport import dashboard_data as dd
from opsreport.daily import business_days
from opsreport.transform import jira_metrics as jm

READY, ATTENTION, IN_PROGRESS = "Ready to sign", "Needs attention", "In progress"
MAX_AGE = dd.AGE_BANDS[-1]
CHECKS = [
    "Every day reconciled",
    "No high-materiality break",
    "NAV impact under 1¢ a share",
    f"No break older than {MAX_AGE} days",
    "No overdue ticket",
]
# How a failed check reads in the reasons column: the problem, not the rule.
PROBLEM = dict(zip(CHECKS, ["Days not reconciled", "High-materiality break", "NAV impact",
                            f"Break older than {MAX_AGE} days", "Overdue tickets"],
                   strict=True))


@dataclass
class MonthEnd:
    month: date                       # first calendar day
    month_end: date                   # last business day
    as_of: date | None                # last successful run in the month
    in_progress: bool
    days: list[date]
    failed_days: list[date]
    missing_days: list[date]
    funds: pd.DataFrame               # one row per fund: status, reasons, figures
    checks: pd.DataFrame              # fund x check: passed, detail
    open_breaks: pd.DataFrame = field(default_factory=pd.DataFrame)
    roll_forward: pd.DataFrame = field(default_factory=pd.DataFrame)
    nav_daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    runs: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def title(self) -> str:
        return f"{self.month:%B %Y}"


def months(h: dd.History) -> list[date]:
    """Months with at least one successful run, newest first."""
    ok = h.runs[h.runs["status"] == "OK"]["run_date"]
    return sorted({d.replace(day=1) for d in ok}, reverse=True)


def default_month(h: dd.History) -> date | None:
    """The latest month whose last business day has run, else the latest month."""
    ms = months(h)
    done = [m for m in ms if h.last is not None and _last_business_day(m) <= h.last]
    pick = done or ms
    return pick[0] if pick else None


def _last_business_day(month: date) -> date:
    last = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    return business_days(month, last)[-1]


def _fmt_days(days: list[date]) -> str:
    return ", ".join(f"{d:%d %b}" for d in days[:5]) + (" …" if len(days) > 5 else "")


def _tickers(rows: pd.DataFrame) -> str:
    return ", ".join(sorted(rows["ticker"].astype(str).unique()))


def month_end(h: dd.History, month: date, funds: tuple[str, ...] = ()) -> MonthEnd:
    month = month.replace(day=1)
    me = _last_business_day(month)
    days = business_days(month, me)
    runs = h.runs[h.runs["run_date"].between(month, me)]
    ok = sorted(runs.loc[runs["status"] == "OK", "run_date"])
    as_of = ok[-1] if ok else None
    in_progress = h.last is None or h.last < me
    horizon = min(me, h.last) if h.last else me
    failed = sorted(runs.loc[runs["status"] == "FAILED", "run_date"])
    missing = [d for d in days if d <= horizon and d not in set(runs["run_date"])]

    f = dd.Filters(month, me, "Monthly", funds)
    names = h.aum[["fund_code", "fund_name"]].drop_duplicates("fund_code")
    codes = [c for c in names["fund_code"] if not funds or c in funds]

    day = dd.Filters(as_of, as_of, "Daily", funds) if as_of else None
    b = dd.open_breaks(h, day) if day else h.breaks.iloc[0:0]
    act = b[b["status"] == "Actionable"]
    nav = dd.nav_impact_daily(h, day) if day else pd.DataFrame(
        columns=["fund_code", "level", "impact_per_share", "aum"])
    t = dd.tickets_in(h, f)
    p = jm.prepare(t, as_of) if as_of and len(t) else t.iloc[0:0].assign(
        open_as_of=False, overdue=False)
    overdue = p[p["open_as_of"] & p["overdue"]] if len(p) else p

    every_day = not failed and not missing
    so_far = [d for d in days if d <= horizon]
    day_detail = (f"{len(ok)} of {len(so_far)} days OK"
                  + (f"; failed {_fmt_days(failed)}" if failed else "")
                  + (f"; missing {_fmt_days(missing)}" if missing else ""))

    rows, summary = [], []
    for code in codes:
        a = act[act["fund_code"] == code]
        high = a[a["materiality"] == "High"]
        old = a[a["days_open"] > MAX_AGE]
        n = nav[nav["fund_code"] == code]
        cents = float(n["impact_per_share"].iloc[0]) * 100 if len(n) else None
        nav_ok = bool(len(n)) and n["level"].iloc[0] == dd.NAV_LEVELS[0]
        od = overdue[overdue["fund_code"] == code] if len(overdue) else overdue
        results = [
            (every_day, day_detail),
            (as_of is not None and high.empty,
             f"{len(high)} open ({_tickers(high)})" if len(high) else "none open"),
            (nav_ok, "no successful run" if cents is None else f"{cents:+.2f}¢ a share"
             + ("" if n["level"].iloc[0] != dd.NAV_LEVELS[2] else ": over 0.5% of NAV")),
            (as_of is not None and old.empty,
             f"{len(old)} open ({_tickers(old)})" if len(old) else "none"),
            (as_of is not None and od.empty,
             f"{len(od)} ({', '.join(od['key'])})" if len(od) else "none"),
        ]
        for check, (passed, detail) in zip(CHECKS, results, strict=True):
            rows.append({"fund_code": code, "check": check, "passed": bool(passed),
                         "detail": detail})
        failed_checks = [c for c, (ok_, _) in zip(CHECKS, results, strict=True) if not ok_]
        status = IN_PROGRESS if in_progress else (READY if not failed_checks else ATTENTION)
        rf = dd.roll_forward(h, dd.Filters(month, me, "Monthly", (code,)))
        mr = dd.match_trend(h, dd.Filters(month, me, "Monthly", (code,)))
        summary.append({
            "fund_code": code,
            "fund_name": names.set_index("fund_code")["fund_name"].get(code),
            "status": status,
            "checks_passed": f"{len(CHECKS) - len(failed_checks)} of {len(CHECKS)}",
            "reasons": "; ".join(f"{PROBLEM[c]}: {d}" for c, (ok_, d)
                                 in zip(CHECKS, results, strict=True) if not ok_),
            "aum": float(n["aum"].iloc[0]) if len(n) else None,
            "open_breaks": len(a),
            "exposure_usd": float(a["exposure_usd"].sum()),
            "impact_per_share": cents / 100 if cents is not None else None,
            "new_in_month": int(rf["new"].sum()) if len(rf) else 0,
            "cleared_in_month": int(rf["cleared"].sum()) if len(rf) else 0,
            "match_rate": float(mr["match_rate"].iloc[0]) if len(mr) else None,
        })

    daily, _ = dd.break_flows(h, dd.Filters(month, me, "Daily", funds))
    return MonthEnd(
        month=month, month_end=me, as_of=as_of, in_progress=in_progress, days=days,
        failed_days=failed, missing_days=missing,
        funds=pd.DataFrame(summary), checks=pd.DataFrame(rows),
        open_breaks=b.sort_values(["status", "exposure_usd"], ascending=[True, False]),
        roll_forward=daily[daily["run_date"].between(month, me)],
        nav_daily=dd.nav_impact_daily(h, dd.Filters(month, me, "Daily", funds)),
        runs=runs.sort_values("run_date"))


def check_matrix(m: MonthEnd) -> pd.DataFrame:
    """Funds down, checks across: '✓' or '✗ <detail>' in each cell."""
    if m.checks.empty:
        return pd.DataFrame(columns=["fund_code", *CHECKS])
    cell = m.checks.assign(cell=m.checks.apply(
        lambda r: "✓" if r["passed"] else f"✗ {r['detail']}", axis=1))
    out = cell.pivot(index="fund_code", columns="check", values="cell")[CHECKS]
    return out.reindex(m.funds["fund_code"]).reset_index()
