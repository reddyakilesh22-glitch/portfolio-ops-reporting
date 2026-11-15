"""The ops analysts' daily exceptions workbook: what needs action today.

Tabs: Action Today | Expected | Cleared | Data Quality. Same safe writer as
the weekly report (text from vendor files and Jira can never become a formula).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from opsreport.report.excel_report import _Writer

ACTION_COLS = ["new", "fund_code", "ticker", "break_type", "materiality", "exposure_usd",
               "days_open", "what_to_check", "ticket_key", "ticket_status", "ticket_due",
               "ops_team", "first_seen", "pricing_source", "isin", "break_id"]
EXPECTED_COLS = ["fund_code", "ticker", "quantity_diff", "exposure_usd", "explanation",
                 "first_seen", "isin", "break_id"]
CLEARED_COLS = ["fund_code", "ticker", "break_type", "first_seen", "days_open",
                "exposure_usd", "ticket_key", "break_id"]
URGENCY = {"Overdue": 0, None: 1, "Open": 2, "Resolved": 3}   # anything else: 1


def action_list(breaks: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Actionable breaks, most urgent first: overdue tickets, then breaks with
    no ticket yet, then by size."""
    a = breaks[breaks["status"] == "Actionable"].copy()
    a["new"] = (a["first_seen"] == as_of).map({True: "NEW", False: ""})
    a["what_to_check"] = a["explanation"].where(a["explanation"].astype(bool),
                                                a["likely_cause"])
    a["_u"] = a["ticket_status"].map(lambda s: URGENCY.get(s, 1))
    a = a.sort_values(["_u", "exposure_usd"], ascending=[True, False])
    return a[[c for c in ACTION_COLS if c in a]]


def write_exceptions(as_of: date, prev: date | None, breaks: pd.DataFrame,
                     cleared: pd.DataFrame, issues: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    w = _Writer(path)
    act = action_list(breaks, as_of)
    exp = breaks[breaks["status"] == "Expected"]

    ws = w.sheet("Action Today")
    ws.write(0, 0, f"Daily exceptions: {as_of:%A %d %B %Y}", w.f["title"])
    ws.write(1, 0, "Actionable breaks, most urgent first: overdue tickets, then breaks "
                   "with no ticket, then by exposure. Synthetic data.", w.f["subtitle"])
    tiles = [("Actionable breaks", len(act), "int"),
             ("Exposure at risk", act["exposure_usd"].sum(), "money"),
             ("New today", int((act["new"] == "NEW").sum()), "int"),
             ("Overdue tickets", int((act["ticket_status"] == "Overdue").sum()), "int"),
             ("No ticket yet", int(act["ticket_key"].isna().sum()), "int"),
             ("Expected (timing)", len(exp), "int")]
    for i, (label, value, kind) in enumerate(tiles):
        w.kpi(ws, 3, i * 2, label, value, kind)
    end = w.table(ws, act, 6, autofilter=True,
                  widths={"what_to_check": 62, "break_id": 34, "ticker": 16,
                          "break_type": 16, "new": 6})
    ws.freeze_panes(7, 3)
    if len(act):
        col = list(act.columns).index("ticket_status")
        ws.conditional_format(7, col, end - 2, col, {
            "type": "cell", "criteria": "==", "value": '"Overdue"', "format": w.f["high"]})
        mcol = list(act.columns).index("materiality")
        for level in ("high", "medium", "low"):
            ws.conditional_format(7, mcol, end - 2, mcol, {
                "type": "cell", "criteria": "==", "value": f'"{level.capitalize()}"',
                "format": w.f[level]})
    ws.activate()

    ws = w.sheet("Expected")
    ws.write(0, 0, "Expected: explained by trades that have not reached their settlement "
                   "date", w.f["title"])
    ws.write(1, 0, "No action unless still here after the settlement date.", w.f["subtitle"])
    w.table(ws, exp[[c for c in EXPECTED_COLS if c in exp]], 3, autofilter=True,
            widths={"explanation": 60, "break_id": 34, "ticker": 16})

    ws = w.sheet("Cleared")
    since = f"since {prev:%a %d %b}" if prev else "(no earlier run)"
    ws.write(0, 0, f"Cleared {since}", w.f["title"])
    w.table(ws, cleared[[c for c in CLEARED_COLS if c in cleared]], 2,
            widths={"break_id": 34, "ticker": 16, "break_type": 16})

    ws = w.sheet("Data Quality")
    ws.write(0, 0, "Data-quality findings", w.f["title"])
    w.table(ws, issues, 2, autofilter=True, widths={"detail": 80, "record": 30})
    w.close()
    return path
