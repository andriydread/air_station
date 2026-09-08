"""One beat of the collector, with scriptable sensors."""

import sys

import pytest

from collector.sampling import SAMPLE_INTERVAL, Sampler
from collector.sensors import BAD_STREAK_REINIT, FAN_CLEAN_BLANK, SILENCE_REINIT, Scd41, Sht41, Sps30
from shared.db import METRICS
from tests.mocks.fake_devices import FakeClock, FakeScd41Device, FakeSht41Device, FakeSps30Device

START = 1_788_436_800.0  # 2026-09-03 12:00:00 UTC, a :00


class Rig:
    """Three wrappers on scriptable fakes, a sampler, a controllable clock."""

    def __init__(self, db, log, config, monkeypatch, start=START):
        self.clock = FakeClock(start=start)
        self.scd = FakeScd41Device()
        self.sht = FakeSht41Device()
        self.sps = FakeSps30Device()
        monkeypatch.setattr(sys.modules["adafruit_scd4x"], "SCD4X", lambda _i2c: self.scd)
        monkeypatch.setattr(sys.modules["adafruit_sht4x"], "SHT4x", lambda _i2c: self.sht)
        self.scd41 = Scd41(object(), config, log, sleep=self.clock.sleep)
        self.sht41 = Sht41(object(), config, log)
        self.sps30 = Sps30(object(), config, log, device_factory=lambda _i2c: self.sps)
        self.buses = 0

        def bus():
            self.buses += 1
            return object()

        self.sampler = Sampler(db, log, self.scd41, self.sht41, self.sps30, i2c_factory=bus,
                               monotonic=self.clock.monotonic)
        self.db = db
        self.log = log

    def beat(self):
        record = self.sampler.beat(self.clock.now())
        self.clock.advance(SAMPLE_INTERVAL)
        return record

    def beats(self, n):
        return [self.beat() for _ in range(n)]

    def ready(self):
        """One beat to initialise the sensors (the SCD41 sleeps 2 s), then on to ready_at."""
        self.beat()
        self.clock._wall = self.clock._mono = float(self.scd41.ready_at)

    def rows(self):
        return self.db.raw_between(0, 10**10)

    def row_lines(self):
        self.log.close()
        return [l for l in self.log.path.read_text().splitlines() if " sample row " in l]


@pytest.fixture
def rig(db, log, tmp_config, monkeypatch):
    return Rig(db, log, tmp_config, monkeypatch)


def test_nothing_is_asked_written_or_logged_before_ready_at(rig):
    record = rig.beat()
    assert record["present"] == ["sht41", "sps30", "scd41"] and record["asked"] == []
    assert all(s.ready_at == START + 60 for s in rig.sampler.sensors)  # a start on :00: the next :00
    rig.beats(5)  # up to :50: still quiet
    assert rig.rows() == [] and rig.sampler.sample_count == 0 and rig.row_lines() == []


def test_the_first_row_is_at_ready_at_with_every_cell(rig):
    rig.ready()
    record = rig.beat()
    assert record["asked"] == record["answered"] == ["sht41", "sps30", "scd41"]
    row = rig.rows()[0]
    assert row["recorded_at"] == START + 60 and row["recorded_at"] % SAMPLE_INTERVAL == 0
    assert row["co2"] == 600 and row["temp"] == 22.5 and row["pm25"] == 2.5 and row["nc10"] == 8.9
    assert all(row[m] is not None for m in METRICS)
    (line,) = rig.row_lines()
    assert " INFO collector sample row " in line and "co2=600 co2_temp=23.0" in line
    assert " ms=sht41:" in line and "bad=" not in line and "raised=" not in line  # a clean beat
    rig.log.close()
    beat_lines = [l for l in rig.log.path.read_text().splitlines() if " DEBUG collector sample beat " in l]
    assert beat_lines and "asked=sht41,sps30,scd41 answered=sht41,sps30,scd41 no_data=- raised=-" in beat_lines[-1]


