"""Command line entry point.

    python -m opsreport run --as-of 2026-11-13 --prior 2026-11-06     weekly report
    python -m opsreport run --as-of 2026-11-13 --jira live
    python -m opsreport daily --as-of 2026-11-13                       one day
    python -m opsreport backfill --from 2026-05-18 --to 2026-11-13     every day
    python -m opsreport monthend --month 2026-10                       sign-off pack
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from opsreport import daily
from opsreport.config import ROOT, ConfigError
from opsreport.extract.excel_loader import RAW_DIR, SourceFileError
from opsreport.extract.jira_client import JiraError
from opsreport.history import DB_PATH, HistoryError
from opsreport.pipeline import OUTPUT_DIR, DataQualityError, SnapshotExistsError, run

# Failures with a clear cause and a runbook entry: one line on the console.
# Anything else is a bug and keeps its full traceback.
KNOWN_FAILURES = (SourceFileError, JiraError, ConfigError, FileNotFoundError,
                  SnapshotExistsError, HistoryError)
_trace = logging.getLogger("opsreport.trace")
MONTHEND_OUT = ROOT / "output" / "monthend"


def _month(text: str) -> date:
    try:
        return date.fromisoformat(f"{text}-01")
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM, got {text!r}") from None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="opsreport")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="build the weekly report")
    r.add_argument("--as-of", type=date.fromisoformat, required=True)
    r.add_argument("--prior", type=date.fromisoformat,
                   help="prior business date for week-over-week AUM")
    r.add_argument("--jira", choices=["snapshot", "live"], default="snapshot",
                   help="snapshot (default, offline) or live pull that saves a snapshot")
    r.add_argument("--snapshot", type=Path, help="explicit Jira snapshot file")
    r.add_argument("--refresh-snapshot", action="store_true",
                   help="with --jira live: replace an existing snapshot for this date")
    r.add_argument("--raw-dir", type=Path, default=RAW_DIR,
                   help="folder holding <date>/ input workbooks (default: data/raw)")
    r.add_argument("--out", type=Path, default=OUTPUT_DIR)
    r.add_argument("--allow-errors", action="store_true",
                   help="write the report even if data-quality ERRORs are found")
    r.add_argument("--commentary", choices=["template", "local-llm"], default="template",
                   help="local-llm drafts commentary with a local Ollama model, "
                        "fact-checked against the report; falls back to template")
    r.add_argument("--model", default="llama3.2", help="Ollama model for --commentary")
    r.add_argument("-v", "--verbose", action="store_true")

    for name, text in (("daily", "reconcile one business day into the history store"),
                       ("backfill", "run every business day in a range, in order")):
        d = sub.add_parser(name, help=text)
        if name == "daily":
            d.add_argument("--as-of", type=date.fromisoformat, required=True)
        else:
            d.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
            d.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
        d.add_argument("--raw-dir", type=Path, default=daily.DAILY_RAW,
                       help="folder holding <date>/ input workbooks (default: data/daily)")
        d.add_argument("--db", type=Path, default=DB_PATH, help="history database")
        d.add_argument("--snapshot", type=Path, default=daily.DAILY_SNAPSHOT,
                       help="Jira snapshot holding the break tickets")
        d.add_argument("--out", type=Path, default=daily.DAILY_OUT)
        d.add_argument("-v", "--verbose", action="store_true")
    m = sub.add_parser("monthend", help="month-end sign-off pack from the history store")
    m.add_argument("--month", type=_month, required=True, help="YYYY-MM")
    m.add_argument("--db", type=Path, default=DB_PATH, help="history database")
    m.add_argument("--out", type=Path, default=MONTHEND_OUT)
    m.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    _setup_logging(args.out, args.verbose)
    log = logging.getLogger("opsreport")
    if args.cmd == "daily":
        return _run_daily(args, log)
    if args.cmd == "backfill":
        return _run_backfill(args, log)
    if args.cmd == "monthend":
        return _run_monthend(args, log)
    try:
        res = run(args.as_of, args.prior, args.jira, args.snapshot, args.out,
                  args.allow_errors, args.commentary, args.model,
                  args.refresh_snapshot, args.raw_dir)
    except DataQualityError as exc:
        log.error("%s", exc)
        return 2
    except KNOWN_FAILURES as exc:
        return _known_failure(log, exc, args.out)
    print(f"\nReport: {res.report_path}\nBreaks: {len(res.breaks)} | "
          f"DQ issues: {len(res.issues)}")
    return 0


def _setup_logging(out: Path, verbose: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.FileHandler(out / "run.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        handlers=[console, file_handler], force=True)
    _trace.handlers = [file_handler]  # tracebacks go to run.log only
    _trace.propagate = False


def _known_failure(log: logging.Logger, exc: Exception, out: Path) -> int:
    log.error("%s: %s", type(exc).__name__, exc)
    _trace.error("Traceback for the error above", exc_info=True)
    log.error("Run failed; see docs/RUNBOOK.md section 3 (traceback in %s)", out / "run.log")
    return 1


def _run_daily(args: argparse.Namespace, log: logging.Logger) -> int:
    try:
        res = daily.run_daily(args.as_of, args.raw_dir, args.db, args.out, args.snapshot)
    except DataQualityError as exc:
        log.error("%s", exc)
        return 2
    except KNOWN_FAILURES as exc:
        return _known_failure(log, exc, args.out)
    act = res.breaks[res.breaks["status"] == "Actionable"]
    print(f"\nExceptions: {res.exceptions_path}\nActionable: {len(act)} | "
          f"Expected: {len(res.breaks) - len(act)} | Cleared: {len(res.cleared)}")
    return 0


def _run_backfill(args: argparse.Namespace, log: logging.Logger) -> int:
    try:
        results = daily.backfill(args.start, args.end, args.raw_dir, args.db, args.out,
                                 args.snapshot)
    except KNOWN_FAILURES as exc:
        return _known_failure(log, exc, args.out)
    failed = [r for r in results if r.status == "FAILED"]
    last = results[-1].exceptions_path if results else None
    print(f"\n{len(results)} business day(s), {len(failed)} failed. History: {args.db}"
          + (f"\nLatest exceptions file: {last}" if last else ""))
    for r in failed:
        print(f"  FAILED {r.as_of}: {r.message}")
    return 2 if failed else 0


def _run_monthend(args: argparse.Namespace, log: logging.Logger) -> int:
    from opsreport import dashboard_data, monthend
    from opsreport.report.monthend_report import pack_name, write_monthend

    try:
        if not args.db.exists():
            raise FileNotFoundError(f"No history at {args.db}; run `opsreport backfill` first")
        h = dashboard_data.load(args.db)
        if args.month not in monthend.months(h):
            raise FileNotFoundError(f"No successful run in {args.month:%Y-%m} in {args.db}")
        m = monthend.month_end(h, args.month)
    except KNOWN_FAILURES as exc:
        return _known_failure(log, exc, args.out)
    path = write_monthend(m, args.out / f"{args.month:%Y-%m}" / pack_name(m))
    if m.in_progress:
        log.warning("%s is not over: last run %s, month ends %s", m.title, m.as_of,
                    m.month_end)
    print(f"\nSign-off pack: {path}")
    for r in m.funds.itertuples():
        print(f"  {r.fund_code:<5} {r.status:<16} {r.reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
