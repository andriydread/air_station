"""The collector's failure stories, played through the real code with misbehaving fakes."""

import pytest

from collector.sensors import BAD_STREAK_REINIT, SILENCE_REINIT
from tests.mocks.fake_devices import FakeSht41Device
from tests.test_collector_main import START, Station

READY = 60  # seconds after a start on a :00 until the first row (see test_three_minutes_of_life)


@pytest.fixture
def station(tmp_config, fake_clock, monkeypatch):
    s = Station(tmp_config, fake_clock, monkeypatch)
    yield s
    s.close()


def _events(db, type_=None, source=None):
    return [e for e in db.recent_events(limit=1000)
            if (type_ is None or e["type"] == type_) and (source is None or e["source"] == source)]


def test_a_zero_ppm_sensor_is_stored_reset_and_recovers(station):
    station.scd.co2_values = [0.0] * BAD_STREAK_REINIT  # garbage for the first minute of rows
    station.run(6 * 60)
    db = station.db
    reinits = _events(db, "sensor_reinit", "scd41")
    assert len(reinits) == 1 and "6 bad readings in a row" in reinits[0]["message"]
    assert reinits[0]["ts"] == START + READY + 50 and station.scd.reinit_calls == 2
    rows = db.raw_between(0, 10**10)
    assert [r["co2"] for r in rows[:6]] == [0] * 6                       # stored as the sensor said
    quiet = [r for r in rows if START + READY + 60 <= r["recorded_at"] < START + READY + 120]
    assert quiet and all(r["co2"] is None and r["temp"] == 22.5 for r in quiet)  # the SCD41's new quiet minute
    assert rows[-1]["co2"] == 600 and rows[-1]["temp"] == 22.5


def test_a_never_ready_dust_sensor_is_reset_after_a_minute(station):
    station.sps.data_ready = False
    station.run(READY + SILENCE_REINIT + 30)
    reinits = _events(station.db, "sensor_reinit", "sps30")
    assert len(reinits) == 1 and "silent for 60 s" in reinits[0]["message"]
    assert _events(station.db, "sensor_error", "sps30") == []  # silence is not an error streak
    assert station.scd.stop_calls == 2 and station.sps.start_calls == 2  # the others were untouched


def test_i2c_errors_on_one_sensor_log_once_then_recover(station):
    errors = [OSError(121, "Remote I/O error")] * 5

    def flaky_temperature():
        if errors:
            raise errors.pop(0)
        return 22.5

    original = FakeSht41Device.temperature
    FakeSht41Device.temperature = property(lambda self: flaky_temperature())
    try:
        station.run(4 * 60)
    finally:
        FakeSht41Device.temperature = original
    db = station.db
    sensor_errors = _events(db, "sensor_error", "sht41")
    assert len(sensor_errors) == 1 and sensor_errors[0]["details"]["errno"] == 121
    rows = db.raw_between(0, 10**10)
    assert [r["temp"] for r in rows[:5]] == [None] * 5 and rows[-1]["temp"] == 22.5
    assert all(r["co2"] == 600 for r in rows)
    assert _events(db, "sensor_reinit", "sht41") == []  # five failures are one short of a reset


def test_all_three_raising_reopens_the_bus_and_everything_comes_back(station):
    station.scd.raise_on_data_ready = OSError(5, "bus")
    station.sht.raise_on_read = OSError(5, "bus")
    station.sps.raise_on_data_ready = OSError(5, "bus")
    station.run(READY + 20)
    assert station.buses >= 2
    assert len(_events(station.db, "sensor_reinit", "i2c")) >= 1
    for fake in (station.scd, station.sht, station.sps):
        fake.raise_on_data_ready = None
        fake.raise_on_read = None
    station.run(READY + 60)  # a fresh process: the quiet time again, then rows
    rows = station.db.raw_between(0, 10**10)
    assert rows[-1]["temp"] == 22.5 and rows[-1]["co2"] == 600


def test_pressure_is_applied_once_per_change(station):
    db = station.db
    db.set_state("last_weather", {"fetched_at": 1, "pressure_hpa": 1013.0, "hourly": {}})
    station.run(5)
    db.set_state("last_weather", {"fetched_at": 2, "pressure_hpa": 1013.4, "hourly": {}})
    station.run(5)
    db.set_state("last_weather", {"fetched_at": 3, "pressure_hpa": 1015.0, "hourly": {}})
    station.run(5)
    # each run() is a fresh process: the pressure is re-sent once at every start
    # (1013, then 1013 again), and inside a process only when it moved ≥ 1 hPa
    # (1013.4 is not sent; 1015 is)
    assert station.scd.ambient_pressures == [1013, 1013, 1015]


def test_sunday_four_am_triggers_one_clean_and_blanks_dust(tmp_config, fake_clock, monkeypatch):
    import time as _time
    from datetime import datetime

    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    try:
        # local Sunday 03:57:25: rows from 03:59:00; the minute check lands at
        # 04:00:25 → clean → the row at 04:00:30 is inside the 15 s blank
        sunday = datetime(2026, 9, 6, 3, 57, 25).astimezone().timestamp()
        fake_clock._wall = sunday
        s = Station(tmp_config, fake_clock, monkeypatch)
        try:
            s.run(4 * 60)
            cleans = _events(s.db, "fan_clean")
            assert len(cleans) == 1 and cleans[0]["details"]["manual"] is False
            rows = s.db.raw_between(0, 10**10)
            clean_ts = cleans[0]["ts"]
            blanked = [r for r in rows if clean_ts <= r["recorded_at"] < clean_ts + 15]
            assert blanked and all(r["pm25"] is None and r["co2"] == 600 for r in blanked)
            after = [r for r in rows if r["recorded_at"] >= clean_ts + 15]
            assert after and after[0]["pm25"] == 2.5
        finally:
            s.close()
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()
