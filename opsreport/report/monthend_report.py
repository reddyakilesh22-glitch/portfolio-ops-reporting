"""The month-end sign-off pack: one workbook a fund controller signs.

Tabs: Sign-off | Checks | Open at Month-end | Roll-forward | NAV Impact | Run Log.
Same safe writer as the other reports (text can never become a formula).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pandas as pd

from opsreport.monthend import ATTENTION, CHECKS, IN_PROGRESS, READY, MonthEnd, check_matrix
from opsreport.report.excel_report import _Writer

OPEN_COLS = ["status", "fund_code", "ticker", "break_type", "materiality", "exposure_usd",
             "base_mv_diff", "days_open", "ticket_key", "ticket_status", "ticket_due",
             "explanation", "likely_cause", "first_seen", "break_id"]
NAV_COLS = ["run_date", "fund_code", "aum", "nav_per_share", "net_diff_usd",
            "impact_per_share", "impact_pct_of_nav", "level"]
RUN_COLS = ["run_date", "status", "actionable", "expected", "actionable_usd", "errors",
            "warnings", "message"]


def pack_name(m: MonthEnd) -> str:
    return f"monthend_signoff_{m.month:%Y%m}.xlsx"


def write_monthend(m: MonthEnd, target: Path | BytesIO) -> Path | BytesIO:
    if isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
    w = _Writer(target)          # type: ignore[arg-type]   # ExcelWriter takes a buffer
    _signoff(w, m)

    ws = w.sheet("Checks")
    ws.write(0, 0, f"Checks per fund as of {m.month_end:%d %b %Y}", w.f["title"])
    ws.write(1, 0, "✓ passed · ✗ needs attention, with the detail", w.f["subtitle"])
    w.table(ws, check_matrix(m), 3, widths={c: 34 for c in CHECKS})

    ws = w.sheet("Open at Month-end")
    when = f"{m.as_of:%d %b %Y}" if m.as_of else "n/a"
    ws.write(0, 0, f"Breaks open on {when}", w.f["title"])
    ws.write(1, 0, "Actionable first, largest first. Expected = explained by trades not yet "
                   "settled.", w.f["subtitle"])
    ob = m.open_breaks
    w.table(ws, ob[[c for c in OPEN_COLS if c in ob]], 3, autofilter=True,
            widths={"explanation": 50, "likely_cause": 40, "break_id": 34})

    ws = w.sheet("Roll-forward")
    ws.write(0, 0, f"Break roll-forward, {m.title}", w.f["title"])
    ws.write(1, 0, "Actionable breaks: opening + new - cleared = closing, each business day",
             w.f["subtitle"])
    rf = m.roll_forward
    if len(rf):
        total = pd.DataFrame([{"run_date": "Month", "opening": rf["opening"].iloc[0],
                               "new": rf["new"].sum(), "cleared": rf["cleared"].sum(),
                               "closing": rf["closing"].iloc[-1]}])
        rf = pd.concat([rf.astype({"run_date": object}), total], ignore_index=True)
    w.table(ws, rf, 3)

    ws = w.sheet("NAV Impact")
    ws.write(0, 0, "Potential NAV impact by fund and day", w.f["title"])
    ws.write(1, 0, "Net unexplained difference if the books are wrong. Timing breaks and "
                   "failed settlements excluded. Lines: 1 cent a share; 0.5% of NAV.",
             w.f["subtitle"])
    nd = m.nav_daily
    w.table(ws, nd[[c for c in NAV_COLS if c in nd]], 3, autofilter=True,
            widths={"level": 26})

    ws = w.sheet("Run Log")
    ws.write(0, 0, f"Daily runs, {m.title}", w.f["title"])
    gaps = ([f"failed: {', '.join(f'{d:%d %b}' for d in m.failed_days)}"]
            if m.failed_days else []) + \
           ([f"missing: {', '.join(f'{d:%d %b}' for d in m.missing_days)}"]
            if m.missing_days else [])
    ws.write(1, 0, "; ".join(gaps) or "Every business day ran.", w.f["subtitle"])
    w.table(ws, m.runs[[c for c in RUN_COLS if c in m.runs]], 3, widths={"message": 60})
    w.close()
    return target


def _signoff(w: _Writer, m: MonthEnd) -> None:
    ws = w.sheet("Sign-off")
    ws.write(0, 0, f"Month-end reconciliation sign-off: {m.title}", w.f["title"])
    state = (f"IN PROGRESS: last run {m.as_of:%d %b}, month ends {m.month_end:%d %b}. "
             "Figures show where each fund would stand today." if m.in_progress and m.as_of
             else f"As of the month's last business day, {m.month_end:%A %d %B %Y}.")
    ws.write(1, 0, state + " Synthetic data.", w.f["subtitle"])
    f = m.funds
    tiles = [("Ready to sign", int((f["status"] == READY).sum()) if len(f) else 0, "int"),
             ("Needs attention", int((f["status"] == ATTENTION).sum()) if len(f) else 0,
              "int"),
             ("Open breaks", int(f["open_breaks"].sum()) if len(f) else 0, "int"),
             ("Exposure at risk", float(f["exposure_usd"].sum()) if len(f) else 0.0,
              "money"),
             ("Days run OK", f"{len(m.days) - len(m.failed_days) - len(m.missing_days)}"
                             f" of {len(m.days)}" if not m.in_progress else "n/a", "int")]
    for i, (label, value, kind) in enumerate(tiles):
        w.kpi(ws, 3, i * 2, label, value, kind)
    sign = f.assign(signed_off_by="", sign_off_date=None, comments="")
    end = w.table(ws, sign, 6, widths={"fund_name": 28, "status": 16, "reasons": 70,
                                       "signed_off_by": 20, "comments": 30})
    col = list(sign.columns).index("status")
    for value, fmt in ((READY, "low"), (ATTENTION, "high"), (IN_PROGRESS, "medium")):
        ws.conditional_format(7, col, end - 2, col, {
            "type": "cell", "criteria": "==", "value": f'"{value}"', "format": w.f[fmt]})
    ws.write(end, 0, "Sign off a fund only when its status is Ready to sign, or record in "
                     "Comments why an open item is accepted.", w.f["subtitle"])
    ws.freeze_panes(7, 2)
    ws.activate()
