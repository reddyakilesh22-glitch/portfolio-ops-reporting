"""Business-day arithmetic on the configured exchange holiday calendar."""

from __future__ import annotations

from datetime import date
from functools import lru_cache

import numpy as np
import pandas as pd

from opsreport.config import ConfigError, load_yaml


@lru_cache(maxsize=1)
def _calendar() -> tuple[frozenset[int], np.ndarray]:
    cfg = load_yaml("calendar.yaml")
    years = frozenset(int(y) for y in cfg["holidays"])
    days = [d for ds in cfg["holidays"].values() for d in ds]
    for d in days:
        if d.weekday() >= 5:
            raise ConfigError(f"calendar.yaml: {d} is a weekend; list the observed date")
    return years, np.array(sorted(days), dtype="datetime64[D]")


def holidays() -> np.ndarray:
    return _calendar()[1]


def require_coverage(first: date, last: date) -> None:
    """Fail if any year between first and last has no holiday list."""
    years, _ = _calendar()
    missing = [y for y in range(first.year, last.year + 1) if y not in years]
    if missing:
        raise ConfigError(f"Holiday calendar (config/calendar.yaml) has no entries for "
                          f"{missing}; add them before running")


def uncovered_years(first: date, last: date) -> list[int]:
    """Years between first and last that have no holiday list."""
    years, _ = _calendar()
    return [y for y in range(first.year, last.year + 1) if y not in years]


def busday_count(start: pd.Series, end: date) -> pd.Series:
    """Business days from each start date (inclusive) to end (exclusive).

    The report date's own year must be in the calendar: without it every age
    from now on would be wrong, so the run stops. An *older* year missing only
    affects tickets raised back then (their age counts that year's holidays as
    working days); callers surface that as a warning via `uncovered_years`.
    """
    if start.empty:
        return pd.Series(dtype="int64", index=start.index)
    s = pd.to_datetime(start).values.astype("datetime64[D]")
    require_coverage(end, end)
    counts = np.busday_count(s, np.datetime64(end), holidays=holidays())
    return pd.Series(counts, index=start.index)


def is_business_day(d: date) -> bool:
    return bool(np.is_busday(np.datetime64(d), holidays=holidays()))
