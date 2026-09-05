"""SCD41 wrapper: start settings, data-ready wait, pressure, calibration safety."""

import sys

import pytest

from collector.sensors import (
    CAL_MAX_DELTA, CAL_MAX_SPREAD, CAL_MIN_RUNTIME, CAL_MIN_SAMPLES, CalibrationRefused, Scd41,
)
from tests.mocks.fake_devices import FakeClock, FakeScd41Device


@pytest.fixture
def scd41(log, tmp_config, monkeypatch):
    """A wrapper whose _open hands out a scriptable fake; clocks under test control."""
    fake = FakeScd41Device()
    clock = FakeClock(start=1000.0)
    monkeypatch.setattr(sys.modules["adafruit_scd4x"], "SCD4X", lambda _i2c: fake)
    sensor = Scd41(object(), tmp_config, log, sleep=clock.sleep, monotonic=clock.monotonic)
    sensor.fake = fake
    sensor.clock = clock
    return sensor


def test_open_applies_the_settings_in_idle_mode_then_starts(scd41):
    assert scd41.ensure(1000) is True
    fake = scd41.fake
    assert fake.reinit_calls == 1 and fake.start_calls == 1
    assert fake.mode == "low_power"  # datasheet 3.8: a value every ~30 s, 3 mA instead of 15
    assert fake.altitude == 296 and fake.temperature_offset == 4.0
    assert fake.self_calibration_enabled is False and scd41.asc is False
    assert scd41.health.id == "001100220033"
    assert scd41.clock.sleeps[0] == 1.0  # the reinit settle


def test_read_returns_the_three_raw_values(scd41):
    scd41.ensure(1000)
    scd41.fake.co2_values = [812.0]
    scd41.fake.temperature = 24.1
    scd41.fake.relative_humidity = 43.2
    assert scd41.read(1010) == {"co2": 812.0, "co2_temp": 24.1, "co2_humid": 43.2}


def test_read_waits_for_data_ready_within_the_deadline(scd41):
    scd41.ensure(1000)
    polls = {"n": 0}
    fake = scd41.fake

    def ready():
        polls["n"] += 1
        return polls["n"] >= 4  # ready on the fourth poll (1.5 s later)

    original = FakeScd41Device.data_ready
    type(fake).data_ready = property(lambda self: ready())
    try:
        result = scd41.read(1010)
    finally:
        type(fake).data_ready = original
    assert result is not None and result["co2"] == 600.0
    assert scd41.clock.sleeps[-3:] == [0.5, 0.5, 0.5]


def test_read_gives_up_after_the_deadline(scd41):
    scd41.ensure(1000)
    scd41.fake.data_ready = False
    before = scd41.clock.monotonic()
    assert scd41.read(1010) is None
    assert 6.0 <= scd41.clock.monotonic() - before <= 6.5


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
    assert scd41.warmup_started_at == now and scd41.recent == []  # a new warm-up, readiness restarts
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


def test_low_power_start_works_with_a_property_and_with_a_method():
    from collector.sensors import _start_low_power

    class Buggy:  # adafruit_scd4x 1.4.13: reading the attribute sends the command
        sent = 0

        @property
        def start_low_periodic_measurement(self):
            self.sent += 1

    class Fixed:
        sent = 0

        def start_low_periodic_measurement(self):
            self.sent += 1

    for cls in (Buggy, Fixed):
        device = cls()
        _start_low_power(device)
        assert device.sent == 1, cls.__name__
