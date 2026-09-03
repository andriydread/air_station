"""Clock helpers: one place to read the time (so tests can replace it),
wall-clock alignment, the NTP wait at start, clock-jump detection, and the
two human schedules that run on the Pi's local time.
"""

import math
import subprocess
import time
from datetime import datetime
from typing import Callable, Optional

NTP_WAIT_SECONDS = 60.0
CLOCK_JUMP_SECONDS = 5.0


def now() -> float:
    """Wall-clock Unix seconds. Tests patch this function on the module."""
    return time.time()


def monotonic() -> float:
    return time.monotonic()


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def next_aligned(interval: float, current: float) -> float:
    """The next multiple of ``interval`` strictly after ``current``."""
    return (math.floor(current / interval) + 1) * interval


def aligned_stamp(interval: float, current: float) -> int:
    """The multiple of ``interval`` at or before ``current`` (a beat's timestamp)."""
    return int(math.floor(current / interval) * interval)


def wait_for_ntp(timeout: float = NTP_WAIT_SECONDS, runner: Callable = subprocess.run,
                 sleeper: Callable[[float], None] = None, poll: float = 2.0) -> bool:
    """Block until ``timedatectl`` says the clock is synced, or ``timeout`` passes.

    Returns True when synced, False on timeout or when ``timedatectl`` is not
    available (a dev machine, a container) — the caller writes anyway and
    logs ``clock_unsynced``.
    """
    sleeper = sleeper or sleep
    deadline = monotonic() + timeout
    while True:
        try:
            result = runner(
                ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0 and result.stdout.strip() == "yes":
                return True
        except FileNotFoundError:
            return False
        except Exception:
            pass
        if monotonic() >= deadline:
            return False
        sleeper(min(poll, max(0.0, deadline - monotonic())))


class ClockWatch:
    """Notices the wall clock moving against the monotonic clock (an NTP step)."""

    def __init__(self):
        self._wall = now()
        self._mono = monotonic()

    def check(self) -> float:
        """Drift in seconds since the last check (positive = wall clock jumped forward)."""
        wall, mono = now(), monotonic()
        drift = (wall - self._wall) - (mono - self._mono)
        self._wall, self._mono = wall, mono
        return drift


def local_now(ts: Optional[float] = None) -> datetime:
    """The Pi's local time (zone from the OS), as an aware datetime."""
    return datetime.fromtimestamp(now() if ts is None else ts).astimezone()


class LocalSchedule:
    """Fires once per matching local-time minute, e.g. Sunday 04:00 or every day 00:05.

    ``weekday`` is Monday=0 … Sunday=6, or None for every day.
    """

    def __init__(self, hour: int, minute: int, weekday: Optional[int] = None):
        self.hour = hour
        self.minute = minute
        self.weekday = weekday
        self._last_fired: Optional[str] = None

    def due(self, ts: Optional[float] = None) -> bool:
        local = local_now(ts)
        if local.hour != self.hour or local.minute != self.minute:
            return False
        if self.weekday is not None and local.weekday() != self.weekday:
            return False
        key = local.strftime("%Y-%m-%d %H:%M")
        if key == self._last_fired:
            return False
        self._last_fired = key
        return True
