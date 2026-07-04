from datetime import datetime

from app.utils import local_to_utc, utc_to_local, format_shift_range


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
