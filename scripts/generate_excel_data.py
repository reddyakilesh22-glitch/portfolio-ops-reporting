"""Generate synthetic custodian, fund accounting, and FX workbooks.

For each business date this writes three files to data/raw/<date>/:

    custodian_holdings_<yyyymmdd>.xlsx   bank-style statement, one sheet per
                                         fund account, deliberately messy
    fund_accounting_positions_<yyyymmdd>.xlsx   accounting system extract
    fx_rates_<yyyymmdd>.xlsx             4pm closing rates (units per 1 USD)

and an answer key of the breaks planted between custodian and accounting:

    data/expected/breaks_<yyyy-mm-dd>.csv

All securities, identifiers (ISIN-format with valid check digits), prices,
and positions are synthetic. Output is deterministic for a given seed.

    python -m scripts.generate_excel_data
"""

from __future__ import annotations

import random
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font

from opsreport.config import ROOT, load_yaml

RANDOM_SEED = 20261113
DATES = [date(2026, 11, 6), date(2026, 11, 13)]
RAW_DIR = ROOT / "data" / "raw"
EXPECTED_DIR = ROOT / "data" / "expected"
CUSTODIAN_NAME = "Harbor Trust Custody Services"  # fictional

# Units of currency per 1 USD on the first date.
FX_START = {"USD": 1.0, "EUR": 0.92, "GBP": 0.79, "CHF": 0.88, "DKK": 6.86,
            "JPY": 151.2, "SEK": 10.6, "AUD": 1.53, "MXN": 17.9, "BRL": 5.6,
            "IDR": 15650.0, "ZAR": 18.2}

COUNTRY = {"USD": "US", "EUR": "NL", "GBP": "GB", "CHF": "CH", "DKK": "DK",
           "JPY": "JP", "SEK": "SE", "AUD": "AU", "MXN": "MX", "BRL": "BR",
           "IDR": "ID", "ZAR": "ZA"}

