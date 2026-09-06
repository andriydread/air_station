"""The "cannot be air" rule: a reason, never a changed value."""

import pytest

from shared.filters import implausible, plausible


@pytest.mark.parametrize(
    "metric, value, reason",
    [
        ("co2", 812.6, None),
        ("co2", 350, None),
        ("co2", 349.9, "range"),
        ("co2", 40000, None),
        ("co2", 65535, "range"),          # the 0xFFFF garbage word
        ("co2", 0, "range"),
        ("temp", 23.456, None),
        ("temp", -40.0, None),
        ("temp", 85.1, "range"),
        ("co2_temp", -41, "range"),
        ("humid", 100.0, None),
        ("humid", 100.01, "range"),
        ("co2_humid", -0.5, "range"),
        ("pm25", 0.0, None),
        ("pm25", -0.1, "negative"),
        ("nc05", 12.3456, None),
        ("pm10", float("nan"), "nonfinite"),
        ("co2", float("inf"), "nonfinite"),
        ("temp", "warm", "nonfinite"),
        ("co2", None, None),              # not read: nothing to judge
    ],
)
def test_implausible(metric, value, reason):
    assert implausible(metric, value) == reason


def test_plausible_needs_a_value_that_could_be_air():
    assert plausible("co2", 600) is True
    assert plausible("co2", 0) is False
    assert plausible("co2", None) is False
