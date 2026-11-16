"""Daily run: reconcile one business day, explain timing breaks, keep history.

    extract -> validate -> reconcile -> pending-trade matching -> break IDs
            -> link Jira tickets -> save to history -> analyst exceptions file

A break keeps one ID from the day it first appears until it clears: if the
same fund and security was a break in the previous successful run, today's
break carries that ID and first-seen date forward.

A day whose inputs fail is still recorded (status FAILED, with its errors),
so the dashboard shows the gap instead of silently skipping it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from opsreport.calendar import busday_count, holidays, is_business_day
from opsreport.config import ROOT, load_yaml
from opsreport.extract import excel_loader
from opsreport.extract.excel_loader import SourceFileError
from opsreport.extract.jira_client import load_snapshot, snapshot_to_frame
from opsreport.history import DB_PATH, connect, previous_breaks, save_day
from opsreport.pipeline import DataQualityError, _git_commit, _rel, _sha256
from opsreport.reconcile.pending import ACTIONABLE, EXPECTED, match_pending, unmatched_trades
from opsreport.reconcile.positions import reconcile
from opsreport.report.exceptions_report import write_exceptions
from opsreport.transform import aum as aum_mod
from opsreport.transform import jira_metrics as jm
from opsreport.validate.checks import (
    ERROR,
    WARNING,
    Issue,
    check_jira,
    check_positions,
    issues_frame,
)

log = logging.getLogger("opsreport.daily")
DAILY_RAW = ROOT / "data" / "daily"
DAILY_SNAPSHOT = DAILY_RAW / "jira_snapshot.json"
DAILY_OUT = ROOT / "output" / "daily"
BREAK_LABEL = r"break-(BRK-[^;]+)"
UNKNOWN_STATUS = "Unknown (ticket data incomplete)"


def make_break_id(first_seen: date, fund_code: str, isin: str) -> str:
    return f"BRK-{first_seen:%Y%m%d}-{fund_code}-{isin}"


@dataclass
class DailyResult:
    as_of: date
    status: str
    breaks: pd.DataFrame = field(default_factory=pd.DataFrame)
    cleared: pd.DataFrame = field(default_factory=pd.DataFrame)
    issues: pd.DataFrame = field(default_factory=pd.DataFrame)
    exceptions_path: Path | None = None
    message: str = ""


def business_days(start: date, end: date) -> list[date]:
    hol = [pd.Timestamp(h).date() for h in holidays()]
    return list(pd.bdate_range(start, end, freq="C", holidays=hol).date)


def load_tickets(snapshot: Path) -> pd.DataFrame | None:
    if not snapshot.exists():
        return None
    t = snapshot_to_frame(load_snapshot(snapshot))
    t["break_id"] = t["labels"].astype("string").str.extract(BREAK_LABEL)[0]
    t["snapshot"] = snapshot.name
    return t


def link_tickets(breaks: pd.DataFrame, tickets: pd.DataFrame | None,
                 as_of: date) -> pd.DataFrame:
    """Attach each break's Jira ticket, with its status *as of* the run date.

    A ticket whose dates are incomplete (e.g. Done with no resolved date) can't
    be placed in time, but the link is still known: it shows as UNKNOWN_STATUS
    and the ticket is listed on the Data Quality tab.
    """
    out = breaks.assign(ticket_key=None, ticket_status=None, ticket_due=None, ops_team=None)
    if tickets is None or out.empty:
        return out
    linked = tickets[tickets["break_id"].notna()
                     & (tickets["business_date"] <= as_of)].drop_duplicates(
                         "break_id", keep="last").set_index("break_id")
    timed = jm.prepare(linked.reset_index(), as_of).set_index("break_id")
    status = pd.Series(UNKNOWN_STATUS, index=linked.index, dtype=object)
    status[timed.index] = np.where(~timed["open_as_of"], "Resolved",
                                   np.where(timed["overdue"], "Overdue", "Open"))
    ids = out["break_id"].where(out["break_id"].isin(linked.index))
    hit = ids.notna()
    out.loc[hit, "ticket_key"] = ids[hit].map(linked["key"]).values
    out.loc[hit, "ticket_status"] = ids[hit].map(status).values
    out.loc[hit, "ticket_due"] = ids[hit].map(linked["due_date"]).values
    out.loc[hit, "ops_team"] = ids[hit].map(linked["ops_team"]).values
    return out


def assign_ids(breaks: pd.DataFrame, prev: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Carry break IDs forward from the previous run; new breaks get new IDs."""
    carried = prev[["fund_code", "isin", "break_id", "first_seen"]]
    out = breaks.merge(carried, on=["fund_code", "isin"], how="left")
    new = out["break_id"].isna()
    out.loc[new, "first_seen"] = as_of
    out.loc[new, "break_id"] = [make_break_id(as_of, f, i) for f, i in
                                zip(out.loc[new, "fund_code"], out.loc[new, "isin"],
                                    strict=True)]
    out["days_open"] = busday_count(out["first_seen"], as_of) if len(out) else []
    return out


def with_match_basis(aum: pd.DataFrame, custodian: pd.DataFrame,
                     accounting: pd.DataFrame) -> pd.DataFrame:
    """Add what the match rate and NAV impact need: holdings compared per fund
    (a position on either side counts once) and shares outstanding."""
    keys = pd.concat([custodian[["fund_code", "isin"]], accounting[["fund_code", "isin"]]])
    compared = keys.drop_duplicates().groupby("fund_code").size()
    shares = {f["code"]: f.get("shares_outstanding")
              for f in load_yaml("funds.yaml")["funds"]}
    return aum.assign(holdings_compared=aum["fund_code"].map(compared).fillna(0).astype(int),
                      shares_outstanding=aum["fund_code"].map(shares))


