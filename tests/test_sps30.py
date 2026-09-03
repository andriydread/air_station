"""SPS30 wrapper: start, auto-clean disabled, read mapping, blanking, status word."""

import pytest

from collector.sensors import FAN_CLEAN_BLANK, FAN_CLEAN_COOLDOWN, Sps30
from tests.mocks.fake_devices import FakeSps30Device


@pytest.fixture
def sps30(log, tmp_config):
    fake = FakeSps30Device()
    sensor = Sps30(object(), tmp_config, log, device_factory=lambda _i2c: fake)
    sensor.fake = fake
    return sensor


def test_open_wakes_starts_and_disables_the_sensors_own_timer(sps30):
    assert sps30.ensure(0) is True
    fake = sps30.fake
    assert fake.wakeup_calls == 1 and fake.start_calls == 1
    assert fake.interval_writes == [0] and fake.auto_cleaning_interval == 0
    assert sps30.health.id == "2.2" and sps30.firmware == (2, 2)
    assert sps30.warmup_left(0) == 30


def test_timer_already_off_is_left_alone(log, tmp_config):
    fake = FakeSps30Device()
    fake._auto_cleaning_interval = 0
    sensor = Sps30(object(), tmp_config, log, device_factory=lambda _i2c: fake)
    sensor.ensure(0)
    assert fake.interval_writes == []


def test_read_maps_driver_keys_to_row_columns_and_extras(sps30):
    sps30.ensure(0)
    row, extra = sps30.read(40)
    assert row == {"pm1": 1.1, "pm25": 2.5, "pm10": 4.2, "tps": 0.6, "nc05": 7.5, "nc1": 8.6, "nc25": 8.8}
    assert extra == {"pm4": 3.0, "nc4": 8.9, "nc10": 8.9}


def test_read_none_when_not_ready_and_errors_propagate(sps30):
    assert sps30.read(0) is None
    sps30.ensure(0)
    sps30.fake.data_ready = False
    assert sps30.read(40) is None
    sps30.fake.data_ready = True
    sps30.fake.raise_on_read = OSError("crc")
    with pytest.raises(OSError):
        sps30.read(41)


def test_fan_clean_blanks_readings_and_rate_limits(sps30, db):
    sps30.ensure(0)
    result = sps30.force_clean(100)
    assert result == {"blank_s": 15, "manual": True} and sps30.fake.clean_calls == 1
    assert sps30.read(100 + FAN_CLEAN_BLANK - 1) is None and sps30.is_blanked(105)
    assert sps30.read(100 + FAN_CLEAN_BLANK) is not None
    with pytest.raises(RuntimeError, match="rate-limited"):
        sps30.force_clean(100 + FAN_CLEAN_COOLDOWN - 1)
    sps30.force_clean(100 + FAN_CLEAN_COOLDOWN)
    assert sps30.fake.clean_calls == 2
    events = [e for e in db.recent_events() if e["type"] == "fan_clean"]
    assert len(events) == 2 and events[0]["details"]["manual"] is True


def test_scheduled_clean_ignores_the_manual_cooldown(sps30):
    sps30.ensure(0)
    sps30.force_clean(100)
    sps30.force_clean(200, manual=False)
    assert sps30.fake.clean_calls == 2


def test_status_word_and_old_firmware(sps30, log):
    sps30.ensure(0)
    sps30.fake.status = {"raw": 1 << 4, "speed_warning": False, "laser_error": False, "fan_error": True}
    assert sps30.status_word()["fan_error"] is True
    sps30.fake.raise_on_status = OSError("x")
    assert sps30.status_word() is None
    sps30.firmware = (2, 1)
    sps30.fake.raise_on_status = None
    assert sps30.status_word() is None and sps30._status_unsupported_logged is True


def test_reinit_clears_a_blank(sps30):
    sps30.ensure(0)
    sps30.force_clean(100)
    sps30.reinit(101, "test")
    assert sps30.blank_until is None and sps30.fake.stop_calls == 1 and sps30.fake.start_calls == 2
