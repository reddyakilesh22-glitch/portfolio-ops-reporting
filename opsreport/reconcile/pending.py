"""Explain breaks with trades that are booked but not yet settled.

Fund accounting books a trade on trade date; the custodian only holds it once
it settles (T+1 in the US, usually T+2 elsewhere). In between, the two records
differ by exactly the trade quantity. That break is *timing*, not an error.

Rules, applied to quantity and missing-position breaks only:

- the open trades on the position explain the whole quantity difference and
  none is past its settlement date   -> **Expected** (no action)
- they explain it, but a trade's settlement date has passed
                                      -> Actionable: settlement failed
- they explain only part of it        -> Actionable, with the unexplained part
- no open trade on the position       -> Actionable

A partly explained break stays actionable so nothing unexplained can hide
behind a pending trade.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from opsreport.config import load_yaml
from opsreport.reconcile.positions import KEY, _fmt

EXPLAINABLE = {"Quantity Break", "Missing Trade"}
EXPECTED, ACTIONABLE = "Expected", "Actionable"


def match_pending(breaks: pd.DataFrame, pending: pd.DataFrame, as_of: date,
                  qty_tol: float | None = None) -> pd.DataFrame:
    """Add `status` (Expected / Actionable) and `explanation` to each break."""
    tol = qty_tol if qty_tol is not None else \
        load_yaml("recon.yaml")["tolerances"]["quantity_abs"]
    out = breaks.copy()
    out["status"] = ACTIONABLE
    out["explanation"] = ""
    if out.empty or pending.empty:
        return out
    by_pos = {k: g for k, g in pending.groupby(KEY)}

    for idx, r in out.iterrows():
        trades = by_pos.get((r["fund_code"], r["isin"]))
        if r["break_type"] not in EXPLAINABLE or trades is None:
            continue
        booked_not_settled = -r["quantity_diff"]      # quantity_diff = custodian - acct
        pending_qty = trades["signed_quantity"].sum()
        ids = ", ".join(trades["trade_id"])
        if abs(booked_not_settled - pending_qty) > tol:
            out.at[idx, "explanation"] = (
                f"{_fmt(pending_qty)} of {_fmt(booked_not_settled)} explained by pending "
                f"trade(s) {ids}; {_fmt(booked_not_settled - pending_qty)} unexplained")
            continue
        late = trades[trades["settle_date"] <= as_of]
        if late.empty:
            out.at[idx, "status"] = EXPECTED
            out.at[idx, "explanation"] = (
                f"Pending trade(s) {ids}; settles {max(trades['settle_date']):%a %d %b}")
        else:
            out.at[idx, "explanation"] = (
                f"Settlement failed: {', '.join(late['trade_id'])} was due "
                f"{min(late['settle_date']):%a %d %b}; chase the custodian or broker")
    return out


def unmatched_trades(pending: pd.DataFrame, breaks: pd.DataFrame) -> pd.DataFrame:
    """Open trades on positions where the two records agree.

    Either the trade settled and the OMS report is stale, or it was never
    booked in accounting. Both are worth a look.
    """
    if pending.empty:
        return pending
    broken = set(zip(breaks["fund_code"], breaks["isin"], strict=True))
    keys = pd.Series(list(zip(pending["fund_code"], pending["isin"], strict=True)),
                     index=pending.index)
    return pending[~keys.isin(broken)]
