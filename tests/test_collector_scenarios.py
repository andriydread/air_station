"""The collector's failure stories, played through the real code with misbehaving fakes."""

import pytest

from collector.sensors import BAD_STREAK_REINIT, SILENCE_REINIT
from tests.mocks.fake_devices import FakeSht41Device
from tests.test_collector_main import Station


@pytest.fixture
def station(tmp_config, fake_clock, monkeypatch):
    s = Station(tmp_config, fake_clock, monkeypatch)
    yield s
    s.close()


def _events(db, type_=None, source=None):
    return [e for e in db.recent_events(limit=1000)
            if (type_ is None or e["type"] == type_) and (source is None or e["source"] == source)]


def test_a_zero_ppm_sensor_is_reinitialised_and_recovers(station):
    station.scd.co2_values = [0.0] * BAD_STREAK_REINIT  # garbage for three minutes after warm-up
    station.run(7 * 60)
    db = station.db
    reinits = _events(db, "sensor_reinit", "scd41")
    assert len(reinits) == 1 and "bad readings" in reinits[0]["message"]
    assert station.scd.reinit_calls == 2
    rows = db.raw_between(0, 10**10)
    # empty during warm-up (1), garbage (6), warm-up again (2-3), then real values
    assert rows[-1]["co2"] == 600 and rows[-1]["temp"] == 22.5
    drops = _events(db, "value_dropped")
    assert len(drops) == 1  # one streak, one event


def test_a_never_ready_dust_sensor_is_reinitialised_after_two_minutes(station):
    station.sps.data_ready = False
    station.run(30 + SILENCE_REINIT + 30)
    reinits = _events(station.db, "sensor_reinit", "sps30")
    assert len(reinits) == 1 and "silent" in reinits[0]["message"]
    assert _events(station.db, "sensor_error", "sps30") == []  # silence is not an error streak
    assert station.scd.stop_calls >= 1  # the others were untouched by the re-init


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
    assert _events(db, "sensor_reinit", "sht41") == []  # five failures are one short of a re-init


def test_all_three_raising_reinits_the_bus_and_everything_comes_back(station):
    station.scd.raise_on_data_ready = OSError(5, "bus")
    station.sht.raise_on_read = OSError(5, "bus")
    station.sps.raise_on_data_ready = OSError(5, "bus")
    station.run(70)
    assert station.buses >= 2
    assert len(_events(station.db, "sensor_reinit", "i2c")) >= 1
    for fake in (station.scd, station.sht, station.sps):
        fake.raise_on_data_ready = None
        fake.raise_on_read = None
    station.run(150)  # the re-inited SCD41 warms up 60 s first
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
        # local Sunday 03:59:25: the minute check lands at 04:00:25, so the
        # 15 s blank after the clean covers the beat read at 04:00:35
        sunday = datetime(2026, 9, 6, 3, 59, 25).astimezone().timestamp()
        fake_clock._wall = sunday
        s = Station(tmp_config, fake_clock, monkeypatch)
        try:
            s.run(3 * 60)
            cleans = _events(s.db, "fan_clean")
            assert len(cleans) == 1 and cleans[0]["details"]["manual"] is False
            rows = s.db.raw_between(0, 10**10)
            clean_ts = cleans[0]["ts"]
            blanked = [r for r in rows if clean_ts <= r["recorded_at"] < clean_ts + 15]
            assert blanked and all(r["pm25"] is None for r in blanked)
            after = [r for r in rows if r["recorded_at"] >= clean_ts + 20]
            assert after and after[0]["pm25"] == 2.5
        finally:
            s.close()
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()
