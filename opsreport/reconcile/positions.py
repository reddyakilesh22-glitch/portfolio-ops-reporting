"""Custodian vs. fund accounting position reconciliation.

Join key is (fund_code, isin). Every row of the outer join is compared on
quantity, price, local MV and base MV; anything outside tolerance is a break.
Breaks are classified in a fixed order so each gets one most-likely cause:

    1. Missing Trade      position on one side only
    2. Cash Break         cash balance differs
    3. Static Data        security currency differs between systems
    4. Corporate Action   quantity ratio is a split ratio and MV is ~unchanged
    5. Quantity Break     quantities differ
    6. Price Break        prices differ
    7. FX Rate            local MV agrees but base MV differs
    8. Unexplained        outside tolerance, no rule matched (investigate)
"""

from __future__ import annotations

import pandas as pd

from opsreport.config import load_yaml

KEY = ["fund_code", "isin"]
COMPARE = ["ticker", "description", "asset_class", "currency", "quantity", "price",
           "local_mv", "fx_rate", "base_mv"]
BREAK_COLUMNS = [
    "fund_code", "isin", "ticker", "description", "asset_class", "break_type",
    "materiality", "likely_cause", "currency_cust", "currency_acct",
    "quantity_cust", "quantity_acct", "quantity_diff", "price_cust", "price_acct",
    "fx_rate_cust", "fx_rate_acct", "base_mv_cust", "base_mv_acct", "base_mv_diff",
    "abs_base_mv_diff", "exposure_usd",
]


