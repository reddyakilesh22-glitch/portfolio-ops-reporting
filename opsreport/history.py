"""SQLite history of daily runs: the memory behind the dashboard.

One row per run in `runs` (including FAILED runs, so gaps are visible), and
that day's breaks, AUM and data-quality issues in their own tables. Weekly
and monthly figures are queries over these daily rows, never separate
calculations, so the views cannot disagree with each other.

Re-running a day replaces that day's rows in one transaction, so a re-run
is safe and a crash mid-write leaves the previous version intact.

The schema version is kept in `PRAGMA user_version`. An older database is
upgraded in place by MIGRATIONS (columns added, nothing rewritten); days saved
before a column existed keep NULL there, and the dashboard shows them as n/a
rather than as a wrong number.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd

from opsreport.config import ROOT

DB_PATH = ROOT / "data" / "history.db"
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_date       TEXT PRIMARY KEY,
    status         TEXT NOT NULL CHECK (status IN ('OK', 'FAILED')),
    started_at     TEXT,
    finished_at    TEXT,
    breaks         INTEGER,
    actionable     INTEGER,
    expected       INTEGER,
    actionable_usd REAL,
    errors         INTEGER,
    warnings       INTEGER,
    message        TEXT,
    git_commit     TEXT,
    inputs         TEXT
);
CREATE TABLE IF NOT EXISTS breaks (
    run_date         TEXT NOT NULL,
    break_id         TEXT NOT NULL,
    fund_code        TEXT,
    isin             TEXT,
    ticker           TEXT,
    description      TEXT,
    asset_class      TEXT,
    break_type       TEXT,
    status           TEXT,
    explanation      TEXT,
    likely_cause     TEXT,
    materiality      TEXT,
    exposure_usd     REAL,
    base_mv_diff     REAL,
    abs_base_mv_diff REAL,
    quantity_diff    REAL,
    pricing_source   TEXT,
    first_seen       TEXT,
    days_open        INTEGER,
    ticket_key       TEXT,
    ticket_status    TEXT,
    ticket_due       TEXT,
    ops_team         TEXT,
    PRIMARY KEY (run_date, break_id)
);
CREATE TABLE IF NOT EXISTS aum_daily (
    run_date             TEXT NOT NULL,
    fund_code            TEXT NOT NULL,
    fund_name            TEXT,
    positions            INTEGER,
    securities_mv        REAL,
    cash                 REAL,
    accrued_income       REAL,
    aum                  REAL,
    custodian_mv         REAL,
    acct_vs_custodian_mv REAL,
    holdings_compared    INTEGER,
    shares_outstanding   REAL,
    PRIMARY KEY (run_date, fund_code)
);
CREATE TABLE IF NOT EXISTS data_quality (
    run_date TEXT NOT NULL,
    severity TEXT,
    source   TEXT,
    rule     TEXT,
    detail   TEXT,
    record   TEXT
);
CREATE TABLE IF NOT EXISTS tickets (
    key                    TEXT PRIMARY KEY,
    break_id               TEXT,
    summary                TEXT,
    status                 TEXT,
    status_category        TEXT,
    priority               TEXT,
    fund                   TEXT,
    ops_team               TEXT,
    break_type             TEXT,
    break_amount_usd       REAL,
    business_date          TEXT,
    due_date               TEXT,
    resolved_business_date TEXT,
    snapshot               TEXT
);
CREATE INDEX IF NOT EXISTS breaks_by_id ON breaks (break_id);
CREATE INDEX IF NOT EXISTS dq_by_date ON data_quality (run_date);
"""

COLUMNS = {
    "runs": ["run_date", "status", "started_at", "finished_at", "breaks", "actionable",
             "expected", "actionable_usd", "errors", "warnings", "message", "git_commit",
             "inputs"],
    "breaks": ["run_date", "break_id", "fund_code", "isin", "ticker", "description",
               "asset_class", "break_type", "status", "explanation", "likely_cause",
               "materiality", "exposure_usd", "base_mv_diff", "abs_base_mv_diff",
               "quantity_diff", "pricing_source", "first_seen", "days_open", "ticket_key",
               "ticket_status", "ticket_due", "ops_team"],
    "aum_daily": ["run_date", "fund_code", "fund_name", "positions", "securities_mv", "cash",
                  "accrued_income", "aum", "custodian_mv", "acct_vs_custodian_mv",
                  "holdings_compared", "shares_outstanding"],
    "data_quality": ["run_date", "severity", "source", "rule", "detail", "record"],
    "tickets": ["key", "break_id", "summary", "status", "status_category", "priority",
                "fund", "ops_team", "break_type", "break_amount_usd", "business_date",
                "due_date", "resolved_business_date", "snapshot"],
}
DATE_COLUMNS = {"run_date", "first_seen", "ticket_due", "business_date", "due_date",
                "resolved_business_date"}


