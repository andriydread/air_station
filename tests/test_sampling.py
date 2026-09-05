"""One beat of the collector, with scriptable sensors."""

import sys

import pytest

from collector.sampling import DROP_EVENT_EVERY, SAMPLE_INTERVAL, Sampler
from collector.sensors import BAD_STREAK_REINIT, SCD41_WARMUP, SPS30_WARMUP, Scd41, Sht41, Sps30
from tests.mocks.fake_devices import FakeClock, FakeScd41Device, FakeSht41Device, FakeSps30Device


class Rig:
    """Three wrappers on scriptable fakes, a sampler, a controllable clock."""

    def __init__(self, db, log, config, monkeypatch, start=1_788_436_805.0):
        self.clock = FakeClock(start=start)
        self.scd = FakeScd41Device()
        self.sht = FakeSht41Device()
        self.sps = FakeSps30Device()
        monkeypatch.setattr(sys.modules["adafruit_scd4x"], "SCD4X", lambda _i2c: self.scd)
        monkeypatch.setattr(sys.modules["adafruit_sht4x"], "SHT4x", lambda _i2c: self.sht)
        self.scd41 = Scd41(object(), config, log, sleep=self.clock.sleep, monotonic=self.clock.monotonic)
        self.sht41 = Sht41(object(), config, log)
        self.sps30 = Sps30(object(), config, log, device_factory=lambda _i2c: self.sps)
        self.buses = 0

        def bus():
            self.buses += 1
            return object()

        self.sampler = Sampler(db, log, self.scd41, self.sht41, self.sps30, i2c_factory=bus,
                               monotonic=self.clock.monotonic)
        self.db = db

    def beat(self):
        record = self.sampler.beat(self.clock.now())
        self.clock.advance(SAMPLE_INTERVAL)
        return record

    def beats(self, n):
        return [self.beat() for _ in range(n)]

    def warm(self):
        """One beat to initialise the sensors, then past every warm-up."""
        self.beat()
        self.clock.advance(60)

    def rows(self):
        return self.db.raw_between(0, 10**10)


@pytest.fixture
def rig(db, log, tmp_config, monkeypatch):
    return Rig(db, log, tmp_config, monkeypatch)


def test_first_beat_row_is_aligned_and_warmups_leave_cells_empty(rig, db):
    record = rig.beat()
    assert record["ts"] % SAMPLE_INTERVAL == 0 and record["ts"] == 1_788_436_800
    row = rig.rows()[0]
    assert row["temp"] == 22.5 and row["humid"] == 45.0        # SHT41 has no warm-up
    assert row["co2"] is None and row["pm25"] is None           # SCD41 / SPS30 warming up
    assert record["warmup_left"] == {"scd41": SCD41_WARMUP, "sht41": 0, "sps30": SPS30_WARMUP}
    warm = [e for e in db.recent_events() if e["type"] == "warming_up"]
    assert sorted(e["source"] for e in warm) == ["scd41", "sps30"]


def test_warmup_event_is_logged_once_and_values_arrive_after(rig, db):
    rig.beats(4)  # 0 … 90 s
    warm = [e for e in db.recent_events() if e["type"] == "warming_up"]
    assert len(warm) == 2
    rows = rig.rows()
    assert [r["pm25"] for r in rows[:3]] == [None, 2.5, 2.5]          # dust from beat 2 (30 s)
    assert [r["co2"] for r in rows[:4]] == [None, None, 600, 600]      # co2 from beat 3 (60 s)
    assert rows[2]["co2_temp"] == 23.0 and rows[2]["nc1"] == 8.6


def test_dropped_value_is_null_with_event_cadence(rig, db):
    rig.warm()
    rig.scd.default_co2 = 0.0  # below 350: garbage forever
    rig.beats(10)  # six drops, a re-init, two warm-up beats, two more drops
    rows = rig.rows()
    assert all(r["co2"] is None for r in rows) and all(r["temp"] == 22.5 for r in rows)
    # six drops re-init the sensor (new warm-up, nothing asked), so a dying
    # sensor costs one event per streak, never one per beat
    drops = [e for e in db.recent_events() if e["type"] == "value_dropped"]
    assert [e["details"]["streak"] for e in reversed(drops)] == [1, 1]
    assert drops[-1]["details"] == {"metric": "co2", "value": 0.0, "reason": "range", "streak": 1}
    assert rig.scd41.reinit_count == 1


