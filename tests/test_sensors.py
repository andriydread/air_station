"""Sensor wrapper tests: good data, bad data, and recovery paths (B3)."""

import pytest

import airmonitor.sensors as sensors
from airmonitor.config import Config
from tests.mocks.fake_devices import FakeScd41Device, FakeSht41Device, FakeSps30Device


class StubEvents:
    def __init__(self):
        self.entries = []

    def log(self, _level, source, event_type, message, details=None):
        self.entries.append((source, event_type, message))

    def types(self, source):
        return [event_type for (src, event_type, _msg) in self.entries if src == source]


@pytest.fixture
def events():
    return StubEvents()


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    monkeypatch.setattr(sensors.time, "sleep", lambda _s: None)


# --- SCD41 ------------------------------------------------------------------


def test_scd41_reads_valid_co2(monkeypatch, events):
    device = FakeScd41Device()
    device.co2_values = [612.0]
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    wrapper = sensors.Scd41(object(), Config(), events)
    assert wrapper.read() == 612.0
    assert wrapper.health.state["healthy"] is True


def test_scd41_invalid_streak_triggers_reinit(monkeypatch, events):
    device = FakeScd41Device()
    device.default_co2 = 0.0  # the July 2026 stuck-at-zero failure mode
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    config = Config(scd41_reinit_after_invalid=3)
    wrapper = sensors.Scd41(object(), config, events)
    for _ in range(3):
        assert wrapper.read() is None
    assert device.reinit_calls == 1
    assert "auto_reinit" in events.types("scd41")
    assert wrapper.invalid_streak == 0


def test_scd41_failed_init_recovers_via_ensure(monkeypatch, events):
    boom = RuntimeError("no ack on the bus")

    def failing_factory(_i2c):
        raise boom

    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", failing_factory)
    wrapper = sensors.Scd41(object(), Config(), events)
    assert wrapper.device is None
    assert wrapper.health.state["available"] is False
    assert wrapper.read() is None  # never raises

    # Hardware comes back; next due attempt re-creates the device.
    device = FakeScd41Device()
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    wrapper.ensure()
    assert wrapper.device is None  # backoff not elapsed yet
    wrapper._backoff.next_attempt = 0.0
    wrapper.ensure()
    assert wrapper.device is device
    assert wrapper.health.state["healthy"] is True


def test_scd41_read_exception_reports_failure(monkeypatch, events):
    device = FakeScd41Device()
    device.raise_on_read = OSError("I2C timeout")
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    wrapper = sensors.Scd41(object(), Config(), events)
    assert wrapper.read() is None
    assert wrapper.health.state["healthy"] is False
    assert "read_failed" in events.types("scd41")


def test_scd41_failure_streak_reinitializes(monkeypatch, events):
    device = FakeScd41Device()
    device.raise_on_read = OSError("I2C timeout")
    replacement = FakeScd41Device()
    made = []

    def factory(_i2c):
        made.append(1)
        return device if len(made) == 1 else replacement

    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", factory)
    monkeypatch.setattr(sensors, "READ_FAILURE_REINIT_THRESHOLD", 3)
    wrapper = sensors.Scd41(object(), Config(), events)
    for _ in range(3):
        assert wrapper.read() is None
    assert "auto_reinit" in events.types("scd41")
    assert wrapper.device is replacement
    assert replacement.start_calls == 1
    assert wrapper.read() == 600.0


def test_scd41_failure_streak_resets_on_any_working_transaction(monkeypatch, events):
    device = FakeScd41Device()
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    monkeypatch.setattr(sensors, "READ_FAILURE_REINIT_THRESHOLD", 3)
    wrapper = sensors.Scd41(object(), Config(), events)

    device.raise_on_read = OSError("I2C timeout")
    assert wrapper.read() is None
    assert wrapper.read() is None
    assert wrapper.failure_streak == 2

    # An invalid-value read is still a working bus: the hard-error streak
    # resets while the invalid streak takes over.
    device.raise_on_read = None
    device.default_co2 = 0.0
    assert wrapper.read() is None
    assert wrapper.failure_streak == 0
    assert wrapper.invalid_streak == 1
    assert wrapper.device is device  # never re-created


def test_scd41_calibration_preconditions_refuse_cold_sensor(monkeypatch, events):
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: FakeScd41Device())
    wrapper = sensors.Scd41(object(), Config(), events)
    with pytest.raises(RuntimeError, match="must run"):
        wrapper.check_calibration_preconditions(420)


def test_scd41_rejected_calibration_raises(monkeypatch, events):
    device = FakeScd41Device()
    device.calibration_result = 0xFFFF
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    wrapper = sensors.Scd41(object(), Config(), events)
    with pytest.raises(RuntimeError, match="0xFFFF"):
        wrapper.force_calibration(420, persist=False)


# --- SHT41 ------------------------------------------------------------------


def test_sht41_valid_reading(monkeypatch, events):
    monkeypatch.setattr(sensors.adafruit_sht4x, "SHT4x", lambda _i2c: FakeSht41Device(21.3, 48.2))
    wrapper = sensors.Sht41(object(), events)
    assert wrapper.read() == (21.3, 48.2)