# version -> statements that bring a database at that version up to the next.
MIGRATIONS = {
    1: ["ALTER TABLE aum_daily ADD COLUMN holdings_compared INTEGER",
        "ALTER TABLE aum_daily ADD COLUMN shares_outstanding REAL"],
}


class HistoryError(RuntimeError):
    """The history database is from an incompatible version."""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        conn.close()
        raise HistoryError(f"{path} has schema version {version}, expected "
                           f"{SCHEMA_VERSION}: written by newer code. Update, or rebuild "
                           "it with `opsreport backfill`")
    with conn:
        if version == 0:                     # new file
            conn.executescript(SCHEMA)
        while 0 < version < SCHEMA_VERSION:
            for sql in MIGRATIONS[version]:
                conn.execute(sql)
            version += 1
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _to_rows(df: pd.DataFrame, table: str, run_date: date | None = None) -> pd.DataFrame:
    out = df.copy()
    if run_date is not None:
        out["run_date"] = run_date
    for col in COLUMNS[table]:
        if col not in out:
            out[col] = None
    out = out[COLUMNS[table]]
    for col in DATE_COLUMNS & set(out.columns):
        out[col] = out[col].map(lambda v: v.isoformat() if isinstance(v, date) else v)
    return out.astype(object).where(out.notna(), None)


def save_day(conn: sqlite3.Connection, run: dict, breaks: pd.DataFrame | None = None,
             aum: pd.DataFrame | None = None, issues: pd.DataFrame | None = None,
             tickets: pd.DataFrame | None = None) -> None:
    """Replace everything stored for run['run_date'] in one transaction."""
    day: date = run["run_date"]
    with conn:
        for table in ("breaks", "aum_daily", "data_quality"):
            conn.execute(f"DELETE FROM {table} WHERE run_date = ?", (day.isoformat(),))
        conn.execute("DELETE FROM runs WHERE run_date = ?", (day.isoformat(),))
        frames = {"runs": pd.DataFrame([run]), "breaks": breaks, "aum_daily": aum,
                  "data_quality": issues}
        if tickets is not None:
            conn.execute("DELETE FROM tickets")
            frames["tickets"] = tickets
        for table, df in frames.items():
            if df is None or df.empty:
                continue
            rows = _to_rows(df, table, None if table == "tickets" else day)
            marks = ", ".join("?" * len(rows.columns))
            conn.executemany(f"INSERT INTO {table} ({', '.join(rows.columns)}) "
                             f"VALUES ({marks})", rows.itertuples(index=False, name=None))


def previous_breaks(conn: sqlite3.Connection, as_of: date) -> tuple[date | None, pd.DataFrame]:
    """Breaks from the latest successful run before `as_of`."""
    row = conn.execute("SELECT max(run_date) FROM runs WHERE status = 'OK' AND run_date < ?",
                       (as_of.isoformat(),)).fetchone()
    if not row or row[0] is None:
        return None, pd.DataFrame(columns=COLUMNS["breaks"])
    prev = date.fromisoformat(row[0])
    return prev, read(conn, "breaks", prev, prev)


def read(conn: sqlite3.Connection, table: str, start: date | None = None,
         end: date | None = None) -> pd.DataFrame:
    """A table as a DataFrame, optionally limited to run dates start..end."""
    sql, args = f"SELECT * FROM {table}", []
    if table != "tickets" and (start or end):
        sql += " WHERE run_date BETWEEN ? AND ?"
        args = [(start or date.min).isoformat(), (end or date.max).isoformat()]
    df = pd.read_sql_query(sql, conn, params=args)
    for col in DATE_COLUMNS & set(df.columns):
        df[col] = pd.to_datetime(df[col], format="%Y-%m-%d").dt.date
    return df