def test_a_value_that_cannot_be_air_is_stored_and_counted_bad(rig):
    rig.ready()
    rig.scd.default_co2 = 0.0
    record = rig.beat()
    assert rig.rows()[0]["co2"] == 0 and rig.rows()[0]["temp"] == 22.5
    assert record["bad"] == {"co2": "range"} and rig.scd41.bad_streak == 1
    assert rig.scd41.health.healthy is False and "co2 0.0" in rig.scd41.health.last_error
    assert rig.sht41.health.healthy and rig.sps30.health.healthy
    assert rig.row_lines()[0].endswith("bad=co2:range")
    debug = [l for l in rig.log.path.read_text().splitlines() if " DEBUG collector scd41 bad " in l]
    assert debug and "streak=1 in_window=1" in debug[0] and "cannot_be_air" in debug[0].replace(" ", "_")


def test_six_bad_in_a_row_reset_that_sensor_only(rig, db):
    rig.ready()
    rig.scd.default_co2 = 0.0
    rig.beats(BAD_STREAK_REINIT)
    assert rig.scd41.reinit_count == 1 and rig.sps30.reinit_count == 0 and rig.sht41.reinit_count == 0
    assert rig.scd.reinit_calls == 2 and rig.scd.start_calls == 2 and rig.scd.self_tests == 1
    assert [r["co2"] for r in rig.rows()] == [0] * BAD_STREAK_REINIT  # stored, all of them
    reinit = [e for e in db.recent_events() if e["type"] == "sensor_reinit"]
    assert len(reinit) == 1 and reinit[0]["source"] == "scd41"
    record = rig.beat()  # the SCD41 is quiet again; the others carry on
    assert record["asked"] == ["sht41", "sps30"]
    assert rig.rows()[-1]["co2"] is None and rig.rows()[-1]["temp"] == 22.5


def test_a_raising_sensor_does_not_stop_the_others(rig, db):
    rig.ready()
    rig.sht.raise_on_read = OSError(121, "Remote I/O error")
    records = rig.beats(2)
    row = rig.rows()[-1]
    assert row["temp"] is None and row["co2"] == 600 and row["pm25"] == 2.5
    assert records[0]["raised"] == ["sht41"] and records[0]["errno"]["sht41"] == 121
    errors = [e for e in db.recent_events() if e["type"] == "sensor_error"]
    assert len(errors) == 1 and errors[0]["details"]["errno"] == 121  # the first of the streak only
    assert rig.row_lines()[-1].endswith("raised=sht41")
    assert rig.buses == 0  # two sensors answered: the bus is fine


def test_a_minute_without_any_answer_resets_the_sensor(rig, db):
    rig.ready()
    rig.sps.data_ready = False
    rig.beats(int(SILENCE_REINIT // SAMPLE_INTERVAL) + 1)
    assert rig.sps30.reinit_count == 1 and rig.sps.start_calls == 2
    reinit = [e for e in db.recent_events() if e["type"] == "sensor_reinit"]
    assert reinit[0]["source"] == "sps30" and "silent for 60 s" in reinit[0]["message"]
    assert [e for e in db.recent_events() if e["type"] == "sensor_error"] == []
    assert all(r["pm25"] is None and r["co2"] == 600 for r in rig.rows())


def test_every_sensor_raising_reopens_the_bus(rig, db):
    rig.ready()
    for fake in (rig.scd, rig.sps):
        fake.raise_on_data_ready = OSError(5, "bus")
    rig.sht.raise_on_read = OSError(5, "bus")
    rig.beat()
    assert rig.buses == 1 and rig.sampler.bus_reinits == 1
    assert [e["source"] for e in db.recent_events() if e["type"] == "sensor_reinit"] == ["i2c"]
    assert all(s.device is None for s in rig.sampler.sensors)
    record = rig.beat()  # the next beat re-inits all three on the new bus: quiet again
    assert all(s.device is not None for s in rig.sampler.sensors) and record["asked"] == []
    assert rig.scd.self_tests == 1  # the self-test runs once per process


def test_a_fan_clean_blank_is_not_silence(rig):
    rig.ready()
    rig.sps30.force_clean(rig.clock.now())
    rig.beat()
    assert rig.rows()[-1]["pm25"] is None and rig.rows()[-1]["co2"] == 600
    rig.clock.advance(FAN_CLEAN_BLANK)
    rig.beat()
    assert rig.rows()[-1]["pm25"] == 2.5 and rig.sps30.reinit_count == 0


def test_a_non_number_cannot_be_stored_and_counts_bad(rig):
    rig.ready()
    rig.scd.default_co2 = float("nan")
    record = rig.beat()
    assert rig.rows()[0]["co2"] is None and record["bad"] == {"co2": "nonfinite"}
