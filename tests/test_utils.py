from datetime import datetime, timedelta
from types import SimpleNamespace

from app.utils import format_date_range, local_to_utc, utc_to_local, format_shift_range


def _shift(day, month=7):
    """A minimal shift-like object (local date at midday) for date-range tests."""
    start = local_to_utc(datetime(2026, month, day, 12, 0))
    return SimpleNamespace(start_time=start, end_time=start + timedelta(hours=2))


def test_local_utc_roundtrip():
    dt = datetime(2026, 7, 3, 14, 30)
    assert utc_to_local(local_to_utc(dt)) == dt


def test_none_passthrough():
    assert utc_to_local(None) is None
    assert local_to_utc(None) is None


def test_format_shift_range_same_day():
    # Two UTC times on the same local day render as "start – end" with one date.
    start = local_to_utc(datetime(2026, 7, 4, 9, 0))
    end = local_to_utc(datetime(2026, 7, 4, 12, 0))
    text = format_shift_range(start, end)
    assert "09:00 AM" in text
    assert "12:00 PM" in text
    assert text.count("Jul 04") == 1


def test_format_shift_range_start_only():
    start = local_to_utc(datetime(2026, 7, 4, 9, 0))
    assert "09:00 AM" in format_shift_range(start)


def test_format_date_range():
    assert format_date_range([]) == ""                              # nothing scheduled
    assert format_date_range([_shift(5)]) == "Jul 05"               # single date
    # Spans multiple dates -> earliest to latest, unordered input is fine.
    assert format_date_range([_shift(20, 8), _shift(5)]) == "Jul 05 – Aug 20"
