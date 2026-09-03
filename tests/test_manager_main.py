"""The manager program end to end: fake panel, fake weather, fake tools, fake clock."""

import io
import json
import signal
import time as _time
from datetime import datetime, timedelta

import pytest

from manager.__main__ import Manager, run
from shared import clock
from shared.db import Database
from shared.events import Log
from tests.mocks.fake_devices import FakeRunner
from tests.test_display import FakeDriver
from tests.test_machine import WIRELESS
from tests.test_network import ROUTE, Net

START = 1_788_436_800.0  # 2026-09-03 12:00:00 UTC, on a minute and a 5-minute boundary


def _payload(now_ts):
    start = datetime.fromtimestamp(now_ts).astimezone().replace(minute=0, second=0, microsecond=0)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(48)]
    return {"timezone": "Europe/Kyiv", "hourly": {
        "time": times, "temperature_2m": [21.0] * 48, "precipitation_probability": [10] * 48,
        "weathercode": [2] * 48, "surface_pressure": [1004.5] * 48, "is_day": [1] * 48}}


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Station:
    def __init__(self, tmp_config, tmp_path, fake_clock, weather_ok=True):
        self.config = tmp_config
        self.db = Database(tmp_config.paths.database, now=clock.now)
        self.log = Log("manager", tmp_config, db=self.db, strict=True, clock=clock.now)
        self.drivers = []
        self.weather_ok = weather_ok
        self.fetches = 0
        self.net = Net()
        self.runner = FakeRunner()
        self.runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x0\n")
        self.runner.results["iw"] = FakeRunner.Completed(stdout="\ttx bitrate: 43.3 MBit/s\n")
        self.spawned = []
        (tmp_path / "route").write_text(ROUTE)
        (tmp_path / "thermal").write_text("48250\n")
        (tmp_path / "loadavg").write_text("0.3 0.2 0.1 1/1 1\n")
        (tmp_path / "meminfo").write_text("MemAvailable: 215040 kB\n")
        (tmp_path / "uptime").write_text("100.0 200.0\n")
        (tmp_path / "wireless").write_text(WIRELESS)
        from manager.machine import Sources
        self.sources = Sources(thermal=str(tmp_path / "thermal"), loadavg=str(tmp_path / "loadavg"),
                               meminfo=str(tmp_path / "meminfo"), uptime=str(tmp_path / "uptime"),
                               wireless=str(tmp_path / "wireless"), data_dir=str(tmp_path))
        self.route_path = str(tmp_path / "route")

    def panel_factory(self):
        self.drivers.append(FakeDriver())
        return self.drivers[-1]

    def opener(self, request, timeout):
        self.fetches += 1
        if not self.weather_ok:
            raise OSError("no route to host")
        return _Response(json.dumps(_payload(clock.now())).encode())

    def spawner(self, argv, **kwargs):
        self.spawned.append(argv)

    def kwargs(self):
        return dict(panel_factory=self.panel_factory, opener=self.opener, runner=self.runner,
                    spawner=self.spawner, connector=self.net.connect, sources=self.sources,
                    route_path=self.route_path)

    def seed_rows(self, minutes=3, ahead_minutes=6):
        """Rows for the last ``minutes`` and, pre-written, for the next ``ahead_minutes``
        (the frame only sees rows up to "now", so they appear to arrive on time)."""
        now = int(clock.now())
        for i in range(-ahead_minutes * 6, minutes * 6):
            self.db.insert_raw(now - 10 * i, {"co2": 800 + i, "temp": 22.0, "humid": 40.0,
                                              "pm25": 4.0, "pm10": 5.0, "pm1": 2.0})
        self.db.set_state("collector_status", {"sensors": {
            name: {"available": True, "healthy": True, "warmup_left": 0} for name in ("i2c", "scd41", "sht41", "sps30")}})

    def refresh_collector_status(self):
        """What the real collector does every 30 s; without it the manager calls it silent."""
        self.db.set_state("collector_status", {"stamp": int(clock.now()), "sensors": {
            name: {"available": True, "healthy": True, "warmup_left": 0}
            for name in ("i2c", "scd41", "sht41", "sps30")}})

    def run(self, seconds, fake_collector=True):
        from shared.loop import Task
        extra = [Task("fake_collector_status", 30, self.refresh_collector_status)] if fake_collector else []
        return run(self.config, self.db, self.log, None, max_passes=int(seconds / 0.2) + 1,
                   extra_tasks=extra, **self.kwargs())

    def close(self):
        self.log.close()
        self.db.close()


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


