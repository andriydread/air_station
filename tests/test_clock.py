"""Clock helpers: alignment, NTP wait, clock-jump detection, local schedules."""

from datetime import datetime

import pytest

from shared import clock
from tests.mocks.fake_devices import FakeClock, FakeRunner


@pytest.fixture
def fake_clock(monkeypatch):
    fake = FakeClock(start=1_788_436_800.0)  # 2026-09-03 12:00:00 UTC
    monkeypatch.setattr(clock, "now", fake.now)
    monkeypatch.setattr(clock, "monotonic", fake.monotonic)
    monkeypatch.setattr(clock, "sleep", fake.sleep)
    return fake


def test_next_aligned_and_aligned_stamp():
    assert clock.next_aligned(10, 1000.0) == 1010
    assert clock.next_aligned(10, 1000.5) == 1010
    assert clock.next_aligned(10, 1009.99) == 1010
    assert clock.next_aligned(60, 1010) == 1020
    assert clock.aligned_stamp(10, 1009.99) == 1000
    assert clock.aligned_stamp(10, 1010.0) == 1010


def test_wait_for_ntp_succeeds_on_the_third_poll(fake_clock):
    answers = iter(["no", "no", "yes"])
    runner = FakeRunner()
    runner.results["timedatectl"] = lambda argv: FakeRunner.Completed(stdout=next(answers) + "\n")
    assert clock.wait_for_ntp(timeout=60, runner=runner) is True
    assert len(runner.calls) == 3 and fake_clock.sleeps == [2.0, 2.0]
    assert runner.calls[0] == ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]


def test_wait_for_ntp_times_out(fake_clock):
    runner = FakeRunner()
    runner.results["timedatectl"] = FakeRunner.Completed(stdout="no\n")
    assert clock.wait_for_ntp(timeout=5, runner=runner) is False
    assert fake_clock.monotonic() >= 1005.0 and len(runner.calls) >= 3


def test_wait_for_ntp_without_timedatectl_returns_false_fast(fake_clock):
    runner = FakeRunner()
    runner.results["timedatectl"] = FileNotFoundError("no timedatectl")
    assert clock.wait_for_ntp(timeout=60, runner=runner) is False
    assert fake_clock.sleeps == []


def test_wait_for_ntp_tolerates_runner_errors(fake_clock):
    runner = FakeRunner()
    runner.results["timedatectl"] = RuntimeError("busy")
    assert clock.wait_for_ntp(timeout=3, runner=runner) is False


def test_clock_watch_sees_only_wall_jumps(fake_clock):
    watch = clock.ClockWatch()
    fake_clock.advance(30)
    assert watch.check() == pytest.approx(0.0)
    fake_clock.jump_wall(7)
    assert watch.check() == pytest.approx(7.0)
    fake_clock.advance(10)
    assert watch.check() == pytest.approx(0.0)  # a jump is reported once


def test_local_schedule_fires_once_per_matching_minute(monkeypatch):
    # Pin the zone so the test means the same thing on any machine.
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    import time as _time
    _time.tzset()
    sunday_4am = datetime(2026, 9, 6, 4, 0, 20).astimezone().timestamp()  # a Sunday
    schedule = clock.LocalSchedule(hour=4, minute=0, weekday=6)
    assert schedule.due(sunday_4am) is True
    assert schedule.due(sunday_4am + 30) is False       # same minute
    assert schedule.due(sunday_4am + 86400) is False    # Monday
    assert schedule.due(sunday_4am + 7 * 86400) is True # next Sunday
    nightly = clock.LocalSchedule(hour=0, minute=5)
    monday_0005 = datetime(2026, 9, 7, 0, 5, 0).astimezone().timestamp()
    assert nightly.due(monday_0005 - 60) is False
    assert nightly.due(monday_0005) is True
    assert nightly.due(monday_0005 + 86400) is True
    _time.tzset()
