from datetime import date

import pandas as pd
import pytest
from openpyxl import load_workbook

from opsreport.extract.excel_loader import (
    SourceFileError,
    load_accounting,
    load_custodian,
    load_fx,
    to_number,
)
from scripts.generate_excel_data import generate, isin_check_digit

AS_OF = date(2026, 11, 13)


@pytest.fixture(scope="module")
def raw(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    generate(root / "raw", root / "expected")
    return root / "raw"


def files(raw, as_of=AS_OF):
    d = raw / as_of.isoformat()
    s = f"{as_of:%Y%m%d}"
    return (d / f"custodian_holdings_{s}.xlsx",
            d / f"fund_accounting_positions_{s}.xlsx",
            d / f"fx_rates_{s}.xlsx")


def edit_copy(src, tmp_path, fn):
    wb = load_workbook(src)
    fn(wb)
    out = tmp_path / src.name
    wb.save(out)
    return out


# ------------------------------------------------------------- to_number
@pytest.mark.parametrize("raw_value, expected", [
    ("1,234.50", 1234.5), ("(1,234.50)", -1234.5), (" 12 ", 12.0),
    ("$5,000", 5000.0), (7, 7.0),
])
def test_to_number_handles_exported_formats(raw_value, expected):
    assert to_number(pd.Series([raw_value], dtype=object)).iloc[0] == expected


def test_to_number_blanks_become_nan():
    out = to_number(pd.Series(["", "-", None], dtype=object))
    assert out.isna().all()


def test_to_number_rejects_garbage():
    with pytest.raises(ValueError):
        to_number(pd.Series(["12.5x"], dtype=object))


def test_generated_isins_have_valid_check_digits():
    assert isin_check_digit("US037833100") == 5  # Apple's real ISIN: US0378331005


# ------------------------------------------------------------- custodian
def test_custodian_loads_and_cleans(raw):
    c, _, _ = files(raw)
    df = load_custodian(AS_OF, c)
    assert set(df["fund_code"]) == {"GEF", "USCB", "EMD", "ISC", "MAI"}
    assert df["quantity"].dtype == "float64"
    assert not df["isin"].str.contains(" ").any()
    assert df["currency"].str.isupper().all()
    assert not df["isin"].str.contains("Total|End of Report", case=False).any()


def test_custodian_survives_inserted_column(raw, tmp_path):
    c, _, _ = files(raw)

    def insert(wb):
        for ws in wb.worksheets:
            ws.insert_cols(3)
            ws.cell(row=6, column=3, value="Sedol")
    df = load_custodian(AS_OF, edit_copy(c, tmp_path, insert))
    assert df["base_mv"].sum() == pytest.approx(load_custodian(AS_OF, c)["base_mv"].sum())


def test_custodian_renamed_column_fails_loudly(raw, tmp_path):
    c, _, _ = files(raw)

    def rename(wb):
        wb.worksheets[0].cell(row=6, column=11, value="Market Value (Base)")
    with pytest.raises(SourceFileError, match="Base Market Value"):
        load_custodian(AS_OF, edit_copy(c, tmp_path, rename))


def test_custodian_wrong_date_fails(raw):
    c, _, _ = files(raw)
    with pytest.raises(SourceFileError, match="expected 2026-11-06"):
        load_custodian(date(2026, 11, 6), c)


def test_custodian_control_total_catches_dropped_row(raw, tmp_path):
    c, _, _ = files(raw)

    def blank_account(wb):
        wb.worksheets[0].cell(row=7, column=1).value = None  # row no longer matches account
    with pytest.raises(SourceFileError, match="statement total"):
        load_custodian(AS_OF, edit_copy(c, tmp_path, blank_account))


def test_custodian_header_moved_down_still_found(raw, tmp_path):
    c, _, _ = files(raw)

    def push_down(wb):
        for ws in wb.worksheets:
            ws.insert_rows(5, amount=3)
    df = load_custodian(AS_OF, edit_copy(c, tmp_path, push_down))
    assert len(df) == len(load_custodian(AS_OF, c))


# ------------------------------------------------------------ accounting / fx
def test_accounting_loads(raw):
    _, a, _ = files(raw)
    df = load_accounting(AS_OF, a)
    assert {"accrued_base", "pricing_source"} <= set(df.columns)
    assert (df["accrued_base"] >= 0).all()


def test_accounting_wrong_date_fails(raw):
    _, a, _ = files(raw)
    with pytest.raises(SourceFileError, match="position dates"):
        load_accounting(date(2026, 11, 6), a)


def test_fx_loads_and_usd_is_one(raw):
    _, _, f = files(raw)
    rates = load_fx(AS_OF, f)
    assert rates["USD"] == 1.0
    assert len(rates) == 12


def test_fx_missing_rate_fails(raw, tmp_path):
    _, _, f = files(raw)

    def blank_brl(wb):
        ws = wb["Rates"]
        for row in ws.iter_rows(min_row=2):
            if row[0].value == "BRL":
                row[1].value = None
    with pytest.raises(SourceFileError, match="BRL"):
        load_fx(AS_OF, edit_copy(f, tmp_path, blank_brl))