# fund code -> [(ticker, description, asset class, currency, start price)]
# Bond prices are per 100 par.
UNIVERSE = {
    "GEF": [
        ("AAPL", "Apple Inc", "Equity", "USD", 228.0),
        ("MSFT", "Microsoft Corp", "Equity", "USD", 415.0),
        ("NVDA", "NVIDIA Corp", "Equity", "USD", 140.0),
        ("AMZN", "Amazon.com Inc", "Equity", "USD", 190.0),
        ("JPM", "JPMorgan Chase & Co", "Equity", "USD", 215.0),
        ("TSM", "Taiwan Semiconductor ADR", "Equity", "USD", 190.0),
        ("NESN", "Nestle SA", "Equity", "CHF", 82.0),
        ("ROG", "Roche Holding AG", "Equity", "CHF", 265.0),
        ("ASML", "ASML Holding NV", "Equity", "EUR", 690.0),
        ("SAP", "SAP SE", "Equity", "EUR", 215.0),
        ("MC", "LVMH Moet Hennessy", "Equity", "EUR", 640.0),
        ("NOVO-B", "Novo Nordisk A/S B", "Equity", "DKK", 780.0),
        ("AZN", "AstraZeneca PLC", "Equity", "GBP", 108.0),
        ("SHEL", "Shell PLC", "Equity", "GBP", 26.0),
        ("ULVR", "Unilever PLC", "Equity", "GBP", 47.0),
        ("7203", "Toyota Motor Corp", "Equity", "JPY", 2700.0),
        ("6758", "Sony Group Corp", "Equity", "JPY", 3100.0),
    ],
    "USCB": [
        ("UST 4.25 2034", "US Treasury 4.25% 15-Nov-2034", "Govt Bond", "USD", 98.6),
        ("UST 3.875 2029", "US Treasury 3.875% 30-Sep-2029", "Govt Bond", "USD", 99.1),
        ("UST 4.625 2054", "US Treasury 4.625% 15-May-2054", "Govt Bond", "USD", 97.4),
        ("UST 4.0 2031", "US Treasury 4.0% 31-Jan-2031", "Govt Bond", "USD", 98.9),
        ("FNMA 5.5 2053", "FNMA 30Y Pool 5.5% 2053", "MBS", "USD", 99.8),
        ("FNMA 6.0 2054", "FNMA 30Y Pool 6.0% 2054", "MBS", "USD", 101.2),
        ("FHLMC 5.0 2052", "FHLMC 30Y Pool 5.0% 2052", "MBS", "USD", 97.9),
        ("JPM 5.35 2034", "JPMorgan Chase 5.35% 2034", "Corp Bond", "USD", 101.5),
        ("AAPL 4.3 2033", "Apple Inc 4.3% 2033", "Corp Bond", "USD", 99.4),
        ("VZ 4.5 2033", "Verizon Comms 4.5% 2033", "Corp Bond", "USD", 97.2),
        ("MS 5.25 2034", "Morgan Stanley 5.25% 2034", "Corp Bond", "USD", 100.6),
        ("CMCSA 4.65 2033", "Comcast Corp 4.65% 2033", "Corp Bond", "USD", 98.3),
    ],
    "EMD": [
        ("MEX 6.35 2035", "United Mexican States 6.35% 2035", "Sovereign", "USD", 100.4),
        ("BRAZIL 6.0 2033", "Federative Rep of Brazil 6.0% 2033", "Sovereign", "USD", 98.7),
        ("INDON 4.65 2032", "Republic of Indonesia 4.65% 2032", "Sovereign", "USD", 98.1),
        ("COLOM 7.5 2034", "Republic of Colombia 7.5% 2034", "Sovereign", "USD", 101.9),
        ("CHILE 4.85 2029", "Republic of Chile 4.85% 2029", "Sovereign", "USD", 100.2),
        ("MBONO 7.75 2034", "Mexican Bonos 7.75% 2034", "Local Sovereign", "MXN", 92.5),
        ("NTN-F 10 2033", "Brazil NTN-F 10% 2033", "Local Sovereign", "BRL", 88.4),
        ("NTN-F 10 2031", "Brazil NTN-F 10% 2031", "Local Sovereign", "BRL", 91.7),
        ("INDOGB 7.0 2033", "Indonesia Govt 7.0% 2033", "Local Sovereign", "IDR", 99.3),
        ("R2035", "South Africa 8.875% 2035", "Local Sovereign", "ZAR", 96.8),
    ],
    "ISC": [
        ("DSY", "Dassault Systemes SE", "Equity", "EUR", 33.0),
        ("SHL", "Siemens Healthineers AG", "Equity", "EUR", 56.0),
        ("ENX", "Euronext NV", "Equity", "EUR", 95.0),
        ("SOI", "Soitec SA", "Equity", "EUR", 105.0),
        ("LOGN", "Logitech International SA", "Equity", "CHF", 78.0),
        ("BCVN", "Banque Cantonale Vaudoise", "Equity", "CHF", 92.0),
        ("WISE", "Wise PLC", "Equity", "GBP", 8.9),
        ("JD", "JD Sports Fashion PLC", "Equity", "GBP", 1.05),
        ("AUTO", "Auto Trader Group PLC", "Equity", "GBP", 7.8),
        ("VIT-B", "Vitrolife AB B", "Equity", "SEK", 265.0),
        ("ALQ", "ALS Ltd", "Equity", "AUD", 15.0),
        ("6146", "Disco Corp", "Equity", "JPY", 41000.0),
        ("4385", "Mercari Inc", "Equity", "JPY", 1900.0),
    ],
    "MAI": [
        ("SPY", "SPDR S&P 500 ETF Trust", "ETF", "USD", 590.0),
        ("AGG", "iShares Core US Aggregate Bond ETF", "ETF", "USD", 98.0),
        ("LQD", "iShares iBoxx IG Corp Bond ETF", "ETF", "USD", 108.0),
        ("HYG", "iShares iBoxx High Yield Corp Bond ETF", "ETF", "USD", 79.0),
        ("VNQ", "Vanguard Real Estate ETF", "ETF", "USD", 92.0),
        ("EFA", "iShares MSCI EAFE ETF", "ETF", "USD", 80.0),
        ("EMB", "iShares JPM USD EM Bond ETF", "ETF", "USD", 91.0),
        ("TIP", "iShares TIPS Bond ETF", "ETF", "USD", 109.0),
        ("GLD", "SPDR Gold Shares", "ETF", "USD", 245.0),
        ("IEF", "iShares 7-10 Year Treasury Bond ETF", "ETF", "USD", 93.0),
    ],
}
BOND_CLASSES = {"Govt Bond", "MBS", "Corp Bond", "Sovereign", "Local Sovereign"}
CASH_USD = {"GEF": 4_200_000, "USCB": 2_750_000, "EMD": 1_900_000,
            "ISC": 1_350_000, "MAI": 3_100_000}


