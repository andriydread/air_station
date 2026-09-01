"""CommandProcessor tests: every handler's happy path and failure path."""

import time

import pytest

import airmonitor.sensors as sensors
from airmonitor.commands import CommandProcessor, as_bool, as_int
from airmonitor.config import Config
from airmonitor.storage import AirMonitorDatabase
from tests.mocks.fake_devices import FakeScd41Device, FakeSps30Device


class StubEvents:
    def log(self, *_args, **_kwargs):
        pass


class StubApp:
    """The slice of AirMonitor that CommandProcessor touches."""

    def __init__(self, database):
        self.database = database
        self.events = StubEvents()
        self.scd41 = None
        self.sps30 = None
        self.redraws = []

    def redraw_display(self, full_refresh):
        self.redraws.append(full_refresh)

    def publish_status(self):
        pass


@pytest.fixture
def app(tmp_path):
    database = AirMonitorDatabase(str(tmp_path / "cmd.db"))
    stub = StubApp(database)
    yield stub
    database.close()


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    monkeypatch.setattr(sensors.time, "sleep", lambda _s: None)


def run_command(app, name, payload=None):
    command_id = app.database.queue_command(name, payload or {})
    CommandProcessor(app).process_pending()
    for row in app.database.get_recent_commands():
        if row["id"] == command_id:
            return row
    raise AssertionError("command row disappeared")


def _warmed_scd41(monkeypatch, events):
    device = FakeScd41Device()
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    wrapper = sensors.Scd41(object(), Config(), events)
    now = time.monotonic()
    wrapper.measurement_started_at = now - 3600
    wrapper.recent_valid_samples.extend([(now - 5, 420.0), (now - 3, 421.0), (now - 1, 419.0)])
    return wrapper, device


def test_as_bool_and_as_int_helpers():
    assert as_bool("yes") is True
    assert as_bool("OFF") is False
    assert as_bool(None, default=True) is True
    with pytest.raises(ValueError):
        as_bool("maybe")
    assert as_int("42", "n") == 42
    with pytest.raises(ValueError):
        as_int(True, "n")
    with pytest.raises(ValueError):
        as_int("nan", "n")


def test_display_refresh_commands(app):
    assert run_command(app, "display_full_refresh")["status"] == "succeeded"
    assert run_command(app, "display_partial_refresh")["status"] == "succeeded"
    assert app.redraws == [True, False]


def test_unknown_command_fails_cleanly(app):
    row = run_command(app, "make_coffee")
    assert row["status"] == "failed"
    assert "Unsupported command" in row["result"]["error"]


def test_missing_sensor_fails_cleanly(app):
    row = run_command(app, "sps30_force_clean")
    assert row["status"] == "failed"
    assert "not initialized" in row["result"]["error"]


def test_sps30_force_clean(app, monkeypatch):
    device = FakeSps30Device()
    monkeypatch.setattr(sensors, "SPS30", lambda _i2c: device)
    app.sps30 = sensors.Sps30(object(), Config(), StubEvents())
    row = run_command(app, "sps30_force_clean")
    assert row["status"] == "succeeded"
    assert device.clean_calls == 1


def test_sps30_interval_validation(app, monkeypatch):
    device = FakeSps30Device()
    monkeypatch.setattr(sensors, "SPS30", lambda _i2c: device)
    app.sps30 = sensors.Sps30(object(), Config(), StubEvents())
    ok = run_command(app, "sps30_set_auto_cleaning_interval", {"seconds": 86400})
    assert ok["status"] == "succeeded"
    assert device.auto_cleaning_interval == 86400
    bad = run_command(app, "sps30_set_auto_cleaning_interval", {"seconds": -1})
    assert bad["status"] == "failed"


def test_scd41_calibration_requires_confirmation(app, monkeypatch):
    app.scd41, _device = _warmed_scd41(monkeypatch, StubEvents())
    row = run_command(app, "scd41_force_calibration", {"target_co2": 420})
    assert row["status"] == "failed"
    assert "confirmation" in row["result"]["error"]


def test_scd41_calibration_happy_path(app, monkeypatch):
    app.scd41, device = _warmed_scd41(monkeypatch, StubEvents())
    row = run_command(
        app, "scd41_force_calibration",
        {"target_co2": 420, "confirmed": True, "persist": True},
    )
    assert row["status"] == "succeeded"
    assert row["result"]["correction"] == device.calibration_result
    assert device.persist_calls == 1
    stored = app.database.get_state("scd41_last_calibration")
    assert stored["value"]["target_co2"] == 420


