"""SCD41 wrapper: periodic mode, the reset ladder at open, pressure, calibration safety."""

import sys

import pytest

from collector.sensors import (
    CAL_MAX_DELTA, CAL_MAX_SPREAD, CAL_MIN_RUNTIME, CAL_MIN_SAMPLES, CalibrationRefused, Scd41, ready_after,
)
from tests.mocks.fake_devices import FakeClock, FakeScd41Device


@pytest.fixture
def scd41(log, tmp_config, monkeypatch):
    """A wrapper whose _open hands out a scriptable fake; the clock under test control."""
    fake = FakeScd41Device()
    clock = FakeClock(start=1000.0)
    monkeypatch.setattr(sys.modules["adafruit_scd4x"], "SCD4X", lambda _i2c: fake)
    sensor = Scd41(object(), tmp_config, log, sleep=clock.sleep)
    sensor.fake = fake
    sensor.clock = clock
    return sensor


def test_open_applies_the_settings_in_idle_mode_then_starts_periodic(scd41, db):
    assert scd41.ensure(1000) is True
    fake = scd41.fake
    assert fake.reinit_calls == 1 and fake.start_calls == 1 and fake.single_shots == 0
    assert fake.altitude == 296 and fake.temperature_offset == 4.0
    assert fake.self_calibration_enabled is False and scd41.asc is False
    assert scd41.health.id == "001100220033"
    assert scd41.clock.sleeps == [1.0, 1.0]  # before wake_up, and the reinit settle
    init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]["details"]
    assert init["mode"] == "periodic" and init["altitude_m"] == 296 and init["asc"] is False
    assert init["ready_at"] == ready_after(1000) and init["id"] == "001100220033"


def test_read_returns_the_newest_values_or_none_without_waiting(scd41):
    scd41.ensure(1000)
    scd41.fake.co2_values = [812.0]
    scd41.fake.temperature = 24.1
    scd41.fake.relative_humidity = 43.2
    assert scd41.read(1010) == {"co2": 812.0, "co2_temp": 24.1, "co2_humid": 43.2}
    scd41.fake.data_ready = False
    sleeps = len(scd41.clock.sleeps)
    assert scd41.read(1020) is None and len(scd41.clock.sleeps) == sleeps


def test_read_errors_propagate_to_the_sampler(scd41):
    scd41.ensure(1000)
    scd41.fake.raise_on_read = OSError("nack")
    with pytest.raises(OSError):
        scd41.read(1010)


def test_pressure_is_sent_only_when_it_moved_a_hpa(scd41):
    assert scd41.set_ambient_pressure(1013.2) is True     # before init: remembered, applied at start
    scd41.ensure(1000)
    assert scd41.fake.ambient_pressures == [1013]
    assert scd41.set_ambient_pressure(1013.9) is False
    assert scd41.set_ambient_pressure(1014.3) is True
    assert scd41.fake.ambient_pressures == [1013, 1014]
    assert scd41.set_ambient_pressure(None) is False


def test_calibration_refusals(scd41):
    scd41.ensure(1000)
    with pytest.raises(CalibrationRefused, match=f"run for {CAL_MIN_RUNTIME} s"):
        scd41.check_preconditions(1000 + CAL_MIN_RUNTIME - 1, 420)
    now = 1000 + CAL_MIN_RUNTIME
    with pytest.raises(CalibrationRefused, match=f"need {CAL_MIN_SAMPLES}"):
        scd41.check_preconditions(now, 420)
    for i, ppm in enumerate((600, 600, 600 + CAL_MAX_SPREAD + 1)):
        scd41.record_valid(now - 30 + i * 10, ppm)
    with pytest.raises(CalibrationRefused, match="not stable"):
        scd41.check_preconditions(now, 420)
    scd41.recent.clear()
    for i in range(3):
        scd41.record_valid(now - 20 + i * 10, 900)
    with pytest.raises(CalibrationRefused, match=f"more than {CAL_MAX_DELTA} ppm"):
        scd41.check_preconditions(now, 420)
    result = scd41.check_preconditions(now, 420, allow_large_offset=True)
    assert result["average_co2"] == 900 and result["large_offset_allowed"] is True
    assert scd41.calibration_readiness(now) == {
        "runtime_seconds": CAL_MIN_RUNTIME, "sample_count": 3, "average_co2": 900.0, "spread_co2": 0.0}


