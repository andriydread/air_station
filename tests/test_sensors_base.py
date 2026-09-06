"""Sensor bookkeeping: backoff, the quiet time, streaks, silence, status."""

from collector.sensors import (
    BAD_WINDOW_COUNT, BAD_WINDOW_S, BAD_STREAK_REINIT, QUIET_SECONDS, SILENCE_REINIT, Sensor, ready_after,
)
from shared.backoff import ReinitBackoff


class Flaky(Sensor):
    name = "sps30"

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


def test_ready_after_is_the_first_whole_minute_at_least_a_minute_away():
    assert QUIET_SECONDS == 60
    assert ready_after(1000) == 1080      # 1060 → up to the next :00
    assert ready_after(1020) == 1080      # exactly on a mark: that mark
    assert ready_after(1021) == 1140


def test_backoff_sequence_and_reset():
    b = ReinitBackoff()
    delays = [b.failed(now=0) for _ in range(6)]
    assert delays == [30, 60, 120, 240, 300, 300]
    assert b.failures == 6 and not b.due(200) and b.due(300)
    b.reset()
    assert b.due(0) and b.delay == 30 and b.failures == 0


def test_init_success_logs_one_event_and_starts_the_quiet_time(log, db):
    s = Flaky(log)
    assert s.ensure(1000) is True
    assert s.health.available and s.health.healthy and s.health.last_ok_at == 1000
    assert s.ready_at == 1080 and s.ready(1079) is False and s.ready(1080) is True
    events = db.recent_events()
    assert [e["type"] for e in events] == ["sensor_init"]
    assert events[0]["details"]["ready_at"] == 1080


def test_init_failures_back_off_and_log_once_per_streak(log, db):
    s = Flaky(log, fail_opens=2)
    assert s.ensure(0) is False
    assert s.health.available is False and "no ack #1" in s.health.last_error
    assert s.ensure(10) is False and s.opens == 1          # backoff: not retried yet
    assert s.ensure(30) is False and s.opens == 2          # 30 s later: retried, failed again
    assert s.ensure(60) is False and s.opens == 2          # next delay is 60 s
    assert s.ensure(90) is True and s.opens == 3
    assert s.ready(90) is False and s.status()["ready_at"] == 180
    types = [e["type"] for e in db.recent_events()]
    assert types.count("sensor_error") == 1 and types.count("sensor_init") == 1


def test_six_bad_readings_trigger_one_reinit(log, db):
    s = Flaky(log)
    s.ensure(0)
    fired = [s.note_bad(100 + i, "garbage") for i in range(BAD_STREAK_REINIT)]
    assert fired == [False] * (BAD_STREAK_REINIT - 1) + [True]
    assert s.reinit_count == 1 and s.opens == 2 and s.closed == [{"n": 1}]
    assert s.bad_streak == 0 and s.ready_at == ready_after(100 + BAD_STREAK_REINIT - 1)
    reinit = [e for e in db.recent_events() if e["type"] == "sensor_reinit"]
    assert len(reinit) == 1 and "6 bad readings in a row" in reinit[0]["message"]


def test_a_good_reading_resets_the_streak(log):
    s = Flaky(log)
    s.ensure(0)
    for i in range(BAD_STREAK_REINIT - 2):
        s.note_bad(10 + i, "x")
    s.note_ok(20)
    assert s.bad_streak == 0 and s.health.healthy
    s.note_bad(30, "x")
    assert s.bad_streak == 1 and s.reinit_count == 0  # five bad in the window: one short of the window rule too


def test_silence_counts_from_ready_at(log):
    s = Flaky(log)
    s.ensure(0)  # quiet until 60
    assert s.check_silence(60 + SILENCE_REINIT - 1) is False
    assert s.check_silence(60 + SILENCE_REINIT) is True
    assert s.reinit_count == 1
    # a reading resets the clock (past the new quiet time)
    s.note_ok(300)
    assert s.check_silence(300 + SILENCE_REINIT - 1) is False
    assert s.check_silence(300 + SILENCE_REINIT) is True


def test_silence_is_not_checked_without_a_device(log):
    s = Flaky(log, fail_opens=99)
    s.ensure(0)
    assert s.check_silence(10_000) is False


def test_status_shape(log):
    s = Flaky(log)
    s.ensure(0)
    s.health.id = "2.2"
    assert s.status() == {
        "available": True, "healthy": True, "last_error": None, "last_ok_at": 0,
        "ready_at": 60, "reinit_count": 0, "id": "2.2",
    }
    s.stop()
    assert s.device is None and s.closed == [{"n": 1}] and s.status()["ready_at"] is None


def test_bad_readings_spread_over_the_window_also_reinit(log, db):
    s = Flaky(log)
    s.ensure(0)
    # a bad reading every other beat: the streak never passes 1, the window rule fires on the 6th
    fired = []
    t = 100
    for i in range(BAD_WINDOW_COUNT):
        fired.append(s.note_bad(t, "garbage"))
        s.note_ok(t + 10)
        t += 20  # six bad readings inside 100 s
    assert fired == [False] * (BAD_WINDOW_COUNT - 1) + [True]
    assert s.reinit_count == 1 and s.bad_times == []  # a fresh device starts clean
    reinit = [e for e in db.recent_events() if e["type"] == "sensor_reinit"]
    assert reinit[0]["message"].endswith(f"{BAD_WINDOW_COUNT} bad readings in {BAD_WINDOW_S} s")


def test_old_bad_readings_fall_out_of_the_window(log):
    s = Flaky(log)
    s.ensure(0)
    for i in range(BAD_WINDOW_COUNT - 1):
        s.note_bad(100 + i * 10, "x")
        s.note_ok(105 + i * 10)
    assert s.note_bad(140 + BAD_WINDOW_S, "x") is False  # the first five are older than the window
    assert s.reinit_count == 0 and len(s.bad_times) == 1
