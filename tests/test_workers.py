"""Worker-thread tests: sampling must never wait for display or network."""

import threading
import time

from airmonitor.workers import DisplayWorker, PeriodicWorker


class StubEvents:
    def __init__(self):
        self.entries = []
        self._lock = threading.Lock()

    def log(self, level, source, event_type, message, details=None):
        with self._lock:
            self.entries.append((source, event_type))

    def types(self):
        with self._lock:
            return [event_type for (_s, event_type) in self.entries]


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- DisplayWorker ----------------------------------------------------------


def test_submit_never_blocks_while_render_is_busy():
    release = threading.Event()
    started = threading.Event()
    rendered = []

    def slow_render(snapshot, full):
        started.set()
        release.wait(5)
        rendered.append((snapshot, full))

    worker = DisplayWorker(slow_render, StubEvents())
    worker.start()
    try:
        worker.submit("frame-1", False)
        assert started.wait(2)
        # While the worker is stuck, submits return instantly and coalesce.
        submitted_at = time.monotonic()
        worker.submit("frame-2", False)
        worker.submit("frame-3", True)
        assert time.monotonic() - submitted_at < 0.5
        release.set()
        assert wait_until(lambda: len(rendered) == 2)
        # frame-2 was replaced by frame-3: latest wins.
        assert rendered == [("frame-1", False), ("frame-3", True)]
    finally:
        release.set()
        worker.stop()


def test_wedged_render_reported_once_then_recovery():
    release = threading.Event()
    started = threading.Event()

    def stuck_render(_snapshot, _full):
        started.set()
        release.wait(5)

    events = StubEvents()
    worker = DisplayWorker(stuck_render, events, wedge_timeout=0.05)
    worker.start()
    try:
        worker.submit("frame", False)
        assert started.wait(2)
        time.sleep(0.1)
        assert worker.check_wedged() is True
        assert worker.check_wedged() is True  # still wedged, but no second event
        assert events.types().count(("display", "worker_wedged")[1]) == 1
        release.set()
        assert wait_until(lambda: "worker_recovered" in events.types())
        assert worker.check_wedged() is False
    finally:
        release.set()
        worker.stop()


def test_render_exception_does_not_kill_worker():
    rendered = []

    def flaky_render(snapshot, _full):
        if snapshot == "bad":
            raise RuntimeError("panel error")
        rendered.append(snapshot)

    worker = DisplayWorker(flaky_render, StubEvents())
    worker.start()
    try:
        worker.submit("bad", False)
        assert wait_until(lambda: worker._slot.empty())
        worker.submit("good", False)
        assert wait_until(lambda: rendered == ["good"])
        assert worker.alive
    finally:
        worker.stop()


# --- PeriodicWorker ---------------------------------------------------------


def test_periodic_worker_runs_and_survives_failures():
    events = StubEvents()
    calls = []

    def sometimes_broken():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("network down")

    worker = PeriodicWorker("net", 0.01, sometimes_broken, events)
    worker.start()
    try:
        assert wait_until(lambda: len(calls) >= 3)
        assert "task_failed" in events.types()
        assert worker.alive
    finally:
        worker.stop()
    assert not worker.alive


def test_periodic_worker_stops_promptly_despite_long_interval():
    worker = PeriodicWorker("weather", 3600, lambda: None, StubEvents())
    worker.start()
    started = time.monotonic()
    worker.stop()
    assert time.monotonic() - started < 5
    assert not worker.alive


# --- Integration: sampling is immune to a blocked display -------------------


def test_collector_samples_while_display_render_is_blocked(tmp_path):
    from airmonitor.config import Config
    from main import AirMonitor

    config = Config(database_path=str(tmp_path / "i.db"), log_file=str(tmp_path / "i.log"))
    monitor = AirMonitor(config)
    release = threading.Event()
    blocked = threading.Event()

    def stuck_render(_snapshot, _full):
        blocked.set()
        release.wait(10)

    monitor.display_worker._render = stuck_render
    monitor.display_worker.start()
    try:
        monitor.update_display(full_refresh=False)  # occupies the worker
        assert blocked.wait(2)

        started = time.monotonic()
        monitor.collect_sample()  # fake sensors initialize + produce a sample
        assert time.monotonic() - started < 2
        assert monitor.database.get_latest_measurement() is not None

        monitor.update_display(full_refresh=True)  # still returns instantly
        assert time.monotonic() - started < 3
    finally:
        release.set()
        monitor.display_worker.stop()
        monitor.database.close()
