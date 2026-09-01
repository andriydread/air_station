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


# --- Network event debounce ---------------------------------------------------


def _fake_probe(healthy):
    return {
        "checked_at": "2026-09-01T00:00:00+00:00",
        "interface": "wlan0",
        "available": True,
        "operstate": "up" if healthy else "down",
        "carrier": "1" if healthy else None,
        "signal_level_dbm": -60,
        "target_host": "1.1.1.1",
        "target_port": 53,
        "healthy": healthy,
        "latency_ms": 5.0 if healthy else None,
        "error": None if healthy else "OSError: unreachable",
    }


def test_network_blip_stays_out_of_events_but_outage_is_reported(monkeypatch, tmp_path):
    from airmonitor.config import Config

    clock = {"now": 1000.0}
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["now"])
    config = Config(
        database_path=str(tmp_path / "n.db"),
        log_file=str(tmp_path / "n.log"),
        network_event_after_failures=3,
        wifi_recovery_after_failures=0,
    )
    monitor = main_module.AirMonitor(config)

    def connectivity_events():
        return [
            e for e in monitor.database.get_recent_events(limit=100)
            if e["event_type"] == "connectivity_check"
        ]

    try:
        # One failed probe healed by the next: no event at all.
        monkeypatch.setattr(main_module, "probe_network", lambda _c: _fake_probe(False))
        monitor.check_network()
        monkeypatch.setattr(main_module, "probe_network", lambda _c: _fake_probe(True))
        monitor.check_network()
        assert connectivity_events() == []
        # The live state still tracked the blip in real time.
        assert monitor.network_state["healthy"] is True

        # A lasting outage: exactly one warning at the threshold...
        monkeypatch.setattr(main_module, "probe_network", lambda _c: _fake_probe(False))
        for _ in range(5):
            clock["now"] += 30
            monitor.check_network()
        events = connectivity_events()
        assert len(events) == 1
        assert "unhealthy for 3 probes" in events[0]["message"]

        # ...and exactly one recovery notice with the outage duration.
        monkeypatch.setattr(main_module, "probe_network", lambda _c: _fake_probe(True))
        monitor.check_network()
        events = connectivity_events()
        assert len(events) == 2
        assert any("recovered after" in e["message"] for e in events)
    finally:
        monitor.database.close()


# --- Calibration readiness in the status payload (R11) --------------------------


def test_status_payload_carries_calibration_readiness(tmp_path):
    from airmonitor.config import Config

    config = Config(database_path=str(tmp_path / "cr.db"), log_file=str(tmp_path / "cr.log"))
    monitor = main_module.AirMonitor(config)
    try:
        payload = monitor._status_payload()["scd41_calibration"]
        assert payload["sample_count"] == 0  # no sensor yet: empty but shaped
        assert payload["limits"]["min_samples"] == config.calibration_min_samples

        monitor._init_i2c_and_sensors()
        payload = monitor._status_payload()["scd41_calibration"]
        assert payload["limits"]["max_reference_delta"] == config.calibration_max_reference_delta_ppm
        assert "average_co2" in payload and "spread_co2" in payload
    finally:
        monitor.database.close()


# --- Display status glyphs (R9) -----------------------------------------------


def test_display_snapshot_carries_health_status(tmp_path):
    from airmonitor.config import Config

    config = Config(database_path=str(tmp_path / "d.db"), log_file=str(tmp_path / "d.log"))
    monitor = main_module.AirMonitor(config)
    try:
        # Before sensor init the sensor glyph is active, but unknown network/
        # power state counts as healthy, not broken.
        assert monitor._display_status() == {"network": True, "power": True, "sensors": False}

        monitor._init_i2c_and_sensors()
        for sensor in (monitor.scd41, monitor.sht41, monitor.sps30):
            sensor.health.state["healthy"] = True
        monitor.update_display(False)
        assert monitor.last_display_snapshot["status"] == {
            "network": True, "power": True, "sensors": True,
        }

        monitor.network_state["healthy"] = False
        monitor.scd41.health.state["healthy"] = False
        monitor.update_display(False)
        status = monitor.last_display_snapshot["status"]
        assert status["network"] is False
        assert status["sensors"] is False
        assert status["power"] is True
    finally:
        monitor.database.close()


