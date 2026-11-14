"""AUM by fund. Fund accounting is the book of record; custodian is shown for
comparison so a large gap is visible before anyone reads the AUM number."""

from __future__ import annotations

import pandas as pd

from opsreport.config import load_yaml


def fund_names() -> dict[str, str]:
    return {f["code"]: f["name"] for f in load_yaml("funds.yaml")["funds"]}


def aum_by_fund(accounting: pd.DataFrame, custodian: pd.DataFrame) -> pd.DataFrame:
    is_cash = accounting["asset_class"].eq("Cash")
    acct = accounting.assign(
        securities_mv=accounting["base_mv"].where(~is_cash, 0.0),
        cash=accounting["base_mv"].where(is_cash, 0.0),
    ).groupby("fund_code").agg(
        positions=("isin", "count"),
        securities_mv=("securities_mv", "sum"),
        cash=("cash", "sum"),
        accrued_income=("accrued_base", "sum"),
    )
    acct["aum"] = acct["securities_mv"] + acct["cash"] + acct["accrued_income"]
    cust = custodian.groupby("fund_code")["base_mv"].sum().rename("custodian_mv")
    out = acct.join(cust, how="outer").fillna(0.0)
    out["acct_vs_custodian_mv"] = (out["securities_mv"] + out["cash"]) - out["custodian_mv"]
    out.insert(0, "fund_name", out.index.map(fund_names()))
    return out.reset_index()


def aum_change(current: pd.DataFrame, prior: pd.DataFrame | None) -> pd.DataFrame:
    """Add prior-week AUM and week-over-week change, plus a TOTAL row."""
    out = current.copy()
    if prior is not None:
        out = out.merge(prior[["fund_code", "aum"]].rename(columns={"aum": "aum_prior"}),
                        on="fund_code", how="left")
        out["aum_change"] = out["aum"] - out["aum_prior"]
        out["aum_change_pct"] = out["aum_change"] / out["aum_prior"]
    total = {"fund_code": "TOTAL", "fund_name": "All funds",
             **out.select_dtypes("number").sum().to_dict()}
    if prior is not None:
        total["aum_change_pct"] = total["aum_change"] / total["aum_prior"]
    return pd.concat([out, pd.DataFrame([total])], ignore_index=True)
