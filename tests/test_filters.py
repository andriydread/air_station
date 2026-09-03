"""The collector's garbage rules."""

import pytest

from collector.filters import clean, clean_row
from shared.db import METRICS


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("co2", 812.6, (813, None)),
        ("co2", 350, (350, None)),
        ("co2", 349.9, (None, "range")),
        ("co2", 40000, (40000, None)),
        ("co2", 65535, (None, "range")),          # the 0xFFFF garbage word
        ("co2", 0, (None, "range")),
        ("temp", 23.456, (23.46, None)),
        ("temp", -40.0, (-40.0, None)),
        ("temp", 85.1, (None, "range")),
        ("co2_temp", -41, (None, "range")),
        ("humid", 100.0, (100.0, None)),
        ("humid", 100.01, (None, "range")),
        ("co2_humid", -0.5, (None, "range")),
        ("pm25", 0.0, (0.0, None)),
        ("pm25", -0.1, (None, "negative")),
        ("nc05", 12.3456, (12.35, None)),
        ("tps", 0.54321, (0.543, None)),
        ("pm10", float("nan"), (None, "nonfinite")),
        ("co2", float("inf"), (None, "nonfinite")),
        ("temp", "warm", (None, "nonfinite")),
        ("co2", None, (None, None)),
    ],
)
def test_clean(field, value, expected):
    assert clean(field, value) == expected


def test_clean_row_keeps_every_metric_and_reports_drops():
    raw = {"co2": 65535, "temp": 22.0, "pm25": -1.0, "tps": 0.5}
    row, dropped = clean_row(raw)
    assert set(row) == set(METRICS)
    assert row["co2"] is None and row["pm25"] is None
    assert row["temp"] == 22.0 and row["tps"] == 0.5 and row["humid"] is None
    assert dropped == {"co2": (65535, "range"), "pm25": (-1.0, "negative")}


def test_clean_row_with_nothing_read():
    row, dropped = clean_row({})
    assert all(value is None for value in row.values()) and dropped == {}
