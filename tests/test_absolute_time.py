import datetime as dt
from src.cli import parse_absolute_time


def test_parse_absolute_time():
    now = dt.datetime(2026, 10, 16, 12, 0).astimezone()

    cases = [
        ("2:55 PM on 22 Oct", dt.datetime(2026, 10, 22, 14, 55)),
        ("10:32 PM", dt.datetime(2026, 10, 16, 22, 32)),
        ("Oct 22, 2026 2:55 PM", dt.datetime(2026, 10, 22, 14, 55)),
    ]

    for text, expected in cases:
        result = parse_absolute_time(text, now)

        assert result.tzinfo is not None
        assert result.utcoffset() is not None
        assert result.tzinfo == now.tzinfo
        assert (
            result.replace(tzinfo=None) == expected
        ), f"{text!r}: expected {expected}, got {result}"
