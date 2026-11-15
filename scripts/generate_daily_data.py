"""Generate about six months of daily synthetic inputs for the daily run.

For every business day from START to END this writes, to data/daily/<date>/:

    custodian_holdings_<yyyymmdd>.xlsx        same layouts as the weekly files
    fund_accounting_positions_<yyyymmdd>.xlsx
    fx_rates_<yyyymmdd>.xlsx
    pending_trades_<yyyymmdd>.xlsx            trades booked but not yet settled

plus, for checking the pipeline:

    data/daily/expected/breaks_<date>.csv     answer key: every break that day
    data/daily/jira_snapshot.json             one ticket per actionable break

How the books move
------------------
The custodian holds what has *settled*. Fund accounting books a trade on its
*trade date*. So a trade shows as a quantity break from trade date until the
custodian receives it (T+1 for USD securities, T+2 otherwise). While the
contractual settlement date is still ahead, that break is **Expected**. Some
settlements fail and arrive late; from the contractual date on, the break is
**Actionable** and gets a ticket.

Other breaks are episodes that start, last a few business days and clear:
price (one vendor is the usual culprit), cash, stale FX rate, unprocessed
split, wrong currency on the security master, position missing at custodian.
A position has at most one break at a time, with at least one clean day
between breaks, so each break keeps one ID from first appearance to clearing.

The story in the data: from STRESS_FROM, ops falls behind (fixes take longer,
more failed settlements, more vendor price errors), and the last two business
days of each month carry rebalancing volume.

Output is deterministic for a given seed. It is not committed: run

    python -m scripts.generate_daily_data
"""

from __future__ import annotations

import argparse
import json
import math
import random
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from opsreport.calendar import holidays
from opsreport.config import ROOT, load_yaml
from scripts.generate_excel_data import (
    FX_START,
    V2_FROM,
    Position,
    add_rounding_noise,
    build_book,
    write_accounting,
    write_custodian,
    write_fx,
)
from scripts.seed_jira import SLA_DAYS, priority_for

SEED = 20260518
START, END = date(2026, 5, 18), date(2026, 11, 13)
STRESS_FROM = date(2026, 10, 1)
DAILY_DIR = ROOT / "data" / "daily"
SNAPSHOT_NAME = "jira_snapshot.json"
PROJECT_KEY = "OPSREC"
VENDOR_B_PRONE = ["NOVO-B", "ROG", "ENX", "SOI", "BCVN", "MBONO 7.75 2034", "R2035",
                  "HYG"]   # securities whose secondary vendor price is often off

# break type -> owning ops team
TEAM = {"Quantity Break": "Trade Support", "Missing Trade": "Trade Support",
        "Price Break": "NAV Oversight", "FX Rate": "NAV Oversight",
        "Cash Break": "Reconciliation", "Corporate Action": "Corporate Actions",
        "Static Data": "Reference Data"}

# Events per business day: (normal, from STRESS_FROM)
RATES = {"trade": (2.2, 2.6), "Price Break": (0.5, 1.0), "Cash Break": (0.2, 0.25),
         "FX Rate": (0.06, 0.08), "Corporate Action": (0.025, 0.025),
         "Static Data": (0.012, 0.012), "Missing Trade": (0.015, 0.02)}
FAIL_RATE = (0.10, 0.17)
SLOWDOWN = (1.0, 1.8)          # multiplier on how long a break takes to fix
STUCK_RATE = (0.06, 0.14)      # share of breaks that get stuck for weeks


# ---------------------------------------------------------------- calendar
def _hol() -> np.ndarray:
    return holidays()


def business_days(start: date, end: date) -> list[date]:
    hol = [pd.Timestamp(h).date() for h in _hol()]
    return list(pd.bdate_range(start, end, freq="C", holidays=hol).date)


def add_bdays(d: date, k: int) -> date:
    return pd.Timestamp(np.busday_offset(np.datetime64(d), k, roll="forward",
                                         holidays=_hol())).date()


def is_month_end_window(d: date) -> bool:
    """Last two business days of the month (rebalancing volume)."""
    return add_bdays(d, 2).month != d.month


# ------------------------------------------------------------------ model
@dataclass
class Trade:
    trade_id: str
    fund: str
    isin: str
    ticker: str
    qty: float              # signed: + buy, - sell
    trade_date: date
    settle_date: date       # contractual
    actual_settle: date     # when the custodian really receives it


@dataclass
class Episode:
    kind: str               # break type
    fund: str
    isin: str
    ticker: str
    start: date             # first day the break shows
    clear: date             # first day it no longer shows
    params: dict = field(default_factory=dict)

    @property
    def break_id(self) -> str:
        return break_id(self.start, self.fund, self.isin)