# ------------------------------------------------------------------ helpers
def isin_check_digit(body: str) -> int:
    """ISIN check digit (letters -> 10..35, then Luhn over the digit string)."""
    digits = "".join(str(int(c, 36)) for c in body)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch) * (2 if i % 2 == 0 else 1)
        total += d - 9 if d > 9 else d
    return (10 - total % 10) % 10


def make_isin(rng: random.Random, country: str) -> str:
    nsin = "".join(rng.choice("0123456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(9))
    body = country + nsin
    return body + str(isin_check_digit(body))


@dataclass
class Position:
    fund: str
    isin: str
    ticker: str
    description: str
    asset_class: str
    currency: str
    quantity: float
    price: float

    @property
    def is_bond(self) -> bool:
        return self.asset_class in BOND_CLASSES

    @property
    def is_cash(self) -> bool:
        return self.asset_class == "Cash"

    def local_mv(self) -> float:
        if self.is_cash:
            return round(self.quantity, 2)
        factor = 0.01 if self.is_bond else 1.0
        return round(self.quantity * self.price * factor, 2)


def build_book(rng: random.Random) -> list[Position]:
    """Starting positions for every fund (first business date)."""
    book = []
    for fund, secs in UNIVERSE.items():
        for ticker, desc, cls, ccy, px in secs:
            if cls in BOND_CLASSES:
                # Par sized so each line is roughly $1-12M in USD terms.
                usd_target = rng.uniform(1e6, 12e6)
                par = usd_target * FX_START[ccy] / (px / 100)
                qty = round(par, -5) if ccy != "IDR" else round(par, -9)
            else:
                usd_target = rng.uniform(0.8e6, 9e6)
                qty = max(100, round(usd_target * FX_START[ccy] / px, -2))
            book.append(Position(fund, make_isin(rng, COUNTRY[ccy]), ticker, desc,
                                 cls, ccy, float(qty), px))
        book.append(Position(fund, f"CASH-USD-{fund}", "USD", "US Dollar Cash",
                             "Cash", "USD", float(CASH_USD[fund]), 1.0))
    return book


def roll_forward(rng: random.Random, book: list[Position]) -> list[Position]:
    """Next week's book: prices drift, a handful of trades settle."""
    nxt = []
    for p in book:
        q = replace(p)
        if not p.is_cash:
            vol = 0.01 if p.is_bond else 0.03
            q.price = round(p.price * (1 + rng.gauss(0, vol)), 4)
        nxt.append(q)
    traded = rng.sample([p for p in nxt if not p.is_cash], 5)
    for p in traded:
        step = 100_000 if p.is_bond else 100
        p.quantity = float(max(step, round(p.quantity * rng.uniform(0.85, 1.2) / step) * step))
    for p in nxt:
        if p.is_cash:
            p.quantity = round(p.quantity * rng.uniform(0.9, 1.1), 2)
    return nxt


def fx_for(rng: random.Random, prev: dict[str, float] | None) -> dict[str, float]:
    base = prev or FX_START
    if prev is None:
        return dict(base)
    return {c: (1.0 if c == "USD" else round(r * (1 + rng.gauss(0, 0.008)), 6))
            for c, r in base.items()}


# ----------------------------------------------------------- plant breaks
@dataclass
class Planted:
    fund: str
    isin: str
    ticker: str
    break_type: str
    note: str


def plant_breaks(rng: random.Random, custodian: list[Position], accounting: list[Position],
                 acct_fx: dict[str, float], as_of: date) -> list[Planted]:
    """Mutate custodian/accounting copies in place; return the answer key."""
    key: list[Planted] = []
    used = {"LOGN"}  # reserved for the static-data break

    def pick(fund: str, pred=lambda p: True) -> int:
        idx = [i for i, p in enumerate(accounting)
               if p.fund == fund and not p.is_cash and p.ticker not in used and pred(p)]
        i = rng.choice(idx)
        used.add(accounting[i].ticker)
        return i

    def find(book: list[Position], isin: str) -> Position:
        return next(p for p in book if p.isin == isin)

    def add(p: Position, btype: str, note: str) -> None:
        key.append(Planted(p.fund, p.isin, p.ticker, btype, note))

    # Quantity: trade booked in accounting, not yet settled at custodian.
    for fund in (["GEF"] if as_of == DATES[0] else ["GEF", "USCB"]):
        a = accounting[pick(fund)]
        step = 100_000 if a.is_bond else 100
        delta = round(a.quantity * 0.12 / step) * step or step
        a.quantity += delta
        add(a, "Quantity Break", f"Unsettled buy of {delta:,.0f} booked in accounting")

    # Price: accounting priced from a different vendor.
    funds = ["MAI"] if as_of == DATES[0] else ["ISC", "USCB"]
    for fund in funds:
        a = accounting[pick(fund)]
        bump = 0.035 if not a.is_bond else 0.012
        a.price = round(a.price * (1 + bump), 4)
        add(a, "Price Break", f"Accounting price {bump:.1%} above custodian (vendor diff)")

    # Missing at custodian: trade booked, settlement failed.
    fund = "MAI" if as_of == DATES[0] else "GEF"
    a = accounting[pick(fund)]
    custodian.remove(find(custodian, a.isin))
    add(a, "Missing Trade", "Position in accounting only; settlement failed at custodian")

    if as_of == DATES[1]:
        # Missing in accounting: custodian received a position never booked.
        c = find(custodian, accounting[pick("ISC")].isin)
        accounting.remove(find(accounting, c.isin))
        add(c, "Missing Trade", "Position at custodian only; trade not booked in accounting")

        # Corporate action: 2-for-1 split processed by custodian only.
        c = find(custodian, accounting[pick("GEF", lambda p: not p.is_bond)].isin)
        c.quantity *= 2
        c.price = round(c.price / 2, 4)
        add(c, "Corporate Action", "2:1 stock split applied at custodian, not in accounting")

        # FX: accounting used a stale BRL rate -> every BRL line breaks in base.
        acct_fx["BRL"] = round(acct_fx["BRL"] * 0.97, 6)
        for a in accounting:
            if a.currency == "BRL":
                add(a, "FX Rate", "Accounting BRL rate stale (prior-day fixing)")

        # Cash: coupon received at custodian, not yet booked in accounting.
        c = find(custodian, "CASH-USD-USCB")
        c.quantity = round(c.quantity + 48_250.00, 2)
        add(c, "Cash Break", "Coupon receipt $48,250 at custodian not booked in accounting")

        # Static data: wrong currency on the security master in accounting.
        a = next(p for p in accounting if p.ticker == "LOGN")
        a.currency = "EUR"
        add(a, "Static Data", "Accounting security master has EUR; trades in CHF")

    return key


def add_rounding_noise(rng: random.Random, accounting: list[Position]) -> None:
    """Sub-tolerance differences the reconciliation must NOT flag."""
    for a in accounting:
        if not a.is_cash and rng.random() < 0.3:
            a.price = round(a.price * (1 + rng.uniform(-5e-7, 5e-7)), 6)


# ------------------------------------------------------------------ writers
def write_custodian(path: Path, book: list[Position], fx: dict[str, float],
                    as_of: date, funds: list[dict], rng: random.Random) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    header = ["Account", "ISIN", "Security Description", "Ticker", "Asset Type", "Ccy",
              "Shares/Par Value", "Local Price", "Local Market Value", "FX Rate",
              "Base Market Value (USD)"]
    for f in funds:
        ws = wb.create_sheet(f["custodian_sheet"])
        ws.append([CUSTODIAN_NAME])
        ws.append([f"Holdings Statement - Account {f['custodian_account']}"])
        ws.append([f"As of: {as_of:%d-%b-%Y}"])
        ws.append([f"Generated: {as_of:%Y-%m-%d} 22:15 ET"])
        ws.append([])
        ws.append(header)
        ws["A1"].font = Font(bold=True, size=13)
        for cell in ws[6]:
            cell.font = Font(bold=True)

        total = 0.0
        for p in sorted((p for p in book if p.fund == f["code"]),
                        key=lambda p: (p.is_cash, p.ticker)):
            local = p.local_mv()
            rate = fx[p.currency]
            base = round(local / rate, 2)
            total += base
            qty: float | str = p.quantity
            if rng.random() < 0.25:  # numbers exported as text
                qty = f"{p.quantity:,.2f}"
            isin = p.isin + ("  " if rng.random() < 0.15 else "")
            ccy = p.currency.lower() if rng.random() < 0.1 else p.currency
            ws.append([f["custodian_account"], isin, p.description, p.ticker,
                       p.asset_class, ccy, qty, None if p.is_cash else p.price,
                       local, rate, base])
        ws.append([])
        ws.append(["Total", None, None, None, None, None, None, None, None, None,
                   round(total, 2)])
        ws.append([])
        ws.append(["*** End of Report ***"])
        ws.append(["Values are indicative and subject to final pricing. "
                   "Synthetic data for demonstration only."])
        for col, width in zip("ABCDEFGHIJK", [14, 16, 36, 16, 16, 6, 18, 12, 18, 10, 22],
                              strict=True):
            ws.column_dimensions[col].width = width
    wb.save(path)


def write_accounting(path: Path, book: list[Position], fx: dict[str, float],
                     as_of: date, funds: list[dict], rng: random.Random) -> None:
    names = {f["code"]: f["name"] for f in funds}
    rows = []
    for p in book:
        local = p.local_mv()
        rate = fx[p.currency]
        accrued = 0.0
        if p.is_bond:
            coupon = float(p.ticker.split()[1]) if len(p.ticker.split()) > 1 else 8.875
            accrued = round(local / rate * coupon / 100 * rng.uniform(0.05, 0.45), 2)
        rows.append({
            "Fund Code": p.fund,
            "Fund Name": names[p.fund],
            "Security ID": p.isin,
            "Ticker": p.ticker,
            "Description": p.description,
            "Asset Class": p.asset_class,
            "Local Ccy": p.currency,
            "Quantity": p.quantity,
            "Price (Local)": None if p.is_cash else p.price,
            "MV Local": local,
            "FX Rate": rate,
            "MV Base": round(local / rate, 2),
            "Accrued Income Base": accrued,
            "Pricing Source": "Cash" if p.is_cash else rng.choice(["VendorA", "VendorA",
                                                                   "VendorB"]),
            "Position Date": as_of,
        })
    df = pd.DataFrame(rows).sort_values(["Fund Code", "Ticker"])
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Positions", index=False)


def write_fx(path: Path, fx: dict[str, float], as_of: date) -> None:
    df = pd.DataFrame({"Currency": list(fx), "Units per USD": list(fx.values()),
                       "Rate Date": as_of, "Source": "London 4pm close"})
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Rates", index=False)


# --------------------------------------------------------------------- main
def generate(out_raw: Path = RAW_DIR, out_expected: Path = EXPECTED_DIR,
             seed: int = RANDOM_SEED) -> dict[date, list[Planted]]:
    rng = random.Random(seed)
    funds = load_yaml("funds.yaml")["funds"]
    book = build_book(rng)
    fx = fx_for(rng, None)
    answer = {}
    for i, as_of in enumerate(DATES):
        if i:
            book = roll_forward(rng, book)
            fx = fx_for(rng, fx)
        custodian, accounting = deepcopy(book), deepcopy(book)
        acct_fx = dict(fx)
        key = plant_breaks(rng, custodian, accounting, acct_fx, as_of)
        add_rounding_noise(rng, accounting)

        day = out_raw / as_of.isoformat()
        day.mkdir(parents=True, exist_ok=True)
        stamp = f"{as_of:%Y%m%d}"
        write_custodian(day / f"custodian_holdings_{stamp}.xlsx", custodian, fx, as_of,
                        funds, rng)
        write_accounting(day / f"fund_accounting_positions_{stamp}.xlsx", accounting,
                         acct_fx, as_of, funds, rng)
        write_fx(day / f"fx_rates_{stamp}.xlsx", fx, as_of)

        out_expected.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([k.__dict__ for k in key]).to_csv(
            out_expected / f"breaks_{as_of.isoformat()}.csv", index=False)
        answer[as_of] = key
    return answer


def main() -> None:
    answer = generate()
    for d, key in answer.items():
        print(f"{d}: {len(key)} planted breaks -> data/raw/{d}/")
        for k in key:
            print(f"   {k.fund:5} {k.ticker:16} {k.break_type:17} {k.note}")
    print(f"Generated at {datetime.now():%H:%M:%S}")


if __name__ == "__main__":
    main()