def _fmt(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def reconcile(custodian: pd.DataFrame, accounting: pd.DataFrame,
              cfg: dict | None = None) -> pd.DataFrame:
    """Return one row per break with its type and a plain-English likely cause."""
    cfg = cfg or load_yaml("recon.yaml")
    tol, ca = cfg["tolerances"], cfg["corporate_action"]

    extra = ["locations"] if "locations" in custodian else []
    c = custodian[KEY + COMPARE + extra].rename(
        columns={k: f"{k}_cust" for k in COMPARE} | {"locations": "custodian_locations"})
    a = accounting[KEY + COMPARE].rename(columns={k: f"{k}_acct" for k in COMPARE})
    m = c.merge(a, on=KEY, how="outer", indicator=True, validate="one_to_one")

    for col in ("ticker", "description", "asset_class"):
        m[col] = m[f"{col}_cust"].fillna(m[f"{col}_acct"])
    m["quantity_diff"] = m["quantity_cust"].fillna(0) - m["quantity_acct"].fillna(0)
    m["base_mv_diff"] = m["base_mv_cust"].fillna(0) - m["base_mv_acct"].fillna(0)
    m["abs_base_mv_diff"] = m["base_mv_diff"].abs()

    both = m["_merge"].eq("both")
    is_cash = m["asset_class"].eq("Cash")
    qty_off = m["quantity_diff"].abs() > tol["quantity_abs"]
    mv_off = m["abs_base_mv_diff"] > tol["base_mv_abs_usd"]
    px_off = ((m["price_cust"] - m["price_acct"]).abs()
              / m["price_acct"].abs()) > tol["price_rel"]
    local_same = ((m["local_mv_cust"] - m["local_mv_acct"]).abs()
                  <= m["local_mv_acct"].abs() * tol["local_mv_rel"] + tol["base_mv_abs_usd"])
    ccy_diff = m["currency_cust"].ne(m["currency_acct"])
    fx_off = ((m["fx_rate_cust"] - m["fx_rate_acct"]).abs()
              / m["fx_rate_acct"].abs()) > cfg["fx_rate_rel_tol"]
    m["_px_off"], m["_fx_off"] = px_off, fx_off

    ratio = m["quantity_cust"] / m["quantity_acct"]
    near_split = pd.Series(False, index=m.index)
    for r in ca["ratios"]:
        near_split |= (ratio - r).abs() <= r * ca["ratio_tol"]
    mv_flat = (m["abs_base_mv_diff"] / m["base_mv_acct"].abs()) <= ca["mv_rel_tol"]

    rules = [
        ("Missing Trade", ~both),
        ("Cash Break", both & is_cash & mv_off),
        ("Static Data", both & ~is_cash & ccy_diff),
        ("Corporate Action", both & ~is_cash & qty_off & near_split & mv_flat),
        ("Quantity Break", both & ~is_cash & qty_off),
        ("Price Break", both & ~is_cash & px_off & mv_off),
        ("FX Rate", both & ~is_cash & mv_off & local_same),
        ("Unexplained", both & mv_off),
    ]
    m["break_type"] = None
    for name, mask in rules:
        m.loc[mask & m["break_type"].isna(), "break_type"] = name

    breaks = m[m["break_type"].notna()].copy()
    breaks["likely_cause"] = breaks.apply(_cause, axis=1)

    # Exposure = what is at risk, which is not always the $ difference. After an
    # unprocessed split the values still agree, but the whole position carries
    # the wrong quantity, so the position value is what's exposed.
    position = breaks[["base_mv_cust", "base_mv_acct"]].abs().max(axis=1)
    breaks["exposure_usd"] = breaks["abs_base_mv_diff"].where(
        breaks["break_type"] != "Corporate Action", position)
    bands = cfg["materiality"]
    breaks["materiality"] = pd.cut(
        breaks["exposure_usd"], [-1, bands["medium"], bands["high"], float("inf")],
        labels=["Low", "Medium", "High"]).astype(str)
    breaks = breaks.sort_values(["exposure_usd"], ascending=False)
    cols = BREAK_COLUMNS + (["custodian_locations"] if extra else [])
    return breaks[cols].reset_index(drop=True)


def _cause(r: pd.Series) -> str:
    """Primary cause, plus any second difference the label would otherwise hide."""
    text = _primary_cause(r)
    t = r["break_type"]
    extra = []
    if t == "Quantity Break" and r["_px_off"]:
        extra.append(f"price also differs ({r['price_acct'] / r['price_cust'] - 1:+.2%} "
                     "in accounting)")
    if t in ("Quantity Break", "Price Break") and r["_fx_off"]:
        extra.append(f"FX rate also differs ({r['fx_rate_acct']:g} acct vs "
                     f"{r['fx_rate_cust']:g} cust)")
    return text + ("; " + "; ".join(extra) if extra else "")


def _primary_cause(r: pd.Series) -> str:
    t = r["break_type"]
    if t == "Missing Trade":
        if pd.isna(r["quantity_cust"]):
            return (f"In accounting only ({_fmt(r['quantity_acct'])}): likely failed or "
                    "pending settlement at custodian")
        return (f"At custodian only ({_fmt(r['quantity_cust'])}): likely trade not "
                "booked in accounting")
    if t == "Cash Break":
        return (f"Cash differs by {r['base_mv_diff']:,.2f}: check unbooked income, "
                "fees or settlements")
    if t == "Static Data":
        return (f"Currency {r['currency_cust']} at custodian vs {r['currency_acct']} in "
                "accounting: fix security master")
    if t == "Corporate Action":
        ratio = r["quantity_cust"] / r["quantity_acct"]
        return (f"Quantity ratio {ratio:g}:1 with MV unchanged: corporate action "
                "(split) processed on one side only")
    if t == "Quantity Break":
        return (f"Quantity differs by {_fmt(r['quantity_diff'])}: check unsettled "
                "trades / allocations")
    if t == "Price Break":
        rel = r["price_acct"] / r["price_cust"] - 1
        return f"Accounting price {rel:+.2%} vs custodian: check pricing source"
    if t == "FX Rate":
        return (f"Local MV agrees; FX {r['fx_rate_acct']:g} (acct) vs "
                f"{r['fx_rate_cust']:g} (cust): check FX rate used")
    return "Outside tolerance with no matching rule: investigate manually"


def summarize(breaks: pd.DataFrame) -> pd.DataFrame:
    """Break counts and absolute $ by fund and type."""
    if breaks.empty:
        return pd.DataFrame(columns=["fund_code", "break_type", "breaks", "abs_base_mv_diff"])
    return (breaks.groupby(["fund_code", "break_type"], as_index=False)
            .agg(breaks=("isin", "count"), abs_base_mv_diff=("abs_base_mv_diff", "sum"))
            .sort_values(["fund_code", "abs_base_mv_diff"], ascending=[True, False]))
