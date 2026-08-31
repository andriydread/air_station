"""Tests for the single-source plausibility rules (B4)."""

from airmonitor.storage import AirMonitorDatabase
from airmonitor.validation import clean_value


def test_none_passes_through():
    assert clean_value("co2", None) is None


def test_co2_floor_default_and_rounding():
    assert clean_value("co2", 612.6) == 613
    assert clean_value("co2", 350) == 350
    assert clean_value("co2", 349.4) is None


def test_co2_floor_is_configurable():
    assert clean_value("co2", 320, min_valid_co2_ppm=300) == 320
    assert clean_value("co2", 280, min_valid_co2_ppm=300) is None


def test_temperature_and_humidity_ranges():
    assert clean_value("temp", -40.0) == -40.0
    assert clean_value("temp", 85.01) is None
    assert clean_value("humid", 0.0) == 0.0
    assert clean_value("humid", 100.01) is None


def test_particulates_reject_negative():
    assert clean_value("pm25", 0.0) == 0.0
    assert clean_value("pm25", -0.01) is None
    assert clean_value("tps", 0.6789) == 0.68


def test_storage_honors_configured_co2_floor(tmp_path):
    """The collector's configured floor is what storage enforces — previously
    storage hardcoded 350 and silently NULLed values the collector accepted."""
    db = AirMonitorDatabase(str(tmp_path / "t.db"), min_valid_co2_ppm=300)
    try:
        db.insert_measurement({"co2": 320})
        assert db.get_latest_measurement()["co2"] == 320
    finally:
        db.close()