def test_scd41_calibration_refused_when_unstable(app, monkeypatch):
    wrapper, _device = _warmed_scd41(monkeypatch, StubEvents())
    now = time.monotonic()
    wrapper.recent_valid_samples.clear()
    wrapper.recent_valid_samples.extend([(now - 5, 400.0), (now - 1, 900.0), (now, 405.0)])
    app.scd41 = wrapper
    row = run_command(
        app, "scd41_force_calibration", {"target_co2": 420, "confirmed": True}
    )
    assert row["status"] == "failed"
    assert "stable" in row["result"]["error"]


def test_scd41_calibration_far_from_target(app, monkeypatch):
    wrapper, device = _warmed_scd41(monkeypatch, StubEvents())
    now = time.monotonic()
    wrapper.recent_valid_samples.clear()
    wrapper.recent_valid_samples.extend([(now - 5, 1460.0), (now - 3, 1465.0), (now - 1, 1463.0)])
    app.scd41 = wrapper

    blocked = run_command(
        app, "scd41_force_calibration", {"target_co2": 420, "confirmed": True}
    )
    assert blocked["status"] == "failed"
    assert "drift override" in blocked["result"]["error"]

    allowed = run_command(
        app, "scd41_force_calibration",
        {"target_co2": 420, "confirmed": True, "allow_large_offset": True},
    )
    assert allowed["status"] == "succeeded"
    assert allowed["result"]["correction"] == device.calibration_result
    assert allowed["result"]["validation"]["large_offset_allowed"] is True


def test_scd41_set_asc(app, monkeypatch):
    app.scd41, device = _warmed_scd41(monkeypatch, StubEvents())
    row = run_command(app, "scd41_set_asc", {"enabled": True})
    assert row["status"] == "succeeded"
    assert row["result"]["enabled"] is True
    assert device.self_calibration_enabled is True


def test_system_commands_spawn_deferred_actions(app, monkeypatch):
    spawned = []
    processor = CommandProcessor(app)
    processor.spawn = lambda args: spawned.append(args)
    monkeypatch.setattr(
        "airmonitor.commands.shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "systemctl" else None,
    )
    monkeypatch.setattr("airmonitor.commands.os.path.exists", lambda p: True)

    for name in ("system_restart_collector", "system_restart_web", "system_reboot"):
        command_id = app.database.queue_command(name, {"confirmed": True})
        processor.process_pending()
        row = next(r for r in app.database.get_recent_commands() if r["id"] == command_id)
        assert row["status"] == "succeeded", row["result"]

    assert len(spawned) == 3
    assert all(args[0] == "sh" and "sleep 2 && sudo -n" in args[2] for args in spawned)
    assert "systemctl restart airmonitor" in spawned[0][2]
    assert "systemctl restart airmonitor-web" in spawned[1][2]
    assert "reboot" in spawned[2][2]


def test_system_commands_refuse_without_confirmation(app):
    row = run_command(app, "system_reboot", {})
    assert row["status"] == "failed"
    assert "confirmation" in row["result"]["error"]


def test_system_command_spawn_failure_marks_row_failed(app, monkeypatch):
    ordinary_id = None
    command_id = app.database.queue_command("system_reboot", {"confirmed": True})
    ordinary_id = app.database.queue_command("display_full_refresh", {})

    processor = CommandProcessor(app)

    def broken_spawn(*_a, **_k):
        raise OSError("sh: not found")

    processor.spawn = broken_spawn
    processor.process_pending()

    rows = {row["id"]: row for row in app.database.get_recent_commands()}
    assert rows[command_id]["status"] == "failed"
    assert "sh: not found" in rows[command_id]["result"]["error"]
    # A broken spawn must not abort the rest of the queue.
    assert rows[ordinary_id]["status"] == "succeeded"


def test_missing_systemctl_fails_cleanly(app, monkeypatch):
    import airmonitor.commands as commands_module

    monkeypatch.setattr(commands_module.shutil, "which", lambda _n: None)
    monkeypatch.setattr(commands_module.os.path, "exists", lambda _p: False)
    row = run_command(app, "system_restart_collector", {"confirmed": True})
    assert row["status"] == "failed"
    assert "not found" in row["result"]["error"]


def test_corrupt_payload_degrades_to_empty_object(app):
    command_id = app.database.queue_command("display_full_refresh", {})
    with app.database._lock:
        app.database._connection.execute(
            "UPDATE commands SET payload='{broken' WHERE id=?", (command_id,)
        )
    CommandProcessor(app).process_pending()
    rows = {row["id"]: row for row in app.database.get_recent_commands()}
    # _from_json falls back to {} — a torn payload write must not crash the poll.
    assert rows[command_id]["status"] == "succeeded"
