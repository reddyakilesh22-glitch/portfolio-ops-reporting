"""Shared test setup.

Tests read a frozen copy of the Jira snapshot (tests/fixtures/snapshots/), not
data/snapshots/, which a real `--jira live --refresh-snapshot` run may replace.
"""

from datetime import date

import pytest

from opsreport import pipeline
from opsreport.config import ROOT

FIXTURE_SNAPSHOTS = ROOT / "tests" / "fixtures" / "snapshots"


@pytest.fixture(autouse=True)
def frozen_snapshots(monkeypatch):
    monkeypatch.setattr(pipeline, "SNAPSHOT_DIR", FIXTURE_SNAPSHOTS)


HISTORY_WINDOW = (date(2026, 9, 28), date(2026, 11, 13))   # 7 weeks: end Sep, Oct, Nov


@pytest.fixture(scope="session")
def history_db(tmp_path_factory):
    """Seven weeks of generated daily data, backfilled into a history store.
    Shared by the dashboard and month-end tests (building it takes ~10s)."""
    from opsreport import daily
    from scripts import generate_daily_data as gd

    tmp = tmp_path_factory.mktemp("history")
    gd.generate(tmp / "raw", *HISTORY_WINDOW)
    path = tmp / "h.db"
    daily.backfill(*HISTORY_WINDOW, raw_dir=tmp / "raw", db_path=path, out_dir=tmp / "out",
                   snapshot=tmp / "raw" / gd.SNAPSHOT_NAME)
    return path
