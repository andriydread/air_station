"""The scheduler: intervals, alignment, isolation of failures, heartbeat, stop."""

import signal

import pytest

from shared import clock
from shared.events import Log
from shared.heartbeat import SystemdNotifier
from shared.loop import Loop, Task
from tests.mocks.fake_devices import FakeClock


@pytest.fixture
def fake_clock(monkeypatch):
    fake = FakeClock(start=1_788_436_805.0)  # :05 past a 10 s boundary
    monkeypatch.setattr(clock, "now", fake.now)
    monkeypatch.setattr(clock, "monotonic", fake.monotonic)
    monkeypatch.setattr(clock, "sleep", fake.sleep)
    return fake


@pytest.fixture
def log(tmp_config, db):
    logger = Log("collector", tmp_config, db=db, strict=True, clock=clock.now)
    yield logger
    logger.close()


class _Pinger(SystemdNotifier):
    def __init__(self):
        super().__init__(address="")
        self.messages = []

    def _send(self, message):
        self.messages.append(message)


def _run_for(loop, fake_clock, seconds, step=0.2):
    passes = int(seconds / step) + 1  # +1: the boundary pass, immune to float drift at epoch scale
    loop.idle_sleep = step
    loop.run(max_passes=passes)


def test_tasks_fire_at_their_intervals(fake_clock, log):
    calls = {"a": [], "b": []}
    tasks = [
        Task("a", 10, lambda: calls["a"].append(clock.now())),
        Task("b", 30, lambda: calls["b"].append(clock.now()), first_run_immediately=False),
    ]
    _run_for(Loop(log, None, tasks), fake_clock, 61)
    assert len(calls["a"]) == 7          # t=0,10,20,30,40,50,60
    assert len(calls["b"]) == 2          # t=30,60 (no immediate first run)


def test_aligned_task_fires_on_wall_clock_multiples(fake_clock, log):
    stamps = []
    task = Task("sample", 10, lambda: stamps.append(clock.now()), aligned=True,
                first_run_immediately=False)
    _run_for(Loop(log, None, [task]), fake_clock, 31)
    assert [round(s) % 10 for s in stamps] == [0, 0, 0]
    assert stamps[0] == pytest.approx(1_788_436_810.0, abs=0.2)


def test_an_exception_in_one_task_does_not_stop_the_others(fake_clock, log, db):
    calls = []

    def bad():
        raise RuntimeError("sensor exploded")

    tasks = [Task("bad", 10, bad), Task("good", 10, lambda: calls.append(1))]
    loop = Loop(log, None, tasks)
    _run_for(loop, fake_clock, 21)
    assert len(calls) == 3 and tasks[0].failures == 3
    events = db.recent_events()
    assert events[0]["type"] == "error" and events[0]["details"]["task"] == "bad"
    assert "sensor exploded" in events[0]["details"]["exc"]


def test_a_long_stall_skips_missed_runs_instead_of_bursting(fake_clock, log):
    calls = []

    def slow_once():
        calls.append(clock.now())
        if len(calls) == 1:
            fake_clock.advance(95)  # the first run took 95 s (a full refresh gone wrong)

    _run_for(Loop(log, None, [Task("t", 10, slow_once)]), fake_clock, 120)
    # 0 s, then the next due after the stall, then every 10 s — never 9 catch-up runs at once
    gaps = [round(b - a) for a, b in zip(calls, calls[1:])]
    assert gaps[0] >= 95 and all(g == 10 for g in gaps[1:])


def test_heartbeat_every_ten_seconds_and_ready_stopping(fake_clock, log):
    pinger = _Pinger()
    loop = Loop(log, pinger, [Task("noop", 60, lambda: None)])
    _run_for(loop, fake_clock, 35)
    assert pinger.messages[0] == "READY=1" and pinger.messages[-1] == "STOPPING=1"
    assert pinger.messages.count("WATCHDOG=1") == 4  # t=0,10,20,30


def test_clock_jump_is_logged_once(fake_clock, log, db):
    loop = Loop(log, None, [Task("noop", 60, lambda: None)])

    fired = []

    def jump():
        if not fired:
            fired.append(1)
            fake_clock.jump_wall(12)

    loop.tasks.append(Task("jump", 5, jump, first_run_immediately=False))
    _run_for(loop, fake_clock, 6)
    jumps = [e for e in db.recent_events() if e["type"] == "clock_jump"]
    assert len(jumps) == 1 and jumps[0]["details"]["seconds"] == pytest.approx(12.0, abs=0.5)


def test_sigterm_stops_the_loop(fake_clock, log):
    pinger = _Pinger()
    loop = Loop(log, pinger, [Task("noop", 60, lambda: None)])
    loop.install_signal_handlers()

    def fire():
        signal.raise_signal(signal.SIGTERM)

    loop.tasks.append(Task("fire", 1, fire, first_run_immediately=False))
    reason = loop.run()
    assert reason == "SIGTERM" and loop.running is False
    assert pinger.messages[-1] == "STOPPING=1"


def test_backward_clock_step_rearms_a_task(fake_clock, log):
    calls = []
    task = Task("t", 10, lambda: calls.append(clock.now()))
    loop = Loop(log, None, [task])
    _run_for(loop, fake_clock, 11)
    assert len(calls) == 2
    fake_clock.jump_wall(-3600)  # NTP pulled the clock back an hour
    _run_for(loop, fake_clock, 11)
    assert len(calls) == 4  # it did not wait an hour for the calendar to catch up


def test_retry_in_overrides_the_next_interval_once(fake_clock, log):
    calls = []
    task = Task("weather", 1800, lambda: None)

    def flaky():
        calls.append(clock.now())
        if len(calls) == 1:
            task.retry_in(120)

    task.func = flaky
    _run_for(Loop(log, None, [task]), fake_clock, 2000)
    gaps = [round(b - a) for a, b in zip(calls, calls[1:])]
    assert gaps == [120, 1800]
