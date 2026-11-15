"""Shared test setup.

Tests read a frozen copy of the Jira snapshot (tests/fixtures/snapshots/), not
data/snapshots/, which a real `--jira live --refresh-snapshot` run may replace.
"""

import pytest

from opsreport import pipeline
from opsreport.config import ROOT

FIXTURE_SNAPSHOTS = ROOT / "tests" / "fixtures" / "snapshots"


@pytest.fixture(autouse=True)
def frozen_snapshots(monkeypatch):
    monkeypatch.setattr(pipeline, "SNAPSHOT_DIR", FIXTURE_SNAPSHOTS)