def test_force_calibration_flow_and_restart(scd41, log):
    scd41.ensure(1000)
    now = 1000 + CAL_MIN_RUNTIME
    for i in range(3):
        scd41.record_valid(now - 20 + i * 10, 430)
    scd41.fake.calibration_result = 12
    result = scd41.force_calibration(now, 420, persist=True)
    assert result["correction_ppm"] == 12 and result["persisted"] is True and result["target_ppm"] == 420
    assert scd41.fake.stop_calls == 2 and scd41.fake.start_calls == 2 and scd41.fake.persist_calls == 1
    assert scd41.ready_at == ready_after(now) and scd41.recent == []  # a new quiet minute
    assert scd41.runtime_seconds(now) == 0


def test_rejected_calibration_still_restarts_measurement(scd41):
    scd41.ensure(1000)
    now = 1000 + CAL_MIN_RUNTIME
    for i in range(3):
        scd41.record_valid(now - 20 + i * 10, 430)
    scd41.fake.calibration_result = 0xFFFF
    with pytest.raises(RuntimeError, match="0xFFFF"):
        scd41.force_calibration(now, 420)
    assert scd41.fake.start_calls == 2 and scd41.fake.persist_calls == 0


def test_recent_window_trims_old_samples(scd41):
    scd41.ensure(1000)
    scd41.record_valid(1000, 500)
    scd41.record_valid(1400, 510)
    assert scd41.calibration_readiness(1400)["sample_count"] == 1


def test_open_sleeps_wakes_resets_and_self_tests_once_per_process(scd41, db):
    scd41.ensure(1000)
    fake = scd41.fake
    assert fake.power_downs == 1 and fake.wake_ups == 1 and fake.reinit_calls == 1 and fake.self_tests == 1
    init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
    assert init["details"]["self_test"] == "ok"
    assert [e["type"] for e in db.recent_events() if e["type"] == "sensor_error"] == []
    scd41.reinit(2000, "test")
    assert fake.self_tests == 1 and fake.reinit_calls == 2 and fake.start_calls == 2 and fake.stop_calls == 3
    again = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
    assert again["details"]["self_test"] == "ok" and again["details"]["ready_at"] == ready_after(2000)


def test_a_failed_self_test_is_an_error_event_and_the_sensor_still_runs(scd41, db):
    scd41.fake.self_test_word = 0x0001  # the sensor's own answer: malfunction
    assert scd41.ensure(1000) is True  # the sensor is used anyway: a verdict, not a refusal
    init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
    assert init["details"]["self_test"] == "fail"
    assert init["details"]["self_test_word"] == 1 and init["details"]["self_test_reason"] == "word"
    errors = [e for e in db.recent_events() if e["type"] == "sensor_error"]
    assert len(errors) == 1 and errors[0]["details"]["self_test"] == "fail" and "malfunction" in errors[0]["message"]
    assert errors[0]["details"]["self_test_word"] == 1 and "supply" in errors[0]["message"]


def test_a_passed_self_test_records_the_zero_word(scd41, db):
    scd41.ensure(1000)
    init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
    assert init["details"]["self_test"] == "ok" and init["details"]["self_test_word"] == 0
    assert init["details"]["self_test_reason"] is None


def test_a_self_test_nack_or_bad_crc_is_named_not_called_a_malfunction(scd41, db):
    scd41.fake.self_test_error = RuntimeError("Could not communicate via I2C, some commands/settings "
                                              "are not available in periodic measurement mode")
    scd41.ensure(1000)
    init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
    assert init["details"]["self_test"] == "fail" and init["details"]["self_test_reason"] == "nack"
    assert init["details"]["self_test_word"] is None
    error = [e for e in db.recent_events() if e["type"] == "sensor_error"][0]
    assert "did not answer" in error["message"] and "malfunction" not in error["message"]
    scd41.fake.self_test_error = RuntimeError("CRC check failed while reading data")
    scd41.opens = 0
    scd41.reinit(2000, "test")
    init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
    assert init["details"]["self_test_reason"] == "crc"


def test_a_driver_without_the_extras_is_reported_not_crashed(scd41, db):
    fake = scd41.fake
    originals = {name: getattr(type(fake), name)
                 for name in ("self_test", "_send_command", "_read_reply", "power_down", "wake_up")}
    for name in originals:  # a driver that is not adafruit_scd4x: the attributes are simply not there
        setattr(type(fake), name, property(lambda self, n=name: (_ for _ in ()).throw(AttributeError(n))))
    try:
        assert scd41.ensure(1000) is True
        init = [e for e in db.recent_events() if e["type"] == "sensor_init"][0]
        assert init["details"]["self_test"] == "unavailable" and fake.power_downs == 0
    finally:
        for name, original in originals.items():
            setattr(type(fake), name, original)
