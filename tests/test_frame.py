"""display_data from raw rows, the collector's status and the weather."""

import time as _time
from datetime import datetime, timedelta

import pytest

from manager.frame import COLLECTOR_SILENT, STATUS_STALE, FrameBuilder
from manager.weather import parse
from shared.db import Database

NOW = 1_788_436_800  # 2026-09-03 12:00:00 UTC


def _weather_doc(fetched_at):
    start = datetime.fromtimestamp(fetched_at).astimezone().replace(minute=0, second=0) - timedelta(hours=2)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(48)]
    payload = {"hourly": {"time": times, "temperature_2m": [20.0] * 48, "precipitation_probability": [5] * 48,
                          "weathercode": [2] * 48, "surface_pressure": [1000.0] * 48, "is_day": [1] * 48}}
    return parse(payload, now=fetched_at)


def _status(now, warmup=0, healthy=True):
    sensor = {"available": True, "healthy": healthy, "last_error": None, "last_ok_at": now,
              "warmup_left": warmup, "reinit_count": 0, "id": "x"}
    return {"sensors": {"i2c": dict(sensor), "scd41": dict(sensor), "sht41": dict(sensor), "sps30": dict(sensor)}}


@pytest.fixture
def frame(tmp_config, tmp_path, log):
    clock = {"t": NOW}
    db = Database(tmp_config.paths.database, now=lambda: clock["t"])
    builder = FrameBuilder(db, log, tmp_config)
    builder.db_ = db
    builder.clock = clock
    yield builder
    db.close()


def _fill(db, now, count=4, **values):
    """Rows on the 30 s beat back from ``now``; the frame averages the two that
    end one beat ago (now-60 and now-30), the ``now`` row has not landed yet."""
    for i in range(count):
        db.insert_raw(now - 30 * i, {"co2": 800 + i, "temp": 22.0, "humid": 40.0, "pm25": 4.0, **values})


def test_happy_frame(frame):
    db = frame.db_
    _fill(db, NOW)
    db.set_state("collector_status", _status(NOW))
    doc = frame.build(NOW, _weather_doc(NOW - 600), wifi_glyph=False, power_glyph=False)
    assert doc["values"]["co2"] == 802 and doc["samples"]["co2"] == 2 and doc["values"]["nc1"] is None  # (801+802)/2
    assert doc["aqi"] == 22 and doc["aqi_category"] == "Good" and doc["aqi_short"] == "Good"
    assert doc["co2_category"] == "Good"
    assert doc["weather"]["stale"] is False and len(doc["weather"]["blocks"]) == 3
    assert doc["glyphs"] == {"wifi": False, "power": False, "sensor": False}
    assert doc["warming_up"] is False and doc["collector_silent"] is False and doc["updated_at"] == NOW


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
    db.set_state("collector_status", _status(NOW, warmup=30))
    frame.clock["t"] = NOW
    doc = frame.build(NOW, None, False, False)
    assert doc["collector_silent"] is True and doc["warming_up"] is False


def test_warming_up_from_a_fresh_status(frame):
    db = frame.db_
    _fill(db, NOW)
    db.set_state("collector_status", _status(NOW, warmup=42))
    doc = frame.build(NOW, None, False, False)
    assert doc["warming_up"] is True and doc["collector_silent"] is False and doc["glyphs"]["sensor"] is False


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
    frame.build(NOW + 180, old, False, False)                 # stale again → a new event
    assert len([e for e in db.recent_events() if e["type"] == "weather_stale"]) == 2


def test_never_fetched_weather_is_absent_not_an_event(frame, db):
    _fill(frame.db_, NOW)
    doc = frame.build(NOW, None, False, False)
    assert doc["weather"]["stale"] is True and doc["weather"]["fetched_at"] is None
    assert not any(e["type"] == "weather_stale" for e in db.recent_events())
