"""SHT41 wrapper: mode, offset, errors, no warm-up."""

import sys

import pytest

from collector.sensors import Sht41
from tests.mocks.fake_devices import FakeSht41Device


@pytest.fixture
def sht41(log, tmp_config, monkeypatch):
    fake = FakeSht41Device(temperature=22.5, humidity=45.0)
    monkeypatch.setattr(sys.modules["adafruit_sht4x"], "SHT4x", lambda _i2c: fake)
    sensor = Sht41(object(), tmp_config, log)
    sensor.fake = fake
    return sensor


def test_open_sets_high_precision_no_heater_and_reads(sht41):
    assert sht41.ensure(0) is True
    assert sht41.fake.mode == sys.modules["adafruit_sht4x"].Mode.NOHEAT_HIGHPRECISION
    assert sht41.health.id == "0000abcd"
    assert sht41.warmup_left(0) == 0
    assert sht41.read(10) == {"temp": 22.5, "humid": 45.0}


def test_offset_is_applied(log, tmp_config, monkeypatch):
    fake = FakeSht41Device(temperature=22.5, humidity=45.0)
    monkeypatch.setattr(sys.modules["adafruit_sht4x"], "SHT4x", lambda _i2c: fake)
    raw = tmp_config.as_dict()
    raw["sensors"]["sht41_temp_offset_c"] = -1.5
    config = tmp_config.__class__.from_dict(
        {k: raw[k] for k in ("location", "sensors", "retention_days", "weather", "dashboard", "paths", "logging")},
        repo_root=tmp_config.repo_root, source=tmp_config.source)
    sensor = Sht41(object(), config, log)
    sensor.ensure(0)
    assert sensor.read(1)["temp"] == 21.0


def test_read_errors_propagate_and_none_without_device(sht41):
    assert sht41.read(0) is None
    sht41.ensure(0)
    sht41.fake.raise_on_read = OSError("nack")
    with pytest.raises(OSError):
        sht41.read(1)


def test_readback_event_says_heater_off(sht41, db):
    sht41.ensure(1000)
    config = [e for e in db.recent_events() if e["source"] == "sht41" and e["type"] == "sensor_config"]
    assert len(config) == 1
    held = config[0]["details"]
    assert held["heater"] == "off" and held["serial"] == "0000abcd" and held["temp_offset_c"] == 0.0
    assert held["mode"] is not None
