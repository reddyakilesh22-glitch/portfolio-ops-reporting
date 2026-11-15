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


def busday_count(start: pd.Series, end: date) -> pd.Series:
    """Business days from each start date (inclusive) to end (exclusive)."""
    if start.empty:
        return pd.Series(dtype="int64", index=start.index)
    s = pd.to_datetime(start).values.astype("datetime64[D]")
    require_coverage(pd.Timestamp(s.min()).date(), end)
    counts = np.busday_count(s, np.datetime64(end), holidays=holidays())
    return pd.Series(counts, index=start.index)


def is_business_day(d: date) -> bool:
    return bool(np.is_busday(np.datetime64(d), holidays=holidays()))
