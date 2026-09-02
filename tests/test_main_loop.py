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
        scd41_warmup_seconds=0,  # this test is about the rate guard, not warm-up
    )
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()  # conftest fakes: all sensors healthy
        monitor.collect_sample()  # baseline sample, everything accepted

        for i in range(5):
            clock["now"] += 10
            # Alternate wildly (but inside the sensor's 40'000 ppm output
            # range) so the rate guard flags every co2 sample.
            monitor.scd41.device.CO2 = 30000.0 if i % 2 == 0 else 600.0
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


# --- NaN never reaches storage, state JSON, or the display ----------------------


def test_nan_reading_never_poisons_state_or_display(tmp_path, monkeypatch):
    import json as _json

    import pytest

    import airmonitor.sensors as sensors_module
    from airmonitor.config import Config
    from tests.mocks.fake_devices import FakeSps30Device
    from utils.display import create_display_image

    monkeypatch.setattr(sensors_module, "SPS30", lambda _i2c: FakeSps30Device())
    config = Config(database_path=str(tmp_path / "nan.db"), log_file=str(tmp_path / "nan.log"))
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()
        monitor.collect_sample()  # good baseline
        monitor.sps30.device.values["pm25"] = float("nan")
        monitor.collect_sample()

        # The state row must be STRICT json — json.dumps writes literal NaN,
        # which browsers refuse to parse and which would kill the live tab.
        raw = monitor.database._query(
            "SELECT value FROM state WHERE key='latest_measurements'"
        )[0]["value"]
        parsed = _json.loads(raw, parse_constant=lambda c: pytest.fail(f"non-finite {c} in state JSON"))
        assert parsed["pm25"] is None or parsed["pm25"] == 2.5

        # And a frame renders from whatever snapshot resulted.
        monitor.update_display(False)
        image = create_display_image(416, 240, monitor.last_display_snapshot, None)
        assert image.size == (416, 240)
    finally:
        monitor.database.close()


# --- Cached forecast survives restarts ------------------------------------------


def test_restart_reuses_stored_forecast_for_first_display_frame(tmp_path):
    from airmonitor.config import Config
    from airmonitor.storage import AirMonitorDatabase

    db_path = str(tmp_path / "wcache.db")
    seed = AirMonitorDatabase(db_path)
    seed.set_state("latest_weather", {1: ["Now", 20, 10, 5, 1]})
    seed.close()

    config = Config(database_path=db_path, log_file=str(tmp_path / "wcache.log"))
    monitor = main_module.AirMonitor(config)
    try:
        # JSON round-trip stringifies the keys; the renderer checks both forms.
        assert monitor.weather == {"1": ["Now", 20, 10, 5, 1]}
        monitor.update_display(False)
        assert monitor.last_display_snapshot["1"] == ["Now", 20, 10, 5, 1]
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


# --- Display tick cadence -------------------------------------------------------


def test_display_tick_full_refresh_cadence(monkeypatch, tmp_path):
    from airmonitor.config import Config

    clock = {"now": 1000.0}
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["now"])
    config = Config(
        database_path=str(tmp_path / "t.db"), log_file=str(tmp_path / "t.log"),
        partial_update_interval=60, full_update_interval=300,
    )
    monitor = main_module.AirMonitor(config)
    submitted = []
    monitor.display_worker.submit = lambda _snap, full: submitted.append(full)
    try:
        monitor._next_full_refresh = clock["now"]
        for _ in range(10):
            monitor._display_tick()
            clock["now"] += 60
        # one full refresh per 300s window, partial otherwise
        assert submitted.count(True) == 2
        assert submitted[0] is True

        # After a long stall, exactly ONE catch-up full refresh, not N.
        submitted.clear()
        clock["now"] += 100 * 300
        monitor._display_tick()
        assert submitted == [True]
        clock["now"] += 60
        monitor._display_tick()
        assert submitted == [True, False]
    finally:
        monitor.database.close()


# --- Disk-space health ----------------------------------------------------------


