"""Main-loop building blocks: PeriodicTask, LatestReadings, SampleBuffer."""

import logging

import main as main_module
from main import LatestReadings, PeriodicTask, SampleBuffer


class StubEvents:
    def __init__(self):
        self.entries = []

    def log(self, level, source, event_type, message, details=None):
        self.entries.append((level, source, event_type))

    def types(self):
        return [event_type for (_lvl, _src, event_type) in self.entries]


# --- PeriodicTask -----------------------------------------------------------


def test_periodic_task_runs_only_when_due():
    calls = []
    task = PeriodicTask("t", 10, lambda: calls.append(1))
    task.next_run = 100.0
    task.run_if_due(99.0, StubEvents())
    assert calls == []
    task.run_if_due(100.0, StubEvents())
    assert calls == [1]
    assert task.next_run == 110.0


def test_periodic_task_catches_up_after_long_stall():
    task = PeriodicTask("t", 10, lambda: None)
    task.next_run = 100.0
    task.run_if_due(147.0, StubEvents())  # stalled 4+ intervals
    assert task.next_run == 150.0  # runs once, not four times, and realigns


def test_periodic_task_isolates_failures():
    events = StubEvents()

    def boom():
        raise RuntimeError("sensor exploded")

    task = PeriodicTask("t", 10, boom)
    task.next_run = 100.0
    task.run_if_due(100.0, events)  # must not raise
    assert events.types() == ["task_failed"]
    assert task.next_run == 110.0


# --- LatestReadings ---------------------------------------------------------


def test_latest_readings_snapshot_and_aging(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["now"])
    readings = LatestReadings(max_age_seconds=45, events=StubEvents())
    readings.record("co2", 600)
    readings.record("temp", 21.5)

    snapshot = readings.fresh_snapshot()
    assert snapshot["co2"] == 600
    assert snapshot["temp"] == 21.5
    assert snapshot["pm25"] is None
    assert snapshot["timestamp"] is not None

    clock["now"] += 46  # both readings now stale
    snapshot = readings.fresh_snapshot()
    assert snapshot["co2"] is None
    assert snapshot["temp"] is None
    assert snapshot["timestamp"] is None


def test_stale_reported_once_then_recovery_logged(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["now"])
    events = StubEvents()
    readings = LatestReadings(max_age_seconds=45, events=events)
    readings.record("co2", 600)

    clock["now"] += 10
    readings.report_stale("co2", "scd41")  # not old enough yet
    assert events.types() == []

    clock["now"] += 40
    readings.report_stale("co2", "scd41")
    readings.report_stale("co2", "scd41")  # second call must not re-log
    assert events.types() == ["measurement_stale"]

    readings.record("co2", 610)
    assert events.types() == ["measurement_stale", "measurement_recovered"]


def test_stale_never_reported_for_metric_never_seen():
    events = StubEvents()
    readings = LatestReadings(max_age_seconds=45, events=events)
    readings.report_stale("co2", "scd41")
    assert events.entries == []


# --- collect_sample + quality guards (B9) ------------------------------------


def test_flagged_stream_raises_stale_alarm(monkeypatch, tmp_path):
    """A sensor whose readings keep getting flagged feeds nothing into the
    history — that must raise measurement_stale like a silent sensor does."""
    from airmonitor.config import Config

    clock = {"now": 1000.0}
    # main, quality and sensors share the one time module; patch it once.
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["now"])
    config = Config(
        database_path=str(tmp_path / "s.db"),
        log_file=str(tmp_path / "s.log"),
        measurement_max_age=45,
    )
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()  # conftest fakes: all sensors healthy
        monitor.collect_sample()  # baseline sample, everything accepted

        for i in range(5):
            clock["now"] += 10
            # Alternate wildly so the rate guard flags every co2 sample.
            monitor.scd41.device.CO2 = 60000.0 if i % 2 == 0 else 600.0
            monitor.collect_sample()

        stale = [
            e for e in monitor.database.get_recent_events(limit=200)
            if e["event_type"] == "measurement_stale"
        ]
        assert len(stale) == 1
        assert stale[0]["source"] == "quality"
        assert stale[0]["details"]["metric"] == "co2"  # temp/humid kept flowing
        flagged = monitor.database.get_recent_flagged()
        assert flagged and "co2" in flagged[0]["flags"]
    finally:
        monitor.database.close()


# --- SampleBuffer -----------------------------------------------------------


def test_sample_buffer_averages_and_rounds_per_metric():
    buffer = SampleBuffer()
    for value in (600.0, 601.0):
        buffer.add("co2", value)
    for value in (21.0, 21.4):
        buffer.add("temp", value)
    buffer.add("pm25", 3.14159)
    averages = buffer.take_averages()
    assert averages["co2"] == 600  # int(round(600.5)) banker's-rounds to 600
    assert averages["temp"] == 21.2
    assert averages["pm25"] == 3.14
    assert averages["humid"] is None


def test_sample_buffer_window_resets_after_take():
    buffer = SampleBuffer()
    buffer.add("co2", 500.0)
    assert buffer.take_averages()["co2"] == 500
    assert buffer.take_averages()["co2"] is None
