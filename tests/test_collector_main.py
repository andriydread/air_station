"""The collector program end to end, with fake hardware and a fake clock."""

import signal
import sys

import pytest

from collector.__main__ import Collector, run
from shared import clock
from shared.db import Database
from shared.events import Log
from tests.mocks.fake_devices import FakeRunner, FakeScd41Device, FakeSht41Device, FakeSps30Device


class Station:
    """The collector on fakes; the fake clock drives everything."""

    def __init__(self, tmp_config, fake_clock, monkeypatch, ntp="yes"):
        self.clock = fake_clock
        self.config = tmp_config
        self.db = Database(tmp_config.paths.database, now=clock.now)
        self.log = Log("collector", tmp_config, db=self.db, strict=True, clock=clock.now)
        self.scd = FakeScd41Device()
        self.sht = FakeSht41Device()
        self.sps = FakeSps30Device()
        monkeypatch.setattr(sys.modules["adafruit_scd4x"], "SCD4X", lambda _i2c: self.scd)
        monkeypatch.setattr(sys.modules["adafruit_sht4x"], "SHT4x", lambda _i2c: self.sht)
        self.ntp = FakeRunner()
        self.ntp.results["timedatectl"] = FakeRunner.Completed(stdout=ntp + "\n")
        self.buses = 0

        def bus():
            self.buses += 1
            return object()

        self.bus = bus

    def run(self, seconds, notifier=None):
        passes = int(seconds / 0.2) + 1
        return run(self.config, self.db, self.log, self.bus, notifier, max_passes=passes,
                   ntp_runner=self.ntp, sps30_factory=lambda _i2c: self.sps)

    def close(self):
        self.log.close()
        self.db.close()


@pytest.fixture
def station(tmp_config, fake_clock, monkeypatch):
    s = Station(tmp_config, fake_clock, monkeypatch)
    yield s
    s.close()


def test_three_minutes_of_life(station):
    db = station.db
    reason = station.run(3 * 60)
    assert reason == "max_passes"
    rows = db.raw_between(0, 10**10)
    assert len(rows) == 18 and all(r["recorded_at"] % 10 == 0 for r in rows)
    # warm-up starts at launch; beats are at :10, :20 … so CO2 (60 s) appears
    # on the 6th beat; the dust sensor initialises 1 s later (the SCD41's
    # settle sleep), so its 30 s end just after the 3rd beat → 4th beat
    assert [r["co2"] for r in rows[:7]] == [None] * 5 + [600, 600]
    assert [r["pm25"] for r in rows[:5]] == [None] * 3 + [2.5, 2.5]
    assert all(r["temp"] == 22.5 for r in rows)
    status = db.get_state("collector_status")["value"]
    assert status["sensors"]["scd41"]["healthy"] and status["sample_count"] == 18
    assert status["sensors"]["sps30"]["id"] == "2.2"
    types = [e["type"] for e in db.recent_events()]
    assert types[-1] == "started" and "shutdown" in types and "clock_unsynced" not in types
    assert types.count("warming_up") == 2
    assert station.buses == 1
    assert station.scd.stop_calls == 2  # once at open (defensive), once at shutdown
    assert rows[0]["recorded_at"] == 1_788_436_810  # first aligned beat after a start at :00


def test_a_queued_fan_clean_is_answered(station):
    db = station.db
    db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    station.run(45)
    row = db.recent_commands()[0]
    assert row["status"] == "success" and station.sps.clean_calls == 1
    assert any(e["type"] == "fan_clean" for e in db.recent_events())


def test_unsynced_clock_is_an_event_not_a_stop(tmp_config, fake_clock, monkeypatch):
    s = Station(tmp_config, fake_clock, monkeypatch, ntp="no")
    try:
        s.run(15)
        types = [e["type"] for e in s.db.recent_events()]
        assert "clock_unsynced" in types and len(s.db.raw_between(0, 10**10)) >= 1
        assert fake_clock.monotonic() >= 1000 + 60  # it waited the full minute first
    finally:
        s.close()


def test_stale_running_commands_are_failed_at_start(station):
    db = station.db
    cid = db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    db.claim_pending("collector")  # a previous life claimed it and died
    station.run(5)
    row = {r["id"]: r for r in db.recent_commands()}[cid]
    assert row["status"] == "fail" and row["result"] == {"error": "collector restarted"}


def test_weather_pressure_reaches_the_sensor(station):
    station.db.set_state("last_weather", {"fetched_at": 1, "pressure_hpa": 1003.4, "hourly": {}})
    station.run(5)
    assert station.scd.ambient_pressures[-1] == 1003


def test_sigterm_stops_cleanly(station):
    collector = Collector(station.config, station.db, station.log, station.bus,
                          ntp_runner=station.ntp, sps30_factory=lambda _i2c: station.sps)
    collector.start()
    from shared.loop import Loop, Task
    loop = Loop(station.log, None, collector.tasks())
    loop.install_signal_handlers()
    loop.tasks.append(Task("fire", 1, lambda: signal.raise_signal(signal.SIGTERM), first_run_immediately=False))
    reason = loop.run()
    collector.stop(reason)
    assert reason == "SIGTERM"
    events = station.db.recent_events()
    assert events[0]["type"] == "shutdown" and events[0]["details"]["reason"] == "SIGTERM"
    assert station.sps.stop_calls == 1