def test_low_disk_space_goes_unhealthy_and_recovers(monkeypatch, tmp_path):
    from collections import namedtuple

    from airmonitor.config import Config

    Stat = namedtuple("Stat", "f_bavail f_frsize")
    config = Config(
        database_path=str(tmp_path / "disk.db"), log_file=str(tmp_path / "disk.log"),
        min_free_disk_mb=200,
    )
    monitor = main_module.AirMonitor(config)
    try:
        monkeypatch.setattr(main_module.os, "statvfs", lambda _p: Stat(100_000, 4096))
        monitor.check_disk()  # ~390 MB free: fine
        assert monitor.storage_health.state["healthy"] is True
        assert monitor.storage_health.state["free_bytes"] == 100_000 * 4096

        monkeypatch.setattr(main_module.os, "statvfs", lambda _p: Stat(10_000, 4096))
        monitor.check_disk()  # ~39 MB free: below the 200 MB threshold
        assert monitor.storage_health.state["healthy"] is False
        assert "Low disk space" in monitor.storage_health.state["last_error"]
        assert monitor._status_payload()["sensors"]["storage"]["healthy"] is False

        monkeypatch.setattr(main_module.os, "statvfs", lambda _p: Stat(200_000, 4096))
        monitor.check_disk()
        assert monitor.storage_health.state["healthy"] is True
    finally:
        monitor.database.close()


# --- Nightly maintenance: integrity check + rotating backup (R12) ---------------


def test_prune_task_writes_and_rotates_backups(tmp_path):
    import os as os_module

    from airmonitor.config import Config
    from airmonitor.storage import AirMonitorDatabase

    db_path = str(tmp_path / "night.db")
    config = Config(database_path=db_path, log_file=str(tmp_path / "night.log"))
    monitor = main_module.AirMonitor(config)
    heartbeats = []
    monitor.notifier.heartbeat = lambda: heartbeats.append(1)
    try:
        monitor.database.insert_measurement({"co2": 700, "temp": 21.0, "humid": 45.0})
        monitor.prune_database()
        assert os_module.path.exists(db_path + ".bak")
        assert heartbeats  # watchdog fed between maintenance steps
        monitor.check_disk()  # the 5-min task owns the health verdict
        assert monitor.storage_health.state["healthy"] is True

        # Second night: previous backup rotates to .bak.1.
        monitor.database.insert_measurement({"co2": 800, "temp": 21.0, "humid": 45.0})
        monitor.prune_database()
        assert os_module.path.exists(db_path + ".bak.1")

        # The newest backup is a standalone, readable copy with both rows.
        copy = AirMonitorDatabase(db_path + ".bak")
        try:
            assert copy.database_stats()["measurements"] == 2
        finally:
            copy.close()

        events = [e["event_type"] for e in monitor.database.get_recent_events(limit=50)]
        assert events.count("backup_written") == 2
    finally:
        monitor.database.close()


def test_backup_skipped_when_disk_headroom_is_too_small(monkeypatch, tmp_path):
    from collections import namedtuple

    from airmonitor.config import Config

    Stat = namedtuple("Stat", "f_bavail f_frsize")
    config = Config(
        database_path=str(tmp_path / "full.db"), log_file=str(tmp_path / "full.log")
    )
    monitor = main_module.AirMonitor(config)
    try:
        monitor.database.insert_measurement({"co2": 700})
        monkeypatch.setattr(main_module.os, "statvfs", lambda _p: Stat(1, 4096))
        monitor.backup_database()
        assert not main_module.os.path.exists(str(tmp_path / "full.db") + ".bak")
        events = [e["event_type"] for e in monitor.database.get_recent_events(limit=20)]
        assert "backup_skipped" in events
    finally:
        monitor.database.close()


