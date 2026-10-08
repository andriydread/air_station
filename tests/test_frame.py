"""display_data from raw rows, the collector's status and the weather."""

import time as _time
from datetime import datetime, timedelta

import pytest

from manager.frame import COLLECTOR_SILENT, STARTUP_GRACE, STATUS_STALE, FrameBuilder
from manager.weather import parse
from shared.db import Database

NOW = 1_788_436_800  # 2026-09-03 12:00:00 UTC


def _weather_doc(fetched_at):
    start = datetime.fromtimestamp(fetched_at).astimezone().replace(minute=0, second=0) - timedelta(hours=2)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(48)]
    payload = {"hourly": {"time": times, "temperature_2m": [20.0] * 48, "precipitation_probability": [5] * 48,
                          "weathercode": [2] * 48, "surface_pressure": [1000.0] * 48, "is_day": [1] * 48}}
    return parse(payload, now=fetched_at)


def _status(now, ready_at=None, healthy=True):
    sensor = {"available": True, "healthy": healthy, "last_error": None, "last_ok_at": now,
              "ready_at": ready_at, "reinit_count": 0, "id": "x"}
    return {"ready_at": ready_at,
            "sensors": {"i2c": dict(sensor), "scd41": dict(sensor), "sht41": dict(sensor), "sps30": dict(sensor)}}


@pytest.fixture
def frame(tmp_config, tmp_path, log):
    clock = {"t": NOW}
    db = Database(tmp_config.paths.database, now=lambda: clock["t"])
    builder = FrameBuilder(db, log, tmp_config)
    builder.db_ = db
    builder.clock = clock
    yield builder
    db.close()


def _fill(db, now, count=6, **values):
    """Insert the six 10 s rows of the minute ending at ``now`` (now-60 to now-10)."""
    for i in range(1, count + 1):
        db.insert_raw(now - 10 * i, {"co2": 800 + i, "temp": 22.0, "humid": 40.0, "pm25": 4.0, **values})


def test_happy_frame(frame):
    db = frame.db_
    _fill(db, NOW)
    db.set_state("collector_status", _status(NOW))
    doc = frame.build(NOW, _weather_doc(NOW - 600), wifi_glyph=False, power_glyph=False)
    assert doc["values"]["co2"] == 804 and doc["samples"]["co2"] == 6 and doc["values"]["nc1"] is None  # 801..806
    assert doc["aqi"] == 22 and doc["aqi_category"] == "Good" and doc["aqi_short"] == "Good"
    assert doc["co2_category"] == "Good"
    assert doc["weather"]["stale"] is False and len(doc["weather"]["blocks"]) == 3
    assert doc["glyphs"] == {"wifi": False, "power": False, "sensor": False}
    assert doc["warming_up"] is False and doc["collector_silent"] is False and doc["updated_at"] == NOW


def test_a_value_that_cannot_be_air_is_left_out_of_the_average(frame):
    db = frame.db_
    _fill(db, NOW)
    db.insert_raw(NOW - 30, {"co2": 0, "temp": 22.0, "humid": 40.0, "pm25": -1.0})  # stored as read
    db.insert_raw(NOW, {"co2": 5000})  # first row of the next minute
    db.set_state("collector_status", _status(NOW))
    doc = frame.build(NOW, None, False, False)
    assert doc["samples"]["co2"] == 5 and doc["samples"]["pm25"] == 5 and doc["samples"]["temp"] == 6
    assert doc["values"]["co2"] == round((801 + 802 + 804 + 805 + 806) / 5)


def test_no_rows_in_the_minute_gives_nulls_and_silence(frame):
    db = frame.db_
    db.insert_raw(NOW - COLLECTOR_SILENT - 1, {"co2": 700})
    db.set_state("collector_status", _status(NOW))
    doc = frame.build(NOW, None, False, False)
    assert doc["values"]["co2"] is None and doc["aqi"] is None and doc["co2_category"] is None
    assert doc["collector_silent"] is True and doc["glyphs"]["sensor"] is True


def test_stale_status_means_silent_and_never_warming(frame):
    db = frame.db_
    _fill(db, NOW)
    frame.clock["t"] = NOW - STATUS_STALE - 5
    db.set_state("collector_status", _status(NOW, ready_at=NOW + 30))
    frame.clock["t"] = NOW
    doc = frame.build(NOW, None, False, False)
    assert doc["collector_silent"] is True and doc["warming_up"] is False


def test_the_quiet_time_from_a_fresh_status(frame):
    db = frame.db_
    _fill(db, NOW)
    db.set_state("collector_status", _status(NOW, ready_at=NOW + 42))
    doc = frame.build(NOW, None, False, False)
    assert doc["warming_up"] is True and doc["collector_silent"] is False and doc["glyphs"]["sensor"] is False
    assert doc["warmup_left"] == 42
    db.set_state("collector_status", _status(NOW, ready_at=NOW - 30))  # ready, first minute not averaged yet
    doc = frame.build(NOW, None, False, False)
    assert doc["warming_up"] is True and doc["warmup_left"] == 0 and doc["collector_silent"] is False
    db.set_state("collector_status", _status(NOW, ready_at=NOW - 60))  # first full minute is in
    doc = frame.build(NOW, None, False, False)
    assert doc["warming_up"] is False and doc["warmup_left"] == 0


