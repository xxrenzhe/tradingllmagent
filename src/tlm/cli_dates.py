from __future__ import annotations

from datetime import date, timedelta


def iter_dates(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
