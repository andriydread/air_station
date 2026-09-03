"""EPA PM2.5 index, the six categories, the CO2 scale."""

import pytest

from shared.aqi import CO2_GUIDES, aqi_category, aqi_from_pm25, co2_category


@pytest.mark.parametrize(
    "pm25, expected",
    [
        (0.0, 0), (9.0, 50), (9.1, 51), (35.4, 100), (35.45, 100), (35.5, 101),
        (55.4, 150), (55.5, 151), (125.4, 200), (225.4, 300), (325.4, 500), (400.0, 500),
        (-3.0, 0), (4.5, 25),
    ],
)
def test_breakpoint_edges(pm25, expected):
    assert aqi_from_pm25(pm25) == expected


def test_none_and_garbage():
    assert aqi_from_pm25(None) is None
    assert aqi_from_pm25(float("nan")) is None
    assert aqi_from_pm25("x") is None


@pytest.mark.parametrize(
    "aqi, full, short",
    [
        (0, "Good", "Good"), (50, "Good", "Good"), (51, "Moderate", "Moderate"),
        (100, "Moderate", "Moderate"), (101, "Unhealthy for Sensitive Groups", "Sensitive"),
        (150, "Unhealthy for Sensitive Groups", "Sensitive"), (151, "Unhealthy", "Unhealthy"),
        (200, "Unhealthy", "Unhealthy"), (201, "Very Unhealthy", "Very Unhealthy"),
        (300, "Very Unhealthy", "Very Unhealthy"), (301, "Hazardous", "Hazardous"),
        (500, "Hazardous", "Hazardous"),
    ],
)
def test_categories(aqi, full, short):
    assert aqi_category(aqi) == (full, short)


def test_category_none():
    assert aqi_category(None) == (None, None)


@pytest.mark.parametrize(
    "ppm, word",
    [(400, "Good"), (999.9, "Good"), (1000, "Elevated"), (1999, "Elevated"), (2000, "Poor"), (5000, "Poor")],
)
def test_co2_scale(ppm, word):
    assert co2_category(ppm) == word


def test_co2_none_and_guides():
    assert co2_category(None) is None and co2_category("x") is None
    assert CO2_GUIDES == (1000, 2000)
