from datetime import date

import pandas as pd
import pytest

from opsreport.config import ROOT
from opsreport.extract.excel_loader import load_all
from opsreport.reconcile.positions import reconcile
from opsreport.validate.checks import check_positions, isin_is_valid

DATES = [date(2026, 11, 6), date(2026, 11, 13)]


@pytest.fixture(scope="module")
def loaded():
    return {d: load_all(d) for d in DATES}


@pytest.mark.parametrize("as_of", DATES)
def test_recon_matches_answer_key_exactly(loaded, as_of):
    x = loaded[as_of]
    breaks = reconcile(x["custodian"], x["accounting"])
    expected = pd.read_csv(ROOT / "data" / "expected" / f"breaks_{as_of}.csv")
    got = set(zip(breaks["fund_code"], breaks["isin"], breaks["break_type"], strict=True))
    want = set(zip(expected["fund"], expected["isin"], expected["break_type"], strict=True))
    assert got - want == set(), "false positives"
    assert want - got == set(), "missed breaks"


def test_rounding_noise_is_not_a_break(loaded):
    x = loaded[DATES[1]]
    breaks = reconcile(x["custodian"], x["accounting"])
    m = x["custodian"].merge(x["accounting"], on=["fund_code", "isin"],
                             suffixes=("_c", "_a"))
    same_qty = (m["quantity_c"] == m["quantity_a"]) & (m["currency_c"] == m["currency_a"])
    noisy = m[same_qty & (m["base_mv_c"] - m["base_mv_a"]).abs().between(0.001, 25)]
    assert len(noisy) > 0, "fixture should contain sub-tolerance noise"
    assert not noisy["isin"].isin(breaks["isin"]).any()


def _pos(**kw):
    base = dict(fund_code="F1", isin="US0378331005", ticker="T", description="d",
                asset_class="Equity", currency="USD", quantity=100.0, price=10.0,
                local_mv=1000.0, fx_rate=1.0, base_mv=1000.0)
    return {**base, **kw}


def _recon(cust_rows, acct_rows):
    return reconcile(pd.DataFrame(cust_rows), pd.DataFrame(acct_rows))


def test_split_classified_as_corporate_action():
    b = _recon([_pos(quantity=300.0, price=10 / 3, local_mv=1000.0)], [_pos()])
    assert b.loc[0, "break_type"] == "Corporate Action"
    assert "3:1" in b.loc[0, "likely_cause"]


def test_quantity_wins_over_price_when_both_differ():
    b = _recon([_pos(quantity=120.0, local_mv=1200.0, base_mv=1200.0)], [_pos()])
    assert b.loc[0, "break_type"] == "Quantity Break"


def test_fx_break_when_local_agrees():
    b = _recon([_pos(currency="BRL", fx_rate=5.5, local_mv=55000.0, base_mv=10000.0)],
               [_pos(currency="BRL", fx_rate=5.0, local_mv=55000.0, base_mv=11000.0)])
    assert b.loc[0, "break_type"] == "FX Rate"


def test_identical_books_have_no_breaks():
    assert _recon([_pos()], [_pos()]).empty


def test_duplicate_keys_are_rejected():
    with pytest.raises(pd.errors.MergeError):
        _recon([_pos(), _pos()], [_pos()])


# ------------------------------------------------------------------ checks
def test_isin_validation():
    assert isin_is_valid("US0378331005")
    assert not isin_is_valid("US0378331006")
    assert not isin_is_valid("US037833100")


def test_stale_fx_rate_flagged(loaded):
    x = loaded[DATES[1]]
    issues = check_positions(x["accounting"], x["fx"])
    fx = [i for i in issues if i.check == "fx_rate_vs_official"]
    assert len(fx) == 1 and "BRL" in fx[0].detail and "-3.00%" in fx[0].detail


def test_checks_flag_bad_isin_and_duplicates():
    fx = pd.Series({"USD": 1.0})
    df = pd.DataFrame([_pos(isin="US0378331006"), _pos(isin="US0378331006")])
    df["source"] = "test"
    checks = {i.check for i in check_positions(df, fx)}
    assert {"isin_check_digit", "duplicate_position"} <= checks