def test_the_first_frames_after_a_boot_say_starting_up_not_silent(frame):
    db = frame.db_
    frame.started_at = NOW - 10  # manager is 10 s old, no collector status yet
    doc = frame.build(NOW, None, False, False)
    assert doc["warming_up"] is True and doc["warmup_left"] == 0 and doc["collector_silent"] is False
    assert doc["glyphs"]["sensor"] is False
    db.set_state("collector_status", _status(NOW, ready_at=NOW + 50))
    doc = frame.build(NOW + 5, None, False, False)
    assert doc["warming_up"] is True and doc["warmup_left"] == 45
    later = NOW - 10 + STARTUP_GRACE + 1  # grace over, still nothing
    frame.clock["t"] = later
    db.set_state("collector_status", _status(later, ready_at=None))
    db.delete_state = None
    doc = frame.build(later, None, False, False)
    assert doc["warming_up"] is False and doc["collector_silent"] is True


def test_unhealthy_sensor_lights_the_glyph(frame):
    db = frame.db_
    _fill(db, NOW)
    status = _status(NOW)
    status["sensors"]["sps30"]["healthy"] = False
    db.set_state("collector_status", status)
    doc = frame.build(NOW, None, wifi_glyph=True, power_glyph=True)
    assert doc["glyphs"] == {"wifi": True, "power": True, "sensor": True}
    assert doc["unhealthy"] == ["sps30"]


def test_weather_stale_flag_and_single_event(frame, db):
    fdb = frame.db_
    _fill(fdb, NOW)
    old = _weather_doc(NOW - 7 * 3600)
    first = frame.build(NOW, old, False, False)
    frame.build(NOW + 60, old, False, False)
    assert first["weather"]["stale"] is True and first["weather"]["blocks"] == []
    stale_events = [e for e in db.recent_events() if e["type"] == "weather_stale"]
    assert len(stale_events) == 1
    frame.build(NOW + 120, _weather_doc(NOW), False, False)   # fresh again
    frame.build(NOW + 180, old, False, False)                 # stale again, a new event
    assert len([e for e in db.recent_events() if e["type"] == "weather_stale"]) == 2


def test_never_fetched_weather_is_absent_not_an_event(frame, db):
    _fill(frame.db_, NOW)
    doc = frame.build(NOW, None, False, False)
    assert doc["weather"]["stale"] is True and doc["weather"]["fetched_at"] is None
    assert not any(e["type"] == "weather_stale" for e in db.recent_events())


def test_after_a_joint_restart_the_dead_collectors_status_is_not_fresh(frame):
    # After a deploy restarts all three apps, the old collector's shutdown status and
    # recent rows must not put stale numbers on the panel before "Starting up".
    db = frame.db_
    _fill(db, NOW)
    frame.clock["t"] = NOW - 1
    db.set_state("collector_status", _status(NOW - 1, ready_at=NOW - 3600))  # written on shutdown
    frame.clock["t"] = NOW
    frame.started_at = NOW  # manager starts a second later
    doc = frame.build(NOW + 5, None, False, False)
    assert doc["warming_up"] is True and doc["collector_silent"] is False and doc["glyphs"]["sensor"] is False
    frame.clock["t"] = NOW + 15
    db.set_state("collector_status", _status(NOW + 15, ready_at=NOW + 60))  # new collector started
    doc = frame.build(NOW + 60, None, False, False)
    assert doc["warming_up"] is True and doc["warmup_left"] == 0  # first full minute still being averaged
    _fill(db, NOW + 120)
    frame.clock["t"] = NOW + 100
    db.set_state("collector_status", _status(NOW + 100, ready_at=NOW + 60))  # its 30 s publish
    doc = frame.build(NOW + 120, None, False, False)
    assert doc["warming_up"] is False and doc["collector_silent"] is False


def test_a_manager_only_restart_says_starting_up_until_the_collectors_next_status(frame):
    db = frame.db_
    _fill(db, NOW)
    frame.clock["t"] = NOW - 20
    db.set_state("collector_status", _status(NOW - 20, ready_at=NOW - 3600))  # the collector keeps running
    frame.clock["t"] = NOW
    frame.started_at = NOW
    doc = frame.build(NOW + 5, None, False, False)
    assert doc["warming_up"] is True and doc["collector_silent"] is False
    frame.clock["t"] = NOW + 10
    db.set_state("collector_status", _status(NOW + 10, ready_at=NOW - 3600))  # its 30 s publish
    _fill(db, NOW + 60)
    doc = frame.build(NOW + 60, None, False, False)
    assert doc["warming_up"] is False and doc["collector_silent"] is False
