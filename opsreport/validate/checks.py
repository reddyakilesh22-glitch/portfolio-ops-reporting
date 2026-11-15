"""Data-quality checks run before any numbers are reported.

Each check returns Issue records. Severity:
- ERROR   the input can't be trusted; the run stops (unless --allow-errors)
- WARNING reported in the Data Quality tab and excluded where noted

Checks never fix data. They say what is wrong, where, and what to do.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import pandas as pd

from opsreport.config import load_yaml

ERROR, WARNING = "ERROR", "WARNING"
BOND_CLASSES = {"Govt Bond", "MBS", "Corp Bond", "Sovereign", "Local Sovereign"}


@dataclass
class Issue:
    severity: str
    source: str
    check: str
    detail: str
    record: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def issues_frame(issues: list[Issue]) -> pd.DataFrame:
    cols = ["severity", "source", "check", "detail", "record"]
    return pd.DataFrame([i.as_dict() for i in issues], columns=cols)


def isin_is_valid(isin: str) -> bool:
    if not isinstance(isin, str) or len(isin) != 12 or not isin[:2].isalpha():
        return False
    if not isin[:11].isalnum() or not isin[11].isdigit():
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:11])
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return (10 - total % 10) % 10 == int(isin[11])


# ----------------------------------------------------------------- positions
def check_positions(df: pd.DataFrame, fx: pd.Series, label: str = "") -> list[Issue]:
    """`label` is appended to the source name, e.g. "(prior week)"."""
    src = (df["source"].iloc[0] if len(df) else "positions") + (f" {label}" if label else "")
    out: list[Issue] = []

    def rec(row) -> str:
        return f"{row.fund_code}/{row.ticker}/{row.isin}"

    required = ["fund_code", "isin", "currency", "quantity", "local_mv", "base_mv"]
    for col in required:
        for row in df[df[col].isna()].itertuples():
            out.append(Issue(ERROR, src, "required_field", f"{col} is blank", rec(row)))

    dupes = df[df.duplicated(["fund_code", "isin"], keep=False)]
    for row in dupes.drop_duplicates(["fund_code", "isin"]).itertuples():
        out.append(Issue(ERROR, src, "duplicate_position",
                         "same fund/ISIN appears more than once", rec(row)))

    if "duplicate_lots" in df:
        for row in df[df["duplicate_lots"].fillna(False).astype(bool)].itertuples():
            out.append(Issue(ERROR, src, "duplicate_lot",
                             "same fund/ISIN/location appears more than once in the "
                             "statement; amounts would be double-counted", rec(row)))

    securities = df[~df["asset_class"].eq("Cash")]
    for row in securities[~securities["isin"].map(isin_is_valid)].itertuples():
        out.append(Issue(ERROR, src, "isin_check_digit",
                         "ISIN fails format/check-digit validation", rec(row)))

    for row in df[~df["currency"].isin(fx.index)].itertuples():
        out.append(Issue(ERROR, src, "unknown_currency",
                         f"currency {row.currency} has no official FX rate", rec(row)))

    for row in securities[securities["quantity"] <= 0].itertuples():
        out.append(Issue(WARNING, src, "non_positive_quantity",
                         f"quantity {row.quantity:,.2f}", rec(row)))

    # Internal arithmetic: local MV = quantity x price (x 1/100 for bonds).
    factor = securities["asset_class"].isin(BOND_CLASSES).map({True: 0.01, False: 1.0})
    implied = securities["quantity"] * securities["price"] * factor
    bad = (implied - securities["local_mv"]).abs() > securities["local_mv"].abs() * 1e-4 + 1
    for row in securities[bad].itertuples():
        out.append(Issue(WARNING, src, "mv_arithmetic",
                         f"local MV {row.local_mv:,.2f} != qty x price", rec(row)))

    # FX: the rate each system used vs. the official 4pm file.
    tol = load_yaml("recon.yaml")["fx_rate_rel_tol"]
    official = df["currency"].map(fx)
    off = ((df["fx_rate"] - official).abs() / official) > tol
    for (ccy, used), grp in df[off].groupby(["currency", "fx_rate"]):
        out.append(Issue(WARNING, src, "fx_rate_vs_official",
                         f"{ccy} rate {used:g} vs official {fx[ccy]:g} "
                         f"({used / fx[ccy] - 1:+.2%}) on {len(grp)} position(s)",
                         ", ".join(grp["ticker"])))
    return out


# ---------------------------------------------------------------------- jira
def check_jira(df: pd.DataFrame) -> list[Issue]:
    out: list[Issue] = []
    cfg = load_yaml("jira_fields.yaml")["custom_fields"]

    for col in ("fund", "ops_team", "break_type", "business_date"):
        for row in df[df[col].isna()].itertuples():
            out.append(Issue(WARNING, "jira", "required_field",
                             f"{cfg[col]['name']} is blank; excluded from fund-level "
                             "metrics" if col == "fund" else f"{cfg[col]['name']} is blank",
                             str(row.key)))

    for col in ("fund", "ops_team", "break_type"):
        allowed = set(cfg[col]["options"])
        bad = df[df[col].notna() & ~df[col].isin(allowed)]
        for row in bad.itertuples():
            out.append(Issue(WARNING, "jira", "unexpected_option",
                             f"{cfg[col]['name']} '{getattr(row, col)}' not in config",
                             str(row.key)))

    for row in df[df["due_date"].isna()].itertuples():
        out.append(Issue(WARNING, "jira", "due_date_blank",
                         "Due date blank; excluded from SLA and overdue counts", str(row.key)))

    done = df["status_category"].eq("done")
    for row in df[done & df["resolved_business_date"].isna()].itertuples():
        out.append(Issue(WARNING, "jira", "done_without_resolved_date",
                         "status Done but Resolved Business Date blank; excluded "
                         "from SLA calculations", str(row.key)))
    for row in df[~done & df["resolved_business_date"].notna()].itertuples():
        out.append(Issue(WARNING, "jira", "open_with_resolved_date",
                         f"status {row.status} but Resolved Business Date set", str(row.key)))

    both = df["resolved_business_date"].notna() & df["business_date"].notna()
    early = df[both & (df["resolved_business_date"] < df["business_date"])]
    for row in early.itertuples():
        out.append(Issue(WARNING, "jira", "resolved_before_raised",
                         "Resolved Business Date is before Business Date", str(row.key)))

    for row in df[df["key"].duplicated()].itertuples():
        out.append(Issue(ERROR, "jira", "duplicate_key", "issue key repeated", str(row.key)))
    return out
