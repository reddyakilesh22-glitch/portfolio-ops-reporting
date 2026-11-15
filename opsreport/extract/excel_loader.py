"""Load custodian, fund accounting, and FX workbooks into canonical DataFrames.

Every loader returns the same canonical position columns (see POSITION_COLUMNS)
so reconciliation never has to know which system a row came from.

Defensive by design, because source files change without notice:
- columns are matched by header text from config/excel_layouts.yaml;
- the custodian header row is located by scanning, not assumed;
- the as-of date inside each file must match the date being reported;
- the custodian's own statement Total is used as a control check.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from opsreport.config import ROOT, load_yaml

log = logging.getLogger(__name__)

RAW_DIR = ROOT / "data" / "raw"
POSITION_COLUMNS = [
    "source", "as_of", "fund_code", "isin", "ticker", "description", "asset_class",
    "currency", "quantity", "price", "local_mv", "fx_rate", "base_mv",
]
NUMERIC = ["quantity", "price", "local_mv", "fx_rate", "base_mv"]
CONTROL_TOLERANCE_USD = 1.00


class SourceFileError(RuntimeError):
    """A source file is missing, mis-dated, or not in the expected layout."""


# ------------------------------------------------------------------ helpers
def to_number(series: pd.Series) -> pd.Series:
    """Coerce exported numbers: '1,234.50', '(1,234.50)', ' 12 ', '-', ''."""
    if pd.api.types.is_numeric_dtype(series):
        return series.astype(float)
    s = series.astype("string").str.strip()
    negative = s.str.match(r"^\(.*\)$", na=False)
    s = s.str.replace(r"[,\s()$]", "", regex=True).replace({"": None, "-": None})
    out = pd.to_numeric(s, errors="raise").astype(float)
    return out.where(~negative, -out)


def parse_statement_date(text: str, formats: list[str], where: str) -> date:
    """Parse a statement date using the first configured format that fits."""
    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SourceFileError(f"{where}: can't read statement date '{text}'; tried formats "
                          f"{formats} (config/excel_layouts.yaml: as_of_formats)")


def source_path(kind: str, as_of: date, raw_dir: Path = RAW_DIR) -> Path:
    pattern = load_yaml("excel_layouts.yaml")[kind]["file_pattern"]
    path = raw_dir / as_of.isoformat() / pattern.format(yyyymmdd=f"{as_of:%Y%m%d}")
    if not path.exists():
        raise SourceFileError(f"{kind} file not found: {path}")
    return path


def _select_columns(df: pd.DataFrame, mapping: dict[str, str], where: str) -> pd.DataFrame:
    headers = {str(c).strip(): c for c in df.columns}
    missing = [src for src in mapping.values() if src not in headers]
    if missing:
        raise SourceFileError(
            f"{where}: expected column(s) {missing} not found. "
            f"Columns present: {list(headers)}. Update config/excel_layouts.yaml "
            "if the vendor renamed them."
        )
    return df[[headers[src] for src in mapping.values()]].set_axis(list(mapping), axis=1)


def _clean_common(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for col in ("isin", "ticker", "description", "asset_class", "currency"):
        df[col] = df[col].astype("string").str.strip()
    df["isin"] = df["isin"].str.upper()
    df["currency"] = df["currency"].str.upper()
    for col in NUMERIC:
        df[col] = to_number(df[col])
    return df


# ---------------------------------------------------------------- custodian
def load_custodian(as_of: date, path: Path | None = None) -> pd.DataFrame:
    cfg = load_yaml("excel_layouts.yaml")["custodian"]
    funds = {f["custodian_sheet"]: f for f in load_yaml("funds.yaml")["funds"]}
    path = path or source_path("custodian", as_of)
    sheets = pd.read_excel(path, sheet_name=None, header=None, dtype=object)

    unknown = set(sheets) - set(funds)
    if unknown:
        raise SourceFileError(f"{path.name}: unknown account sheet(s) {sorted(unknown)}; "
                              "add them to config/funds.yaml")
    missing = set(funds) - set(sheets)
    if missing:
        raise SourceFileError(f"{path.name}: account sheet(s) missing {sorted(missing)}")

    frames = []
    for sheet, raw in sheets.items():
        fund = funds[sheet]
        where = f"{path.name}[{sheet}]"

        # 1) header row: first row containing the marker cell
        scan = raw.head(cfg["max_header_scan_rows"])
        hits = scan.index[scan.apply(
            lambda r: r.astype(str).str.strip().eq(cfg["header_marker"]).any(), axis=1)]
        if hits.empty:
            raise SourceFileError(f"{where}: no header row containing "
                                  f"'{cfg['header_marker']}' in first "
                                  f"{cfg['max_header_scan_rows']} rows")
        h = hits[0]

        # 2) as-of date printed in the preamble must match the reporting date
        preamble = scan.iloc[:h, 0].dropna().astype(str)
        stamp = preamble[preamble.str.startswith(cfg["as_of_prefix"])]
        if stamp.empty:
            raise SourceFileError(f"{where}: no '{cfg['as_of_prefix']}' line in preamble")
        file_date = parse_statement_date(
            stamp.iloc[0].removeprefix(cfg["as_of_prefix"]).strip(),
            cfg["as_of_formats"], where)
        if file_date != as_of:
            raise SourceFileError(f"{where}: statement is as of {file_date}, expected {as_of}")

        body = raw.iloc[h + 1:].set_axis(raw.iloc[h].tolist(), axis=1)
        df = _select_columns(body, cfg["columns"], where)

        # 3) keep only position rows for this account; capture the control total
        account = df["account"].astype("string").str.strip()
        total_rows = df[account.eq(cfg["total_label"])]
        positions = df[account.eq(fund["custodian_account"])]
        dropped = df.drop(index=total_rows.index.union(positions.index)).dropna(how="all")
        log.debug("%s: %d positions, %d non-position rows ignored",
                  where, len(positions), len(dropped))

        positions = _clean_common(positions)
        if len(total_rows) != 1:
            raise SourceFileError(f"{where}: expected 1 '{cfg['total_label']}' row, "
                                  f"found {len(total_rows)}")
        control = float(to_number(total_rows["base_mv"]).iloc[0])
        loaded = positions["base_mv"].sum()
        if abs(control - loaded) > CONTROL_TOLERANCE_USD:
            raise SourceFileError(
                f"{where}: loaded base MV {loaded:,.2f} != statement total {control:,.2f} "
                f"(diff {loaded - control:,.2f}); rows were dropped or mis-parsed")

        positions["fund_code"] = fund["code"]
        present = {str(c).strip(): c for c in body.columns}
        for name, header in cfg.get("optional_columns", {}).items():
            positions[name] = (body.loc[positions.index, present[header]]
                               .astype("string").str.strip()
                               if header in present else pd.NA)
        frames.append(positions)

    rows = pd.concat(frames, ignore_index=True)
    out = aggregate_lots(rows, path.name)
    out["source"] = "custodian"
    out["as_of"] = as_of
    log.info("Custodian %s: %d positions (%d rows) across %d accounts",
             as_of, len(out), len(rows), len(frames))
    return out[[*POSITION_COLUMNS, "lots", "locations"]]


LOT_KEYS = ["fund_code", "isin"]
LOT_MUST_AGREE = ["ticker", "description", "asset_class", "currency", "price", "fx_rate"]


def aggregate_lots(rows: pd.DataFrame, where: str = "custodian") -> pd.DataFrame:
    """Collapse custodian lot/location rows to one row per fund and ISIN.

    Custodians may report a holding once per depository. Quantities and market
    values add; descriptive fields, price and FX rate must be identical across
    lots, otherwise the file is inconsistent and the run stops.
    """
    g = rows.groupby(LOT_KEYS, sort=False, dropna=False)
    disagree = [c for c in LOT_MUST_AGREE if (g[c].nunique(dropna=False) > 1).any()]
    if disagree:
        bad = g[disagree].nunique(dropna=False)
        bad = bad[(bad > 1).any(axis=1)].index.tolist()
        raise SourceFileError(f"{where}: lots of the same holding disagree on {disagree} "
                              f"for {bad[:5]}")
    if "locations" not in rows:
        rows = rows.assign(locations=pd.NA)
    out = g.agg(
        **{c: (c, "first") for c in LOT_MUST_AGREE},
        quantity=("quantity", "sum"),
        local_mv=("local_mv", "sum"),
        base_mv=("base_mv", "sum"),
        lots=("isin", "size"),
        locations=("locations", lambda s: "+".join(s.dropna().astype(str)) or pd.NA),
    ).reset_index()
    split = out[out["lots"] > 1]
    if len(split):
        log.info("%s: %d holding(s) reported across multiple locations: %s", where,
                 len(split), ", ".join(split["fund_code"] + "/" + split["ticker"]))
    return out


# --------------------------------------------------------------- accounting
def load_accounting(as_of: date, path: Path | None = None) -> pd.DataFrame:
    cfg = load_yaml("excel_layouts.yaml")["accounting"]
    path = path or source_path("accounting", as_of)
    raw = pd.read_excel(path, sheet_name=cfg["sheet"], dtype=object)
    df = _clean_common(_select_columns(raw, cfg["columns"], path.name))

    dates = pd.to_datetime(df["position_date"]).dt.date.unique()
    if len(dates) != 1 or dates[0] != as_of:
        raise SourceFileError(f"{path.name}: position dates {list(dates)}, expected {as_of}")

    known = {f["code"] for f in load_yaml("funds.yaml")["funds"]}
    unknown = set(df["fund_code"]) - known
    if unknown:
        raise SourceFileError(f"{path.name}: unknown fund code(s) {sorted(unknown)}")

    df["accrued_base"] = to_number(df["accrued_base"]).fillna(0.0)
    df["source"] = "accounting"
    df["as_of"] = as_of
    log.info("Accounting %s: %d positions", as_of, len(df))
    return df[[*POSITION_COLUMNS, "accrued_base", "pricing_source"]]


# ----------------------------------------------------------------------- fx
def load_fx(as_of: date, path: Path | None = None) -> pd.Series:
    """Return units-of-currency per 1 USD, indexed by ISO currency code."""
    cfg = load_yaml("excel_layouts.yaml")["fx"]
    path = path or source_path("fx", as_of)
    raw = pd.read_excel(path, sheet_name=cfg["sheet"], dtype=object)
    df = _select_columns(raw, cfg["columns"], path.name)
    df["currency"] = df["currency"].astype(str).str.strip().str.upper()

    dates = pd.to_datetime(df["rate_date"]).dt.date.unique()
    if len(dates) != 1 or dates[0] != as_of:
        raise SourceFileError(f"{path.name}: rate dates {list(dates)}, expected {as_of}")
    if df["currency"].duplicated().any():
        raise SourceFileError(f"{path.name}: duplicate currencies "
                              f"{df.loc[df.currency.duplicated(), 'currency'].tolist()}")
    rates = to_number(df["units_per_usd"]).set_axis(df["currency"]).rename("units_per_usd")
    if rates.isna().any() or (rates <= 0).any():
        raise SourceFileError(f"{path.name}: missing or non-positive rates "
                              f"{rates[rates.isna() | (rates <= 0)].index.tolist()}")
    return rates


def load_all(as_of: date) -> dict[str, pd.DataFrame | pd.Series]:
    return {
        "custodian": load_custodian(as_of),
        "accounting": load_accounting(as_of),
        "fx": load_fx(as_of),
    }
