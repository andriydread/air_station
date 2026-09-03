"""The whole shared layer together, the way the apps will use it:
config → database → strict logger → scheduler with aligned tasks → rollup →
a display_data document → the panel picture → the log file.
"""

from datetime import datetime, timezone

import pytest

from shared import clock
from shared.aqi import aqi_category, aqi_from_pm25, co2_category
from shared.db import Database
from shared.events import Log
from shared.loop import Loop, Task
from shared.render import render
from tests.mocks.fake_devices import FakeClock

START = datetime(2026, 9, 3, 11, 58, 5, tzinfo=timezone.utc).timestamp()  # two minutes before an hour


@pytest.fixture
def fake_clock(monkeypatch):
    fake = FakeClock(start=START)
    monkeypatch.setattr(clock, "now", fake.now)
    monkeypatch.setattr(clock, "monotonic", fake.monotonic)
    monkeypatch.setattr(clock, "sleep", fake.sleep)
    return fake


def test_five_simulated_minutes(tmp_config, fake_clock):
    db = Database(tmp_config.paths.database, now=clock.now)
    log = Log("collector", tmp_config, db=db, strict=True, clock=clock.now)
    log.start_line(tmp_config, commit="e2e0000")
    log.event("info", "app", "started", "collector started")

    beats = []

    def sample():
        ts = clock.aligned_stamp(10, clock.now())
        beats.append(ts)
        db.insert_raw(ts, {"co2": 800 + len(beats), "temp": 22.0, "pm25": 3.0})
        log.debug("scd41", "sample", co2=800 + len(beats), read_ms=6)

    def status():
        db.set_state("collector_status", {"uptime": int(clock.now() - START), "ok": True})

    def minute():
        averages = db.minute_average(int(clock.now()))
        aqi = aqi_from_pm25(averages["values"]["pm25"])
        full, short = aqi_category(aqi)
        db.set_state("display_data", {
            "updated_at": int(clock.now()), "warming_up": False, "collector_silent": False,
            "values": averages["values"], "samples": averages["samples"],
            "aqi": aqi, "aqi_category": full, "aqi_short": short,
            "co2_category": co2_category(averages["values"]["co2"]),
            "weather": {"stale": True, "blocks": []}, "glyphs": {},
        })

    hours_rolled = []

    def hourly():
        current_hour = int(clock.now()) // 3600 * 3600
        if hours_rolled and hours_rolled[-1] == current_hour:
            return
        result = db.rollup_catchup(int(clock.now()))
        if result["rolled"]:
            hours_rolled.append(current_hour)
            log.event("info", "storage", "rollup_catchup", "rolled", hours=result["hours"])

    loop = Loop(log, None, [
        Task("sample", 10, sample, aligned=True, first_run_immediately=False),
        Task("status", 30, status),
        Task("minute", 60, minute, aligned=True, first_run_immediately=False),
        Task("hourly", 60, hourly, aligned=True, first_run_immediately=False),
    ])
    loop.run(max_passes=int(5 * 60 / 0.2) + 1)

    # 30 beats on :00/:10/... stamps
    rows = db.raw_between(0, 10**10)
    assert len(rows) == 30 and all(r["recorded_at"] % 10 == 0 for r in rows)
    assert rows[0]["recorded_at"] == START - 5 + 10  # first aligned beat after start

    # the finished hour (11:00–12:00 UTC) was rolled up once the clock crossed 12:00
    hour = int(START) // 3600 * 3600
    hourly_rows = db.hourly_between(hour, hour + 3600)
    assert len(hourly_rows) == 1 and hourly_rows[0]["samples"] == 11  # 11:58:10 … 11:59:50
    assert db.count_events("rollup_catchup", 0) == 1

    # display_data has the shape the panel and the Live tab expect, and renders
    doc = db.get_state("display_data")["value"]
    assert doc["samples"]["co2"] == 6 and doc["co2_category"] == "Good" and doc["aqi_short"] == "Good"
    image, painted = render(doc, now=clock.now())
    assert image.size == (416, 240) and any(s.startswith("CO2: 8") for s in painted)

    # the log file: exact format, debug lines present, start line first
    log.close()
    lines = log.path.read_text().splitlines()
    assert lines[0].split(" ")[1:5] == ["INFO", "collector", "app", "start"]
    assert sum(1 for l in lines if " DEBUG collector scd41 sample " in l) == 30
    assert all(l[10] == "T" and l[19] == "Z" for l in lines)
    db.close()
