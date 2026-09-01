"""EPA AQI math edge cases (utils/aqi.py is now the ONLY AQI implementation)."""

from utils.aqi import calculate_aqi, get_aqi_category, get_co2_category


def test_breakpoint_edges_pm25():
    assert calculate_aqi(0.0, 0.0) == 0
    assert calculate_aqi(12.0, 0.0) == 50
    assert calculate_aqi(35.4, 0.0) == 100
    assert calculate_aqi(500.4, 0.0) == 500
    assert calculate_aqi(9999.0, 0.0) == 500


def test_worst_pollutant_wins():
    # pm10 at 154 -> AQI 100; pm25 at 12 -> AQI 50
    assert calculate_aqi(12.0, 154.0) == 100
    assert calculate_aqi(35.4, 54.0) == 100


def test_negative_warmup_values_floored():
    assert calculate_aqi(-3.0, -1.0) == 0


def test_categories():
    assert get_aqi_category(50) == "Good"
    assert get_aqi_category(100) == "Moderate"
    assert get_aqi_category(175) == "Unhealthy"
    assert get_aqi_category(301) == "Hazardous"
    assert get_co2_category(999) == "Good"
    assert get_co2_category(1000) == "Moderate"
    assert get_co2_category(1500) == "Unhealthy"
    assert get_co2_category(None) == "N/A"


def test_epa_truncation_at_breakpoint_gaps():
    from utils.aqi import calculate_aqi

    # Values in the gap between brackets truncate DOWN per EPA, and must not
    # jump to the higher bracket (35.45 -> 100 "Moderate", not 101).
    assert calculate_aqi(35.45, 0) == 100
    assert calculate_aqi(12.05, 0) == 50
    assert calculate_aqi(0, 54.9) == 50
    # Exact boundaries unchanged.
    assert calculate_aqi(12.0, 0) == 50
    assert calculate_aqi(0, 54) == 50
