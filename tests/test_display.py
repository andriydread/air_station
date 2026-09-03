"""Panel wrapper: cadence, BUSY timeout recovery, status."""

import pytest
from PIL import Image

from manager.display import FULL_REFRESH_EVERY, Panel


class FakeDriver:
    MODE_FULL = "full"
    MODE_PARTIAL = "partial"

    def __init__(self):
        self.modes = []
        self.raise_next = None
        self.closed = False
        self.slept = 0

    def display_image(self, _image, mode, auto_sleep=True):
        if self.raise_next is not None:
            exc, self.raise_next = self.raise_next, None
            raise exc
        self.modes.append(mode)

    def sleep(self):
        self.slept += 1

    def close(self):
        self.closed = True


@pytest.fixture
def panel(log):
    drivers = []

    def factory():
        drivers.append(FakeDriver())
        return drivers[-1]

    p = Panel(log, driver_factory=factory, monotonic=lambda: 0.0)
    p.drivers = drivers
    return p


FRAME = Image.new("1", (416, 240), 255)


def test_first_frame_is_full_then_partial_until_five_minutes(panel):
    modes = [panel.show(FRAME, now=t) for t in (0, 60, 120, 180, 240, 300, 360)]
    assert modes == ["full", "partial", "partial", "partial", "partial", "full", "partial"]
    assert panel.drivers[0].modes == modes
    assert panel.last_full_at == 300 and panel.last_partial_at == 360 and panel.frames == 7
    assert panel.next_full_at == 300 + FULL_REFRESH_EVERY


def test_forced_full(panel):
    panel.show(FRAME, now=0)
    assert panel.show(FRAME, now=60, full=True) == "full"
    assert panel.show(FRAME, now=120) == "partial"


def test_busy_timeout_logs_recovers_with_backoff_and_paints_full(panel, db):
    panel.show(FRAME, now=0)
    panel.drivers[0].raise_next = TimeoutError("UC8253C busy pin timeout")
    assert panel.show(FRAME, now=60) is None
    assert panel.drivers[0].closed and panel.driver is None and panel.healthy is False
    assert "TimeoutError" in panel.last_error
    events = [e["type"] for e in db.recent_events()]
    assert events[0] == "display_error"
    assert panel.show(FRAME, now=70) is None           # backoff: not before 30 s
    assert panel.show(FRAME, now=90) == "full"         # recovered, first frame full
    assert len(panel.drivers) == 2 and panel.reinit_count == 1
    assert [e["type"] for e in db.recent_events()][0] == "display_reinit"
    assert panel.status()["failures"] == 1


def test_init_failure_backs_off_and_logs_once(log, db):
    attempts = []

    def factory():
        attempts.append(1)
        raise OSError("no SPI")

    panel = Panel(log, driver_factory=factory)
    assert panel.show(FRAME, now=0) is None
    assert panel.show(FRAME, now=10) is None and len(attempts) == 1
    assert panel.show(FRAME, now=30) is None and len(attempts) == 2
    assert [e["type"] for e in db.recent_events()].count("display_error") == 1
    assert panel.status()["available"] is False


def test_sleep_close_and_status_shape(panel):
    panel.show(FRAME, now=0)
    panel.sleep()
    assert panel.drivers[0].slept == 1
    panel.close()
    assert panel.driver is None and panel.drivers[0].closed
    assert set(panel.status()) == {"available", "healthy", "last_error", "last_full_at", "last_partial_at",
                                   "render_ms", "busy_ms", "frames", "failures", "reinit_count"}


def test_real_driver_busy_timeout_through_the_wrapper(log, db, monkeypatch):
    """The real UC8253C driver on the fake GPIO with a stuck BUSY pin."""
    import RPi.GPIO as gpio
    import drivers.uc8253c as uc

    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 1.0
        return clock["t"]

    monkeypatch.setattr(uc.time, "sleep", lambda _s: None)
    monkeypatch.setattr(uc.time, "monotonic", fake_monotonic)
    panel = Panel(log)
    frame = Image.new("1", (416, 240), 255)
    assert panel.show(frame, now=0) == "full"
    gpio.pin_values[24] = 0  # BUSY stuck low
    assert panel.show(frame, now=60) is None
    assert "TimeoutError" in panel.last_error
    gpio.pin_values[24] = 1
    assert panel.show(frame, now=100) == "full"
