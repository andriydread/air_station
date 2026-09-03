"""The manager's failure stories, played through the real code."""

import time as _time
from datetime import datetime

import pytest

from manager.maintenance import COLLECTOR_RESTART_AFTER, RESTART_COOLDOWN
from manager.network import BOUNCE_OFF, BOUNCE_ON
from shared import clock
from tests.test_manager_main import START, Station


@pytest.fixture
def station(tmp_config, tmp_path, fake_clock, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    fake_clock._wall = START
    s = Station(tmp_config, tmp_path, fake_clock)
    yield s
    s.close()
    monkeypatch.delenv("TZ")
    _time.tzset()


def _types(db, type_=None):
    return [e["type"] for e in db.recent_events(limit=1000) if type_ is None or e["type"] == type_]


def test_collector_stops_writing_event_then_restart_then_cooldown(station):
    station.seed_rows(minutes=1, ahead_minutes=0)  # rows stop at START
    station.run(16 * 60)  # restart at +5 min, cooldown 10 min, restart again at +15 min
    db = station.db
    assert _types(db, "collector_silent") == ["collector_silent"]
    restarts = [e for e in db.recent_events(limit=1000) if e["type"] == "collector_restarted"]
    assert len(restarts) == 2  # at ~5 min and again after the 10 min cooldown
    assert restarts[-1]["ts"] - START >= COLLECTOR_RESTART_AFTER
    assert restarts[0]["ts"] - restarts[-1]["ts"] >= RESTART_COOLDOWN
    doc = db.get_state("display_data")["value"]
    assert doc["collector_silent"] is True and doc["values"]["co2"] is None


def test_router_down_three_minutes_bounces_but_internet_only_does_not(station):
    station.seed_rows(ahead_minutes=8)
    station.net.up[("1.1.1.1", 53)] = False
    station.run(4 * 60)
    assert "wifi_bounce" not in _types(station.db) and "internet_down" in _types(station.db)
    station.net.up[("192.168.1.1", 53)] = False
    station.run(4 * 60)
    assert _types(station.db, "wifi_bounce") == ["wifi_bounce"]
    assert BOUNCE_OFF in station.runner.calls and BOUNCE_ON in station.runner.calls
    doc = station.db.get_state("display_data")["value"]
    assert doc["glyphs"]["wifi"] is True


def test_weather_fails_twice_then_succeeds(station):
    station.seed_rows(ahead_minutes=8)
    attempts = {"n": 0}
    real_opener = station.opener

    def flaky(request, timeout):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise OSError("temporary")
        return real_opener(request, timeout)

    station.opener = flaky
    station.run(6 * 60)
    assert attempts["n"] == 3  # start, +2 min, +4 min (success)
    assert _types(station.db, "weather_error") == ["weather_error"]
    doc = station.db.get_state("display_data")["value"]
    assert doc["weather"]["stale"] is False and len(doc["weather"]["blocks"]) == 3
    assert station.db.get_state("manager_status")["value"]["weather"]["ok"] is True


def test_midnight_runs_hourly_then_nightly_in_order(station, fake_clock, tmp_config):
    # start at 23:58:30 local so both the hourly (:00) and the nightly (00:05) happen
    fake_clock._wall = datetime(2026, 9, 3, 23, 58, 30).astimezone().timestamp()
    old = int(fake_clock.now()) - (tmp_config.retention_days.raw + 1) * 86400
    station.db.insert_raw(old, {"co2": 1})  # to be pruned
    station.seed_rows(minutes=2, ahead_minutes=8)
    station.run(8 * 60)
    db = station.db
    hour = int(datetime(2026, 9, 3, 23, 0).astimezone().timestamp())
    assert db.hourly_between(hour, hour + 3600)  # the 23:00 hour was rolled up
    nightly = [e for e in db.recent_events(limit=1000) if e["type"] == "nightly"]
    assert len(nightly) == 1 and nightly[0]["details"]["pruned"]["raw"] >= 1
    assert (tmp_config.paths.database.parent / "airstation.db.bak").exists()
    rollup_ts = max(e["ts"] for e in db.recent_events(limit=1000) if e["type"] == "rollup_catchup")
    assert rollup_ts <= nightly[0]["ts"]
    status = db.get_state("manager_status")["value"]
    assert status["storage"]["last_backup_at"] == nightly[0]["ts"]


def test_collector_warm_up_shows_the_warming_frame_then_numbers(station):
    station.seed_rows(ahead_minutes=4)
    warm = {"left": 50}

    def warming_status():
        station.db.set_state("collector_status", {"stamp": int(clock.now()), "sensors": {
            name: {"available": True, "healthy": True, "warmup_left": warm["left"]}
            for name in ("i2c", "scd41", "sht41", "sps30")}})
        warm["left"] = max(0, warm["left"] - 30)

    station.refresh_collector_status = warming_status
    frames = []
    from shared.render import render
    real_show = station.panel_factory

    station.run(3 * 60)
    docs = station.db.get_state("display_data")["value"]
    assert docs["warming_up"] is False
    warming_lines = [l for l in station.log.path.read_text().splitlines() if " display frame " in l and "warming=1" in l]
    numbers_lines = [l for l in station.log.path.read_text().splitlines() if " display frame " in l and "warming=0" in l]
    assert warming_lines and numbers_lines


def test_display_busy_timeout_recovers_and_paints_full(station):
    from tests.test_display import FakeDriver

    class FailsOnSecondFrame(FakeDriver):
        calls = 0

        def display_image(self, image, mode, auto_sleep=True):
            FailsOnSecondFrame.calls += 1
            if FailsOnSecondFrame.calls == 2:
                raise TimeoutError("UC8253C busy pin timeout")
            return super().display_image(image, mode, auto_sleep)

    def factory():
        station.drivers.append(FailsOnSecondFrame())
        return station.drivers[-1]

    station.panel_factory = factory
    station.seed_rows(ahead_minutes=8)
    station.run(4 * 60)
    assert "display_error" in _types(station.db) and "display_reinit" in _types(station.db)
    assert len(station.drivers) == 2
    assert station.drivers[0].modes == ["full"] and station.drivers[1].modes[0] == "full"