def test_sht41_out_of_range_rejected(monkeypatch, events):
    monkeypatch.setattr(
        sensors.adafruit_sht4x, "SHT4x", lambda _i2c: FakeSht41Device(temperature=130.0)
    )
    wrapper = sensors.Sht41(object(), events)
    assert wrapper.read() is None
    assert wrapper.health.state["healthy"] is False


def test_sht41_failure_streak_reinitializes(monkeypatch, events):
    device = FakeSht41Device()
    device.raise_on_read = OSError("bus glitch")
    replacement = FakeSht41Device(20.0, 50.0)
    made = []

    def factory(_i2c):
        made.append(1)
        return device if len(made) == 1 else replacement

    monkeypatch.setattr(sensors.adafruit_sht4x, "SHT4x", factory)
    monkeypatch.setattr(sensors, "READ_FAILURE_REINIT_THRESHOLD", 3)
    wrapper = sensors.Sht41(object(), events)
    for _ in range(3):
        assert wrapper.read() is None
    assert "auto_reinit" in events.types("sht41")
    assert wrapper.device is replacement
    assert wrapper.read() == (20.0, 50.0)


# --- SPS30 ------------------------------------------------------------------


def test_sps30_valid_reading(monkeypatch, events):
    device = FakeSps30Device()
    monkeypatch.setattr(sensors, "SPS30", lambda _i2c: device)
    wrapper = sensors.Sps30(object(), Config(), events)
    values = wrapper.read()
    assert values == {"pm1": 1.1, "pm25": 2.5, "pm4": 3.0, "pm10": 4.2, "tps": 0.6}


def test_sps30_negative_value_rejected(monkeypatch, events):
    device = FakeSps30Device()
    device.values["pm25"] = -0.5
    monkeypatch.setattr(sensors, "SPS30", lambda _i2c: device)
    wrapper = sensors.Sps30(object(), Config(), events)
    assert wrapper.read() is None
    assert wrapper.health.state["healthy"] is False


def test_sps30_failure_streak_reinitializes(monkeypatch, events):
    device = FakeSps30Device()
    device.raise_on_read = OSError("CRC mismatch")
    replacement = FakeSps30Device()
    made = []

    def factory(_i2c):
        made.append(1)
        return device if len(made) == 1 else replacement

    monkeypatch.setattr(sensors, "SPS30", factory)
    monkeypatch.setattr(sensors, "READ_FAILURE_REINIT_THRESHOLD", 3)
    wrapper = sensors.Sps30(object(), Config(), events)
    for _ in range(3):
        assert wrapper.read() is None
    assert "auto_reinit" in events.types("sps30")
    assert wrapper.device is replacement
    assert replacement.start_calls == 1
    assert wrapper.read() is not None


def test_sps30_manual_clean_rate_limited(monkeypatch, events):
    device = FakeSps30Device()
    monkeypatch.setattr(sensors, "SPS30", lambda _i2c: device)
    wrapper = sensors.Sps30(object(), Config(), events)
    wrapper.force_clean()
    with pytest.raises(RuntimeError, match="rate-limited"):
        wrapper.force_clean()
    assert device.clean_calls == 1


# --- Bus-level recovery in the collector (main.ensure_hardware) --------------


def test_collector_recovers_i2c_bus_after_failed_boot(monkeypatch, tmp_path):
    import busio

    import main as main_module

    def failing_i2c(_scl, _sda):
        raise RuntimeError("bus stuck low")

    monkeypatch.setattr(busio, "I2C", failing_i2c)
    config = Config(database_path=str(tmp_path / "m.db"), log_file=str(tmp_path / "m.log"))
    monitor = main_module.AirMonitor(config)
    try:
        monitor._init_i2c_and_sensors()
        assert monitor.i2c is None
        assert monitor.scd41 is None

        # Bus comes back, but the backoff window hasn't elapsed yet.
        working = object()
        monkeypatch.setattr(busio, "I2C", lambda _scl, _sda: working)
        monitor.ensure_hardware()
        assert monitor.i2c is None

        monitor._i2c_backoff.next_attempt = 0.0
        monitor.ensure_hardware()
        assert monitor.i2c is working
        assert monitor.scd41 is not None
        assert monitor.sht41 is not None
        assert monitor.sps30 is not None
    finally:
        monitor.database.close()


# --- R4 additions: altitude, ambient capture, temp offset -------------------


def test_scd41_altitude_set_before_measurement(monkeypatch, events):
    device = FakeScd41Device()
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    sensors.Scd41(object(), Config(scd41_altitude_m=500), events)
    assert device.altitude == 500


def test_scd41_captures_own_ambient_readings(monkeypatch, events):
    device = FakeScd41Device()
    device.temperature = 24.5
    device.relative_humidity = 38.0
    monkeypatch.setattr(sensors.adafruit_scd4x, "SCD4X", lambda _i2c: device)
    wrapper = sensors.Scd41(object(), Config(), events)
    assert wrapper.last_temperature is None
    wrapper.read()
    assert wrapper.last_temperature == 24.5
    assert wrapper.last_humidity == 38.0


def test_sht41_temp_offset_applied(monkeypatch, events):
    monkeypatch.setattr(
        sensors.adafruit_sht4x, "SHT4x", lambda _i2c: FakeSht41Device(22.5, 45.0)
    )
    wrapper = sensors.Sht41(object(), events, temp_offset=-1.5)
    assert wrapper.read() == (21.0, 45.0)