def test_drop_event_cadence_is_first_then_every_sixth(rig, db):
    for _ in range(14):
        rig.sampler._drop_events({"pm25": (-1.0, "negative")})
    drops = [e for e in db.recent_events() if e["type"] == "value_dropped"]
    assert [e["details"]["streak"] for e in reversed(drops)] == [1, 1 + DROP_EVENT_EVERY, 1 + 2 * DROP_EVENT_EVERY]
    rig.sampler._drop_events({})
    rig.sampler._drop_events({"pm25": (-1.0, "negative")})
    assert db.recent_events()[0]["details"]["streak"] == 1  # a good beat resets the streak


def test_six_dropped_in_a_row_reinit_the_sensor_only(rig, db):
    rig.warm()
    rig.scd.default_co2 = 0.0
    rig.beats(BAD_STREAK_REINIT)
    assert rig.scd41.reinit_count == 1 and rig.sps30.reinit_count == 0 and rig.sht41.reinit_count == 0
    assert rig.scd.reinit_calls == 2 and rig.scd.start_calls == 0  # single shot: no start
    # a new warm-up follows the re-init: no CO2 asked for, no more drops
    before = len([e for e in db.recent_events() if e["type"] == "value_dropped"])
    rig.sampler.beat(rig.clock.now())  # the next beat, 30 s into the new warm-up
    after = len([e for e in db.recent_events() if e["type"] == "value_dropped"])
    assert after == before and rig.scd41.warmup_left(rig.clock.now()) > 0


def test_a_raising_sensor_does_not_stop_the_others(rig, db):
    rig.warm()
    rig.sht.raise_on_read = OSError(121, "Remote I/O error")
    records = rig.beats(2)
    row = rig.rows()[-1]
    assert row["temp"] is None and row["co2"] == 600 and row["pm25"] == 2.5
    assert records[0]["errno"]["sht41"] == 121 and "sht41" in records[0]["raised"]
    errors = [e for e in db.recent_events() if e["type"] == "sensor_error"]
    assert len(errors) == 1 and errors[0]["source"] == "sht41"  # first of the streak only


def test_all_sensors_raising_reinits_the_bus(rig, db):
    rig.warm()
    for fake in (rig.scd, rig.sht, rig.sps):
        fake.raise_on_read = OSError(121, "Remote I/O error")
    rig.beat()
    assert rig.sampler.bus_reinits == 1 and rig.buses == 1
    bus_events = [e for e in db.recent_events() if e["source"] == "i2c" and e["type"] == "sensor_reinit"]
    assert len(bus_events) == 1
    assert all(s.device is None for s in rig.sampler.sensors)
    for fake in (rig.scd, rig.sht, rig.sps):
        fake.raise_on_read = None
    rig.beat()
    assert all(s.device is not None for s in rig.sampler.sensors)  # re-opened on the new bus


def test_row_is_written_even_when_everything_is_empty(rig, db, log, tmp_config, monkeypatch):
    rig.sht.raise_on_read = OSError("x")
    rig.beat()  # co2/pm warming up, sht failing
    rows = rig.rows()
    assert len(rows) == 1 and all(rows[0][k] is None for k in rows[0] if k != "recorded_at")


def test_no_fresh_co2_counts_toward_silence_not_bad_streak(rig):
    rig.warm()
    rig.scd.data_ready = False
    rig.beats(3)
    assert rig.scd41.bad_streak == 0 and rig.scd41.reinit_count == 0
    assert rig.rows()[-1]["co2"] is None and rig.rows()[-1]["temp"] == 22.5
    rig.clock.advance(120)
    rig.beat()
    assert rig.scd41.reinit_count == 1  # 2 min without a reading


def test_blanked_dust_readings_do_not_count_as_silence(rig):
    rig.warm()
    rig.sps30.force_clean(rig.clock.now())
    rig.beat()
    assert rig.rows()[-1]["pm25"] is None and rig.sps30.reinit_count == 0


def test_debug_record_carries_extras_and_status_word(rig):
    rig.warm()
    record = rig.beat()
    assert record["raw"]["pm4"] == 3.0 and record["raw"]["nc4"] == 8.9 and record["raw"]["nc10"] == 8.9
    assert record["sps30_status"]["fan_error"] is False
    assert set(record["read_ms"]) == {"scd41", "sht41", "sps30"}


def test_storage_failure_is_counted_not_raised(rig, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.db, "insert_raw", boom)
    rig.beat()
    assert rig.sampler.storage_failures == 1
