"""Sensor bookkeeping: backoff, streaks, silence, warm-up, status."""

from collector.sensors import BAD_STREAK_REINIT, SILENCE_REINIT, Sensor
from shared.backoff import ReinitBackoff


class Flaky(Sensor):
    name = "sps30"
    warmup_seconds = 30

    def __init__(self, log, fail_opens=0):
        super().__init__(log)
        self.fail_opens = fail_opens
        self.opens = 0
        self.closed = []

    def _open(self):
        self.opens += 1
        if self.opens <= self.fail_opens:
            raise OSError(f"no ack #{self.opens}")
        return {"n": self.opens}

    def _close(self, device):
        self.closed.append(device)


def test_backoff_sequence_and_reset():
    b = ReinitBackoff()
    delays = [b.failed(now=0) for _ in range(6)]
    assert delays == [30, 60, 120, 240, 300, 300]
    assert b.failures == 6 and not b.due(200) and b.due(300)
    b.reset()
    assert b.due(0) and b.delay == 30 and b.failures == 0


def test_init_success_logs_one_event_and_starts_warmup(log, db):
    s = Flaky(log)
    assert s.ensure(1000) is True
    assert s.health.available and s.health.healthy and s.health.last_ok_at == 1000
    assert s.warmup_left(1000) == 30 and s.warmup_left(1029) == 1 and s.warmup_left(1030) == 0
    events = db.recent_events()
    assert [e["type"] for e in events] == ["sensor_init"]
    assert events[0]["details"]["warmup_s"] == 30


def test_init_failures_back_off_and_log_once_per_streak(log, db):
    s = Flaky(log, fail_opens=2)
    assert s.ensure(0) is False
    assert s.health.available is False and "no ack #1" in s.health.last_error
    assert s.ensure(10) is False and s.opens == 1          # backoff: not retried yet
    assert s.ensure(30) is False and s.opens == 2          # 30 s later: retried, failed again
    assert s.ensure(60) is False and s.opens == 2          # next delay is 60 s
    assert s.ensure(90) is True and s.opens == 3
    types = [e["type"] for e in db.recent_events()]
    assert types.count("sensor_error") == 1 and types.count("sensor_init") == 1


def test_six_bad_readings_trigger_one_reinit(log, db):
    s = Flaky(log)
    s.ensure(0)
    fired = [s.note_bad(100 + i, "garbage") for i in range(BAD_STREAK_REINIT)]
    assert fired == [False] * (BAD_STREAK_REINIT - 1) + [True]
    assert s.reinit_count == 1 and s.opens == 2 and s.closed == [{"n": 1}]
    assert s.bad_streak == 0 and s.warmup_started_at == 100 + BAD_STREAK_REINIT - 1
    reinit = [e for e in db.recent_events() if e["type"] == "sensor_reinit"]
    assert len(reinit) == 1 and "6 bad readings" in reinit[0]["message"]


def test_a_good_reading_resets_the_streak(log):
    s = Flaky(log)
    s.ensure(0)
    for i in range(BAD_STREAK_REINIT - 1):
        s.note_bad(10 + i, "x")
    s.note_ok(20)
    assert s.bad_streak == 0 and s.health.healthy
    s.note_bad(30, "x")
    assert s.reinit_count == 0


def test_silence_counts_from_the_end_of_warmup(log):
    s = Flaky(log)
    s.ensure(0)  # warm-up until 30
    assert s.check_silence(30 + SILENCE_REINIT - 1) is False
    assert s.check_silence(30 + SILENCE_REINIT) is True
    assert s.reinit_count == 1
    # a reading resets the clock
    s.note_ok(200)
    assert s.check_silence(200 + SILENCE_REINIT - 1) is False
    assert s.check_silence(200 + SILENCE_REINIT) is True


def test_silence_is_not_checked_without_a_device(log):
    s = Flaky(log, fail_opens=99)
    s.ensure(0)
    assert s.check_silence(10_000) is False


def test_status_shape(log):
    s = Flaky(log)
    s.ensure(0)
    s.health.id = "2.2"
    assert s.status(10) == {
        "available": True, "healthy": True, "last_error": None, "last_ok_at": 0,
        "warmup_left": 20, "reinit_count": 0, "id": "2.2",
    }
    s.stop()
    assert s.device is None and s.closed == [{"n": 1}]
