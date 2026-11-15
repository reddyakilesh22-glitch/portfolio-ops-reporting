"""Command line entry point.

    python -m opsreport run --as-of 2026-11-13 --prior 2026-11-06
    python -m opsreport run --as-of 2026-11-13 --jira live
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from opsreport.config import ConfigError
from opsreport.extract.excel_loader import SourceFileError
from opsreport.extract.jira_client import JiraError
from opsreport.pipeline import OUTPUT_DIR, DataQualityError, run

# Failures with a clear cause and a runbook entry: one line on the console.
# Anything else is a bug and keeps its full traceback.
KNOWN_FAILURES = (SourceFileError, JiraError, ConfigError, FileNotFoundError)
_trace = logging.getLogger("opsreport.trace")


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
    r.add_argument("--out", type=Path, default=OUTPUT_DIR)
    r.add_argument("--allow-errors", action="store_true",
                   help="write the report even if data-quality ERRORs are found")
    r.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    file_handler = logging.FileHandler(args.out / "run.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        handlers=[console, file_handler], force=True)
    _trace.handlers = [file_handler]  # tracebacks go to run.log only
    _trace.propagate = False
    log = logging.getLogger("opsreport")
    try:
        res = run(args.as_of, args.prior, args.jira, args.snapshot, args.out,
                  args.allow_errors)
    except DataQualityError as exc:
        log.error("%s", exc)
        return 2
    except KNOWN_FAILURES as exc:
        log.error("%s: %s", type(exc).__name__, exc)
        _trace.error("Traceback for the error above", exc_info=True)
        log.error("Run failed; see docs/RUNBOOK.md section 3 (traceback in %s)",
                  args.out / "run.log")
        return 1
    print(f"\nReport: {res.report_path}\nBreaks: {len(res.breaks)} | "
          f"DQ issues: {len(res.issues)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