def test_integrity_failure_is_sticky_until_next_clean_check(monkeypatch, tmp_path):
    from airmonitor.config import Config
    from airmonitor.storage import AirMonitorDatabase

    config = Config(
        database_path=str(tmp_path / "corrupt.db"), log_file=str(tmp_path / "corrupt.log"),
        min_free_disk_mb=0,  # disk-space branch stays quiet in this test
    )
    monitor = main_module.AirMonitor(config)
    try:
        monkeypatch.setattr(
            AirMonitorDatabase, "integrity_check",
            lambda _self: ["*** in database main *** page 7: btree corruption"],
        )
        monitor._check_integrity()
        assert monitor.storage_health.state["healthy"] is False
        events = [e["event_type"] for e in monitor.database.get_recent_events(limit=20)]
        assert "integrity_check_failed" in events

        # The 5-minute disk check must NOT declare the database healthy again.
        monitor.check_disk()
        assert monitor.storage_health.state["healthy"] is False
        assert "Integrity" in monitor.storage_health.state["last_error"]

        # A later clean check clears the sticky flag.
        monkeypatch.setattr(AirMonitorDatabase, "integrity_check", lambda _self: [])
        monitor._check_integrity()
        monitor.check_disk()
        assert monitor.storage_health.state["healthy"] is True
    finally:
        monitor.database.close()


# --- Warm-up flagging (datasheet pass 2026-09-02) ------------------------------


def test_warmup_readings_are_flagged_not_recorded(monkeypatch, tmp_path):
    """Right after a (re)start the SCD41/SPS30 numbers are kept as flagged raw
    values: not in the metric columns, not the rate guard's baseline."""
    from airmonitor.config import Config
    import airmonitor.sensors as sensors_module
    from tests.mocks.fake_devices import FakeSps30Device

    clock = {"now": 1000.0}
    monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["now"])
    assert sensors_module.time is main_module.time  # one clock for all modules
    # conftest's import-time I2C stub can't answer the CRC-checked SPS30
    # driver; give the wrapper a scriptable device instead.
    monkeypatch.setattr(sensors_module, "SPS30", lambda _i2c: FakeSps30Device())
    config = Config(
        database_path=str(tmp_path / "w.db"), log_file=str(tmp_path / "w.log"),
        scd41_warmup_seconds=60, sps30_warmup_seconds=30,
    )
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()  # started at t=1000
        monitor.scd41.device.CO2 = 1400.0  # the classic post-boot artefact
        clock["now"] += 10
        monitor.collect_sample()
        latest = monitor.database.get_latest_measurement()
        assert latest["co2"] is None and latest["temp"] is not None  # SHT41 has no warm-up
        assert latest["flags"]["co2"]["value"] == 1400.0
        assert "warm-up" in latest["flags"]["co2"]["reason"]
        assert latest["pm25"] is None and latest["flags"]["pm25"]["value"] == 2.5
        assert monitor.readings.values.get("co2") is None
        assert not monitor.database.get_recent_events(limit=50, source="quality")

        clock["now"] += 25  # t+35: SPS30 settled, SCD41 still warming
        monitor.collect_sample()
        latest = monitor.database.get_latest_measurement()
        assert latest["pm25"] == 2.5 and latest["co2"] is None

        clock["now"] += 30  # t+65: SCD41 settled; first real value = 460
        monitor.scd41.device.CO2 = 460.0
        monitor.collect_sample()
        latest = monitor.database.get_latest_measurement()
        # 1400 -> 460 would have been a rate-guard flag had 1400 been the baseline
        assert latest["co2"] == 460 and latest["flags"] is None
        assert not monitor.database.get_recent_events(limit=50, source="quality")
    finally:
        monitor.database.close()


def test_sps30_status_task_marks_sensor_unhealthy(monkeypatch, tmp_path):
    from airmonitor.config import Config
    import airmonitor.sensors as sensors_module
    from tests.mocks.fake_devices import FakeSps30Device

    monkeypatch.setattr(sensors_module, "SPS30", lambda _i2c: FakeSps30Device())
    config = Config(database_path=str(tmp_path / "f.db"), log_file=str(tmp_path / "f.log"))
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()
        monitor.sps30.device.status = {"raw": 1 << 5, "speed_warning": False, "laser_error": True, "fan_error": False}
        monitor.check_sps30_status()
        assert monitor.sps30.health.state["healthy"] is False
        payload = monitor._status_payload()
        assert payload["sensors"]["sps30"]["device_status"]["laser_error"] is True
        assert monitor._display_status()["sensors"] is False  # glyph on the e-paper
    finally:
        monitor.database.close()