# --- Weather failure debounce -------------------------------------------------


def test_weather_goes_unhealthy_only_after_second_failed_fetch(monkeypatch, tmp_path):
    from airmonitor.config import Config

    config = Config(database_path=str(tmp_path / "w.db"), log_file=str(tmp_path / "w.log"))
    monitor = main_module.AirMonitor(config)

    def weather_events():
        return [
            e for e in monitor.database.get_recent_events(limit=50)
            if e["source"] == "weather"
        ]

    try:
        monkeypatch.setattr(main_module, "get_weather_forecast", lambda *_a: None)
        assert monitor.fetch_weather() is False  # first miss: quiet
        assert weather_events() == []
        assert monitor.fetch_weather() is False  # second miss: now unhealthy
        events = weather_events()
        assert len(events) == 1
        assert "using previous forecast" in events[0]["message"]

        monkeypatch.setattr(
            main_module, "get_weather_forecast", lambda *_a: {"1": ["Now", 20, 10, 5, 1]}
        )
        assert monitor.fetch_weather() is True
        assert monitor.weather_health.state["healthy"] is True
        assert monitor._weather_fail_streak == 0
    finally:
        monitor.database.close()


# --- SCD41 calibration-due reminder ------------------------------------------


def _calibration_events(monitor):
    return [
        e for e in monitor.database.get_recent_events(limit=50)
        if e["event_type"] == "calibration_due"
    ]


def test_calibration_reminder_when_never_calibrated(tmp_path):
    from airmonitor.config import Config

    config = Config(database_path=str(tmp_path / "r.db"), log_file=str(tmp_path / "r.log"))
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()  # ASC off by default
        monitor.check_calibration_age()
        assert len(_calibration_events(monitor)) == 1
        monitor.check_calibration_age()  # once per boot, not per day
        assert len(_calibration_events(monitor)) == 1
    finally:
        monitor.database.close()


def test_calibration_reminder_stays_quiet_when_not_due(tmp_path):
    from airmonitor.config import Config

    base = dict(database_path=str(tmp_path / "q.db"), log_file=str(tmp_path / "q.log"))

    # A recent forced calibration on record: nothing to say.
    monitor = main_module.AirMonitor(Config(**base))
    try:
        monitor._init_i2c_and_sensors()
        monitor.database.set_state("scd41_last_calibration", {"correction": 12})
        monitor.check_calibration_age()
        assert _calibration_events(monitor) == []
    finally:
        monitor.database.close()

    # ASC enabled: the sensor corrects its own baseline.
    monitor = main_module.AirMonitor(Config(**base, scd41_asc_enabled=True))
    try:
        monitor._init_i2c_and_sensors()
        monitor.check_calibration_age()
        assert _calibration_events(monitor) == []
    finally:
        monitor.database.close()

    # Reminder disabled outright.
    monitor = main_module.AirMonitor(Config(**base, scd41_calibration_reminder_days=0))
    try:
        monitor._init_i2c_and_sensors()
        monitor.check_calibration_age()
        assert _calibration_events(monitor) == []
    finally:
        monitor.database.close()


# --- run() cleanup on a setup crash (B10) ------------------------------------


def test_setup_crash_still_runs_shutdown(monkeypatch, tmp_path):
    import sqlite3

    import pytest

    from airmonitor.config import Config

    config = Config(
        database_path=str(tmp_path / "c.db"), log_file=str(tmp_path / "c.log")
    )
    monitor = main_module.AirMonitor(config)

    def exploding_setup():
        raise RuntimeError("first-boot disaster")

    monkeypatch.setattr(monitor, "setup", exploding_setup)
    with pytest.raises(RuntimeError, match="first-boot disaster"):
        monitor.run()
    assert monitor.running is False
    with pytest.raises(sqlite3.ProgrammingError):  # connection really closed
        monitor.database._query("SELECT 1")


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