def break_id(first_seen: date, fund: str, isin: str) -> str:
    return f"BRK-{first_seen:%Y%m%d}-{fund}-{isin}"


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


@dataclass
class DayResult:
    as_of: date
    key: list[dict]          # answer-key rows for this day


class Simulator:
    def __init__(self, seed: int = SEED, start: date = START, end: date = END):
        self.rng = random.Random(seed)
        self.seed = seed
        self.days = business_days(start, end)
        self.end = end
        self.funds = load_yaml("funds.yaml")["funds"]
        self.book: list[Position] = build_book(self.rng)
        self.fx: dict[str, float] = dict(FX_START)
        self.prev_fx: dict[str, float] | None = None
        self.trades: list[Trade] = []
        self.episodes: list[Episode] = []
        self.locked_until: dict[tuple[str, str], date] = {}
        self.source = {p.isin: ("VendorB" if p.ticker in VENDOR_B_PRONE else
                                self.rng.choice(["VendorA", "VendorA", "VendorA", "VendorB"]))
                       for p in self.book if not p.is_cash}
        self.n_trades = 0

    # ------------------------------------------------------------ helpers
    def pos(self, fund: str, isin: str) -> Position:
        return next(p for p in self.book if p.fund == fund and p.isin == isin)

    def free(self, p: Position, d: date) -> bool:
        return self.locked_until.get((p.fund, p.isin), date.min) < d

    def lock(self, p: Position, until: date) -> None:
        self.locked_until[(p.fund, p.isin)] = until

    def stressed(self, d: date) -> int:
        return int(d >= STRESS_FROM)

    def duration(self, d: date, mean: float, minimum: int = 1) -> int:
        """Business days until fixed: usually quick, with a long tail of breaks
        stuck waiting on a counterparty or an escalation."""
        s = self.stressed(d)
        days = minimum + int(self.rng.expovariate(1 / mean) * SLOWDOWN[s])
        if self.rng.random() < STUCK_RATE[s]:
            days += self.rng.randint(5, 30)
        return days

    # --------------------------------------------------------------- day
    def drift(self) -> None:
        for p in self.book:
            if not p.is_cash:
                vol = 0.004 if p.is_bond else 0.012
                p.price = round(p.price * (1 + self.rng.gauss(0, vol)), 4)
        self.prev_fx = dict(self.fx)
        self.fx = {c: (1.0 if c == "USD" else round(r * (1 + self.rng.gauss(0, 0.003)), 6))
                   for c, r in self.fx.items()}

    def settle_and_clear(self, d: date) -> None:
        for t in self.trades:
            if t.actual_settle == d:
                self.pos(t.fund, t.isin).quantity += t.qty
        for e in self.episodes:
            if e.clear != d:
                continue
            p = self.pos(e.fund, e.isin)
            if e.kind == "Cash Break":       # accounting books the receipt
                p.quantity = round(p.quantity + e.params["amount"], 2)
            elif e.kind == "Corporate Action":   # accounting applies the split
                p.quantity *= e.params["ratio"]
                p.price = round(p.price / e.params["ratio"], 4)

    def new_trades(self, d: date, i: int) -> None:
        lam = RATES["trade"][self.stressed(d)] * (2.5 if is_month_end_window(d) else 1)
        for _ in range(_poisson(self.rng, lam)):
            cands = [p for p in self.book if not p.is_cash and self.free(p, d)]
            if not cands:
                return
            p = self.rng.choice(cands)
            step = 100_000 if p.is_bond else 100
            delta = max(step, round(p.quantity * self.rng.uniform(0.05, 0.2) / step) * step)
            qty = delta if self.rng.random() < 0.55 or p.quantity - delta <= 0 else -delta
            settle = add_bdays(d, 1 if p.currency == "USD" else 2)
            actual = settle
            if self.rng.random() < FAIL_RATE[self.stressed(d)]:
                actual = add_bdays(settle, self.duration(d, 2.5))
            self.n_trades += 1
            self.trades.append(Trade(f"T{d:%Y%m%d}-{self.n_trades:05d}", p.fund, p.isin,
                                     p.ticker, float(qty), d, settle, actual))
            self.lock(p, actual)

    def new_episodes(self, d: date) -> None:
        s = self.stressed(d)
        rng = self.rng

        def candidates(pred=lambda p: True) -> list[Position]:
            return [p for p in self.book if not p.is_cash and self.free(p, d) and pred(p)]

        def start(kind: str, p: Position, days: int, **params) -> None:
            clear = add_bdays(d, days)
            self.episodes.append(Episode(kind, p.fund, p.isin, p.ticker, d, clear, params))
            self.lock(p, clear)

        for _ in range(_poisson(rng, RATES["Price Break"][s])):
            prone = candidates(lambda p: p.ticker in VENDOR_B_PRONE)
            pool = prone if prone and rng.random() < 0.6 else candidates()
            if pool:
                p = rng.choice(pool)
                bump = rng.uniform(0.015, 0.04) if not p.is_bond else rng.uniform(0.005, 0.015)
                start("Price Break", p, self.duration(d, 1.5),
                      bump=bump * rng.choice([1, -1]))

        for _ in range(_poisson(rng, RATES["Cash Break"][s])):
            cash = [p for p in self.book if p.is_cash and self.free(p, d)]
            if cash:
                start("Cash Break", rng.choice(cash), self.duration(d, 1.0),
                      amount=round(rng.uniform(10_000, 300_000), 2))

        if self.prev_fx and rng.random() < RATES["FX Rate"][s]:
            ccy = rng.choice(["BRL", "BRL", "MXN", "ZAR", "IDR", "JPY", "GBP"])
            hit = [p for p in self.book if p.currency == ccy and not p.is_cash]
            if hit and all(self.free(p, d) for p in hit):
                stale = self.prev_fx[ccy]
                if abs(stale / self.fx[ccy] - 1) < 0.002:   # make the error visible
                    stale = round(self.fx[ccy] * 1.004, 6)
                days = 1 if rng.random() < 0.7 else 2
                for p in hit:
                    start("FX Rate", p, days, ccy=ccy, stale=stale)

        if rng.random() < RATES["Corporate Action"][s]:
            pool = candidates(lambda p: p.asset_class == "Equity")
            if pool:
                start("Corporate Action", rng.choice(pool), self.duration(d, 3.0, 2),
                      ratio=rng.choice([2.0, 2.0, 3.0]))

        if rng.random() < RATES["Static Data"][s]:
            pool = candidates(lambda p: p.asset_class == "Equity" and p.currency != "USD")
            if pool:
                p = rng.choice(pool)
                start("Static Data", p, self.duration(d, 5.0, 3),
                      wrong="EUR" if p.currency != "EUR" else "CHF")

        if rng.random() < RATES["Missing Trade"][s]:
            pool = candidates()
            if pool:
                start("Missing Trade", rng.choice(pool), self.duration(d, 4.0, 2))

    # ----------------------------------------------------------- views
    def views(self, d: date) -> tuple[list[Position], list[Position], dict[str, float]]:
        custodian, accounting = deepcopy(self.book), deepcopy(self.book)
        acct_fx = dict(self.fx)
        idx_c = {(p.fund, p.isin): p for p in custodian}
        idx_a = {(p.fund, p.isin): p for p in accounting}
        for t in self.trades:     # booked in accounting, not yet at custodian
            if t.trade_date <= d < t.actual_settle:
                idx_a[(t.fund, t.isin)].quantity += t.qty
        drop_c = set()
        for e in self.active(d):
            k = (e.fund, e.isin)
            if e.kind == "Price Break":
                idx_a[k].price = round(idx_a[k].price * (1 + e.params["bump"]), 4)
            elif e.kind == "Cash Break":
                idx_c[k].quantity = round(idx_c[k].quantity + e.params["amount"], 2)
            elif e.kind == "FX Rate":
                acct_fx[e.params["ccy"]] = e.params["stale"]
            elif e.kind == "Corporate Action":
                idx_c[k].quantity *= e.params["ratio"]
                idx_c[k].price = round(idx_c[k].price / e.params["ratio"], 4)
            elif e.kind == "Static Data":
                idx_a[k].currency = e.params["wrong"]
            elif e.kind == "Missing Trade":
                drop_c.add(k)
        custodian = [p for p in custodian if (p.fund, p.isin) not in drop_c]
        return custodian, accounting, acct_fx

    def active(self, d: date) -> list[Episode]:
        return [e for e in self.episodes if e.start <= d < e.clear]

    def open_trades(self, d: date) -> list[Trade]:
        return [t for t in self.trades if t.trade_date <= d < t.actual_settle]

    def answer_key(self, d: date, custodian: list[Position], accounting: list[Position],
                   acct_fx: dict[str, float]) -> list[dict]:
        cust = {(p.fund, p.isin): p for p in custodian}
        acct = {(p.fund, p.isin): p for p in accounting}

        def amounts(k: tuple[str, str], kind: str) -> tuple[float, float]:
            """(abs $ difference, exposure). For an unprocessed split the values
            agree, but the whole position carries the wrong share count."""
            a = acct.get(k)
            c = cust.get(k)
            a_base = a.local_mv() / acct_fx[a.currency] if a else 0.0
            c_base = c.local_mv() / self.fx[c.currency] if c else 0.0
            diff = abs(a_base - c_base)
            exposure = max(a_base, c_base) if kind == "Corporate Action" else diff
            return round(diff, 2), round(exposure, 2)

        rows = []
        for t in self.open_trades(d):
            k = (t.fund, t.isin)
            diff, exposure = amounts(k, "Quantity Break")
            rows.append(dict(fund=t.fund, isin=t.isin, ticker=t.ticker,
                             break_type="Quantity Break",
                             break_id=break_id(t.trade_date, t.fund, t.isin),
                             first_seen=t.trade_date,
                             status="Expected" if d < t.settle_date else "Actionable",
                             actionable_from=t.settle_date, abs_diff=diff, exposure=exposure,
                             note=f"trade {t.trade_id} settles {t.settle_date}"))
        for e in self.active(d):
            k = (e.fund, e.isin)
            diff, exposure = amounts(k, e.kind)
            rows.append(dict(fund=e.fund, isin=e.isin, ticker=e.ticker, break_type=e.kind,
                             break_id=e.break_id, first_seen=e.start, status="Actionable",
                             actionable_from=e.start, abs_diff=diff, exposure=exposure,
                             note=json.dumps(e.params, default=str)))
        return rows

    # ------------------------------------------------------------- run
    def run(self, out_dir: Path = DAILY_DIR, write: bool = True) -> list[DayResult]:
        results = []
        for i, d in enumerate(self.days):
            if i:
                self.drift()
                self.settle_and_clear(d)
            self.new_trades(d, i)
            self.new_episodes(d)
            custodian, accounting, acct_fx = self.views(d)
            add_rounding_noise(self.rng, accounting)
            key = self.answer_key(d, custodian, accounting, acct_fx)
            if write:
                self.write_day(out_dir, d, i, custodian, accounting, acct_fx, key)
            results.append(DayResult(d, key))
        if write:
            self.write_snapshot(out_dir, results)
        return results

    def write_day(self, out_dir: Path, d: date, i: int, custodian: list[Position],
                  accounting: list[Position], acct_fx: dict[str, float],
                  key: list[dict]) -> None:
        day = out_dir / d.isoformat()
        day.mkdir(parents=True, exist_ok=True)
        stamp = f"{d:%Y%m%d}"
        fmt_rng = random.Random(self.seed * 1000 + i)     # messy-format noise only
        lot_rng = random.Random(self.seed + i) if d >= V2_FROM else None
        write_custodian(day / f"custodian_holdings_{stamp}.xlsx", custodian, self.fx, d,
                        self.funds, fmt_rng, lot_rng)
        write_accounting(day / f"fund_accounting_positions_{stamp}.xlsx", accounting,
                         acct_fx, d, self.funds, fmt_rng, pricing_source=self.source)
        write_fx(day / f"fx_rates_{stamp}.xlsx", self.fx, d)
        write_pending(day / f"pending_trades_{stamp}.xlsx", self.open_trades(d), d)
        exp = out_dir / "expected"
        exp.mkdir(parents=True, exist_ok=True)
        cols = ["fund", "isin", "ticker", "break_type", "break_id", "first_seen", "status",
                "actionable_from", "abs_diff", "exposure", "note"]
        pd.DataFrame(key, columns=cols).to_csv(exp / f"breaks_{d.isoformat()}.csv",
                                               index=False)

    # ---------------------------------------------------------- tickets
    def tickets(self, results: list[DayResult]) -> list[dict]:
        """One ticket per break that was ever actionable."""
        first: dict[str, dict] = {}
        for r in results:
            for row in r.key:
                if row["status"] == "Actionable" and row["break_id"] not in first:
                    first[row["break_id"]] = {**row, "business_date": r.as_of}
        clears: dict[str, date] = {}
        for t in self.trades:
            clears[break_id(t.trade_date, t.fund, t.isin)] = t.actual_settle
        for e in self.episodes:
            clears[e.break_id] = e.clear

        rng = random.Random(self.seed + 7)
        names = {f["code"]: f["name"] for f in self.funds}
        out = []
        for row in sorted(first.values(), key=lambda r: (r["business_date"], r["break_id"])):
            biz = row["business_date"]
            amount = row["exposure"]
            prio = priority_for(amount)
            due = add_bdays(biz, SLA_DAYS[prio])
            clear = clears[row["break_id"]]
            resolved = clear if clear <= self.end else None
            out.append(dict(
                break_id=row["break_id"], fund=names[row["fund"]], ticker=row["ticker"],
                break_type=row["break_type"], ops_team=TEAM[row["break_type"]],
                amount=round(amount, 2), business_date=biz, priority=prio, due=due,
                resolved=resolved,
                status="Done" if resolved else rng.choice(
                    ["In Progress", "In Progress", "Selected for Development", "Backlog"])))
        # Realistic data-quality noise for the checks to catch.
        for t in rng.sample(out, k=max(1, len(out) // 40)):
            t["fund"] = None
        done = [t for t in out if t["status"] == "Done"]
        for t in rng.sample(done, k=min(2, len(done))):
            t["resolved"] = None
        return out

    def write_snapshot(self, out_dir: Path, results: list[DayResult]) -> Path:
        lock = load_yaml("jira_field_lock.yaml")["fields"]
        ids = {k: v["id"] for k, v in lock.items()}
        issues = []
        for n, t in enumerate(self.tickets(results), start=1):
            done = t["status"] == "Done"
            f = {
                "summary": f"{t['break_type']}: {t['ticker']} | {t['fund'] or '?'} | "
                           f"{t['business_date']}",
                "issuetype": {"name": "Task"},
                "status": {"name": t["status"], "statusCategory": {
                    "key": "done" if done else
                    ("new" if t["status"] == "Backlog" else "indeterminate")}},
                "priority": {"name": t["priority"]},
                "assignee": None,
                "labels": ["custodian-recon", "daily-sim", f"break-{t['break_id']}"],
                "duedate": t["due"].isoformat(),
                "created": f"{t['business_date']}T09:30:00.000-0400",
                "updated": f"{(t['resolved'] or t['business_date'])}T16:00:00.000-0400",
                "resolutiondate": f"{t['resolved']}T16:00:00.000-0400"
                                  if done and t["resolved"] else None,
                ids["fund"]: {"value": t["fund"]} if t["fund"] else None,
                ids["ops_team"]: {"value": t["ops_team"]},
                ids["break_type"]: {"value": t["break_type"]},
                ids["break_amount_usd"]: t["amount"],
                ids["business_date"]: t["business_date"].isoformat(),
                ids["resolved_business_date"]:
                    t["resolved"].isoformat() if t["resolved"] else None,
            }
            issues.append({"key": f"{PROJECT_KEY}-{n}", "fields": f})
        snap = {"extracted_at": f"{self.end}T23:00:00+00:00",
                "jql": f"project = {PROJECT_KEY} AND labels = daily-sim (synthetic)",
                "field_ids": ids, "field_warnings": [], "issues": issues}
        path = out_dir / SNAPSHOT_NAME
        path.write_text(json.dumps(snap, indent=1), encoding="utf-8")
        return path


def write_pending(path: Path, trades: list[Trade], as_of: date) -> None:
    """Open-trades report from the order management system."""
    cols = ["Trade ID", "Fund Code", "Security ID", "Ticker", "Side", "Quantity",
            "Trade Date", "Contractual Settle Date", "Trade Status", "Report Date"]
    rows = [[t.trade_id, t.fund, t.isin, t.ticker, "Buy" if t.qty > 0 else "Sell",
             abs(t.qty), t.trade_date, t.settle_date, "Open", as_of] for t in trades]
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        pd.DataFrame(rows, columns=cols).to_excel(xw, sheet_name="Open Trades", index=False)


def generate(out_dir: Path = DAILY_DIR, start: date = START, end: date = END,
             seed: int = SEED) -> list[DayResult]:
    return Simulator(seed, start, end).run(out_dir)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, default=START)
    ap.add_argument("--end", type=date.fromisoformat, default=END)
    ap.add_argument("--out", type=Path, default=DAILY_DIR)
    a = ap.parse_args()
    t0 = datetime.now()
    res = generate(a.out, a.start, a.end)
    n = sum(len(r.key) for r in res)
    act = sum(1 for r in res for k in r.key if k["status"] == "Actionable")
    print(f"{len(res)} business days {res[0].as_of}..{res[-1].as_of} -> {a.out}")
    print(f"{n} break-days ({act} actionable), "
          f"{(datetime.now() - t0).total_seconds():.0f}s")


if __name__ == "__main__":
    main()
