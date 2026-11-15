"""End-to-end weekly run: extract -> validate -> reconcile -> metrics -> report."""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from opsreport import __version__
from opsreport.ai import commentary as commentary_mod
from opsreport.calendar import is_business_day
from opsreport.config import ROOT
from opsreport.extract import excel_loader
from opsreport.extract.jira_client import (
    fetch_snapshot,
    load_snapshot,
    save_snapshot,
    snapshot_to_frame,
)
from opsreport.reconcile.positions import reconcile, summarize
from opsreport.report.excel_report import ReportData, default_commentary, write_report
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

log = logging.getLogger("opsreport")
SNAPSHOT_DIR = ROOT / "data" / "snapshots"
OUTPUT_DIR = ROOT / "output"


class DataQualityError(RuntimeError):
    pass


@dataclass
class RunResult:
    report_path: Path
    breaks: pd.DataFrame
    issues: pd.DataFrame
    data: ReportData


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True).stdout.strip()
        return out + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def resolve_snapshot(as_of: date, explicit: Path | None) -> Path:
    """Snapshot for as_of, else the earliest one taken after it.

    Metrics are point-in-time on business dates, so a later snapshot can
    still reproduce an earlier week.
    """
    if explicit:
        return explicit
    exact = SNAPSHOT_DIR / f"jira_{as_of.isoformat()}.json"
    if exact.exists():
        return exact
    later = sorted(p for p in SNAPSHOT_DIR.glob("jira_*.json")
                   if p.stem.removeprefix("jira_") >= as_of.isoformat())
    if not later:
        raise FileNotFoundError(f"No Jira snapshot on/after {as_of} in {SNAPSHOT_DIR}; "
                                "run with --jira live")
    return later[0]


def run(as_of: date, prior: date | None = None, jira: str = "snapshot",
        snapshot: Path | None = None, out_dir: Path = OUTPUT_DIR,
        allow_errors: bool = False, commentary: str = "template",
        model: str = commentary_mod.DEFAULT_MODEL) -> RunResult:
    started = datetime.now()
    log.info("Run start: as_of=%s prior=%s jira=%s", as_of, prior, jira)

    # ---- extract
    cur = excel_loader.load_all(as_of)
    prev = excel_loader.load_all(prior) if prior else None
    if jira == "live":
        snap = fetch_snapshot()
        snap_path = SNAPSHOT_DIR / f"jira_{as_of.isoformat()}.json"
        save_snapshot(snap, snap_path)
        log.info("Saved Jira snapshot %s", snap_path.relative_to(ROOT))
    else:
        snap_path = resolve_snapshot(as_of, snapshot)
        snap = load_snapshot(snap_path)
        log.info("Using Jira snapshot %s (extracted %s)", snap_path.name,
                 snap.get("extracted_at"))
    tickets = snapshot_to_frame(snap)

    # ---- validate
    field_issues = [Issue(WARNING, "jira", "field_renamed", w)
                    for w in snap.get("field_warnings", [])]
    if not is_business_day(as_of):
        field_issues.append(Issue(WARNING, "run", "as_of_not_business_day",
                                  f"{as_of} is a weekend or exchange holiday; "
                                  "check the run date"))
    issues = issues_frame(
        field_issues
        + check_positions(cur["custodian"], cur["fx"])
        + check_positions(cur["accounting"], cur["fx"])
        + check_jira(tickets))
    n_err = int((issues["severity"] == ERROR).sum())
    log.info("Data quality: %d error(s), %d warning(s)", n_err, len(issues) - n_err)
    if n_err and not allow_errors:
        raise DataQualityError(
            f"{n_err} data-quality error(s); fix inputs or re-run with --allow-errors:\n"
            + issues[issues["severity"] == ERROR].to_string(index=False))

    # ---- reconcile + metrics
    breaks = reconcile(cur["custodian"], cur["accounting"])
    log.info("Reconciliation: %d break(s), abs $%s", len(breaks),
             f"{breaks['abs_base_mv_diff'].sum():,.0f}")
    aum_now = aum_mod.aum_by_fund(cur["accounting"], cur["custodian"])
    aum_prev = aum_mod.aum_by_fund(prev["accounting"], prev["custodian"]) if prev else None
    aum_tbl = aum_mod.aum_change(aum_now, aum_prev)

    t = jm.prepare(tickets, as_of)
    kpis = jm.kpis(t, as_of)

    # ---- run info (audit trail)
    inputs = [excel_loader.source_path(k, d) for d in filter(None, [as_of, prior])
              for k in ("custodian", "accounting", "fx")] + [snap_path]
    run_info = pd.DataFrame(
        [("as_of", as_of.isoformat()), ("prior", prior.isoformat() if prior else ""),
         ("jira_source", f"{jira}: {snap_path.name}"),
         ("jira_extracted_at", snap.get("extracted_at", "")),
         ("jira_tickets", len(tickets)),
         ("custodian_positions", len(cur["custodian"])),
         ("accounting_positions", len(cur["accounting"])),
         ("opsreport_version", __version__), ("git_commit", _git_commit()),
         ("python", platform.python_version()), ("pandas", pd.__version__),
         ("run_started", started.isoformat(timespec="seconds"))]
        + [(f"input: {p.relative_to(ROOT)}", f"sha256 {_sha256(p)}") for p in inputs],
        columns=["item", "value"])

    data = ReportData(
        as_of=as_of, prior=prior, aum=aum_tbl, breaks=breaks,
        break_summary=summarize(breaks), jira_kpis=kpis,
        jira_by_team=jm.by_team(t, as_of), jira_aging=jm.aging(t),
        jira_trend=jm.weekly_trend(t, as_of), jira_overdue=jm.open_overdue_list(t),
        issues=issues, run_info=run_info)

    if commentary == "local-llm":
        res = commentary_mod.generate(data, default_commentary(data), model=model)
        data.commentary, data.commentary_source = res.bullets, res.source
        day_dir = out_dir / as_of.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / f"commentary_audit_{as_of:%Y%m%d}.json").write_text(
            json.dumps({"source": res.source, "attempts": res.attempts}, indent=1),
            encoding="utf-8")
    data.run_info.loc[len(data.run_info)] = ["commentary", data.commentary_source]

    day_dir = out_dir / as_of.isoformat()
    path = write_report(data, day_dir / f"portfolio_ops_report_{as_of:%Y%m%d}.xlsx")
    breaks.to_csv(day_dir / f"breaks_{as_of:%Y%m%d}.csv", index=False)
    issues.to_csv(day_dir / f"data_quality_{as_of:%Y%m%d}.csv", index=False)
    log.info("Report written: %s (%.1fs)", path.relative_to(ROOT) if path.is_relative_to(ROOT)
             else path, (datetime.now() - started).total_seconds())
    return RunResult(path, breaks, issues, data)