def run_daily(as_of: date, raw_dir: Path = DAILY_RAW, db_path: Path = DB_PATH,
              out_dir: Path = DAILY_OUT, snapshot: Path = DAILY_SNAPSHOT,
              write_excel: bool = True) -> DailyResult:
    """Run one day. Raises SourceFileError / DataQualityError after recording
    the day as FAILED."""
    started = datetime.now()
    run = {"run_date": as_of, "started_at": started.isoformat(timespec="seconds"),
           "git_commit": _git_commit()}
    conn = connect(db_path)
    try:
        def fail(issues: pd.DataFrame, message: str) -> None:
            save_day(conn, {**run, "status": "FAILED", "message": message,
                            "errors": int((issues["severity"] == ERROR).sum()),
                            "warnings": int((issues["severity"] == WARNING).sum()),
                            "finished_at": datetime.now().isoformat(timespec="seconds")},
                     issues=issues.rename(columns={"check": "rule"}))

        try:
            paths = {k: excel_loader.source_path(k, as_of, raw_dir)
                     for k in ("custodian", "accounting", "fx", "pending_trades")}
            cur = excel_loader.load_all(as_of, raw_dir)
            pending = excel_loader.load_pending(as_of, paths["pending_trades"])
        except SourceFileError as exc:
            fail(issues_frame([Issue(ERROR, "run", "source_file", str(exc))]), str(exc))
            raise
        run["inputs"] = json.dumps({_rel(p): _sha256(p) for p in paths.values()})

        found = (check_positions(cur["custodian"], cur["fx"])
                 + check_positions(cur["accounting"], cur["fx"]))
        if not is_business_day(as_of):
            found.append(Issue(WARNING, "run", "as_of_not_business_day",
                               f"{as_of} is a weekend or exchange holiday"))
        tickets = load_tickets(snapshot)
        if tickets is None:
            found.append(Issue(WARNING, "jira", "no_ticket_snapshot",
                               f"{_rel(snapshot)} not found; breaks shown without tickets"))
        else:
            found += check_jira(tickets[tickets["business_date"].isna()
                                        | (tickets["business_date"] <= as_of)])
        issues = issues_frame(found)
        n_err = int((issues["severity"] == ERROR).sum())
        if n_err:
            msg = f"{n_err} data-quality error(s); nothing published for {as_of}"
            fail(issues, msg)
            raise DataQualityError(msg + ":\n" + issues[issues["severity"] == ERROR]
                                   .to_string(index=False))

        breaks = match_pending(reconcile(cur["custodian"], cur["accounting"]), pending, as_of)
        src = cur["accounting"][["fund_code", "isin", "pricing_source"]]
        breaks = breaks.merge(src, on=["fund_code", "isin"], how="left")
        stale = unmatched_trades(pending, breaks)
        if len(stale):
            found += [Issue(WARNING, "pending_trades", "pending_trade_without_break",
                            "open trade but custodian and accounting agree: OMS report "
                            "stale or trade not booked", f"{r.fund_code}/{r.ticker}/{r.trade_id}")
                      for r in stale.itertuples()]
            issues = issues_frame(found)

        prev_date, prev = previous_breaks(conn, as_of)
        breaks = link_tickets(assign_ids(breaks, prev, as_of), tickets, as_of)
        breaks = breaks.sort_values(["status", "exposure_usd"], ascending=[True, False])
        cleared = prev[~prev["break_id"].isin(breaks["break_id"])]
        actionable = breaks[breaks["status"] == ACTIONABLE]
        aum = with_match_basis(aum_mod.aum_by_fund(cur["accounting"], cur["custodian"]),
                               cur["custodian"], cur["accounting"])

        run.update(status="OK", breaks=len(breaks), actionable=len(actionable),
                   expected=int((breaks["status"] == EXPECTED).sum()),
                   actionable_usd=float(actionable["exposure_usd"].sum()),
                   errors=0, warnings=len(issues),
                   finished_at=datetime.now().isoformat(timespec="seconds"))
        save_day(conn, run, breaks, aum, issues.rename(columns={"check": "rule"}), tickets)
    finally:
        conn.close()

    result = DailyResult(as_of, "OK", breaks, cleared, issues)
    if write_excel:
        result.exceptions_path = write_exceptions(
            as_of, prev_date, breaks, cleared, issues,
            out_dir / as_of.isoformat() / f"ops_exceptions_{as_of:%Y%m%d}.xlsx")
    log.info("Daily %s: %d actionable ($%s), %d expected, %d cleared since %s",
             as_of, len(actionable), f"{actionable['exposure_usd'].sum():,.0f}",
             run["expected"], len(cleared), prev_date or "n/a")
    return result


def backfill(start: date, end: date, raw_dir: Path = DAILY_RAW, db_path: Path = DB_PATH,
             out_dir: Path = DAILY_OUT, snapshot: Path = DAILY_SNAPSHOT
             ) -> list[DailyResult]:
    """Run every business day in order. A failed day is recorded and skipped.

    Only the last day writes an exceptions file: that is the one analysts use.
    """
    days = business_days(start, end)
    results = []
    for d in days:
        try:
            results.append(run_daily(d, raw_dir, db_path, out_dir, snapshot,
                                     write_excel=d == days[-1]))
        except (SourceFileError, DataQualityError) as exc:
            log.error("%s FAILED: %s", d, str(exc).splitlines()[0])
            results.append(DailyResult(d, "FAILED", message=str(exc).splitlines()[0]))
    return results