def test_first_frame_immediately_then_every_minute_full_every_five(station):
    station.seed_rows()
    station.run(5 * 60)
    db = station.db
    doc = db.get_state("display_data")["value"]
    assert doc["values"]["co2"] is not None and doc["aqi_short"] == "Good"
    assert doc["weather"]["stale"] is False and len(doc["weather"]["blocks"]) == 3
    assert doc["glyphs"] == {"wifi": False, "power": False, "sensor": False}
    modes = station.drivers[0].modes
    assert modes == ["full", "partial", "partial", "partial", "partial", "full"]
    assert station.fetches == 1
    assert db.get_state("last_weather")["value"]["pressure_hpa"] == 1004.5
    status = db.get_state("manager_status")["value"]
    assert status["display"]["frames"] == 6 and status["weather"]["ok"] is True
    assert status["wifi"]["router_ok"] is True and status["power"]["available"] is True
    assert db.latest_vitals()["cpu_temp"] == 48.2
    types = [e["type"] for e in db.recent_events()]
    assert types[-1] == "started" and types[0] == "shutdown" and "rollup_catchup" in types


def test_weather_failure_retries_in_two_minutes_and_logs_once(station, db):
    station.weather_ok = False
    station.seed_rows()
    station.run(5 * 60)
    assert station.fetches == 3  # start, +2 min, +4 min
    errors = [e for e in station.db.recent_events() if e["type"] == "weather_error"]
    assert len(errors) == 1
    doc = station.db.get_state("display_data")["value"]
    assert doc["weather"]["stale"] is True


def test_stored_weather_is_used_before_the_first_fetch(station):
    from manager.weather import parse
    station.db.set_state("last_weather", parse(_payload(clock.now()), now=clock.now() - 100))
    station.weather_ok = False
    station.seed_rows()
    station.run(65)
    doc = station.db.get_state("display_data")["value"]
    assert doc["weather"]["stale"] is False and doc["weather"]["blocks"][0]["t_max"] == 21.0


def test_manager_commands_round_trip(station):
    station.seed_rows()
    station.db.queue_command("restart_collector", "dashboard", "manager", {})
    station.run(5)
    row = station.db.recent_commands()[0]
    assert row["status"] == "success"
    assert station.spawned == [["sh", "-c", "sleep 2; exec sudo systemctl restart airstation-collector"]]


def test_silent_collector_gets_restarted_after_five_minutes(station):
    station.db.insert_raw(int(clock.now()) - 400, {"co2": 700})  # the last row is 400 s old
    station.run(65, fake_collector=False)
    types = [e["type"] for e in station.db.recent_events()]
    assert "collector_silent" in types and "collector_restarted" in types
    assert station.spawned[-1][-1].endswith("airstation-collector")
    doc = station.db.get_state("display_data")["value"]
    assert doc["collector_silent"] is True and doc["glyphs"]["sensor"] is True


def test_sigterm_stops_cleanly_and_sleeps_the_panel(station):
    station.seed_rows()
    manager = Manager(station.config, station.db, station.log, **station.kwargs())
    manager.start()
    from shared.loop import Loop, Task
    loop = Loop(station.log, None, manager.tasks())
    loop.install_signal_handlers()
    loop.tasks.append(Task("fire", 3, lambda: signal.raise_signal(signal.SIGTERM), first_run_immediately=False))
    reason = loop.run()
    manager.stop(reason)
    assert reason == "SIGTERM"
    assert station.drivers[0].slept == 1 and station.drivers[0].closed
    assert station.db.recent_events()[0]["type"] == "shutdown"
