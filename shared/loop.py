"""Single-threaded scheduler used by every app.

Tasks run one after another; an exception in a task is logged and the task
keeps its schedule. SIGTERM/SIGINT stop the loop cleanly.
"""

import signal
from typing import Callable, Iterable, List, Optional

from shared import clock
from shared.heartbeat import Heartbeat, SystemdNotifier

IDLE_SLEEP = 0.2


class Task:
    def __init__(self, name: str, interval: float, func: Callable[[], None],
                 aligned: bool = False, first_run_immediately: bool = True, initial_delay: float = 0.0,
                 offset: float = 0.0):
        if interval <= 0:
            raise ValueError("interval must be positive")
        if not 0 <= offset < interval:
            raise ValueError("offset must be in [0, interval)")
        self.name = name
        self.interval = float(interval)
        self.func = func
        self.aligned = aligned
        self.first_run_immediately = first_run_immediately
        self.initial_delay = float(initial_delay)
        self.offset = float(offset)  # delay after the wall-clock mark for aligned tasks
        self.next_due: Optional[float] = None
        self.runs = 0
        self.failures = 0
        self._retry: Optional[float] = None

    def retry_in(self, seconds: float) -> None:
        """Called from inside the task to run again after `seconds` instead of the interval."""
        self._retry = float(seconds)

    def schedule(self, now: float) -> None:
        if self.first_run_immediately:
            self.next_due = now + self.initial_delay
        else:
            self.next_due = self._next_mark(now) if self.aligned else now + self.interval

    def _next_mark(self, now: float) -> float:
        return clock.next_aligned(self.interval, now - self.offset) + self.offset

    def due(self, now: float) -> bool:
        if self.next_due is None:
            self.schedule(now)
        # If the wall clock stepped backwards, next_due may be far in the
        # future; re-arm the task rather than waiting for it.
        if self.next_due - now > 2 * self.interval:
            self.next_due = now
        return now >= self.next_due

    def run(self, now: float, log) -> None:
        self.runs += 1
        try:
            self.func()
        except Exception:
            self.failures += 1
            log.exception("loop", "task_failed", task=self.name)
        finally:
            # Reschedule from the finish time so a stalled run doesn't
            # cause a burst of catch-up runs.
            self._reschedule(clock.now())

    def _reschedule(self, now: float) -> None:
        if self._retry is not None:
            self.next_due, self._retry = now + self._retry, None
            return
        if self.aligned:
            self.next_due = self._next_mark(now)
            return
        self.next_due += self.interval
        while self.next_due <= now:  # skip missed runs after a long stall
            self.next_due += self.interval


class Loop:
    def __init__(self, log, notifier: Optional[SystemdNotifier], tasks: Iterable[Task],
                 idle_sleep: float = IDLE_SLEEP):
        self.log = log
        self.notifier = notifier or SystemdNotifier(address="")
        self.tasks: List[Task] = list(tasks)
        self.idle_sleep = idle_sleep
        self.running = False
        self.stop_reason: Optional[str] = None
        self.passes = 0

    def stop(self, reason: str) -> None:
        self.running = False
        if self.stop_reason is None:
            self.stop_reason = reason

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.stop(signal.Signals(signum).name)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _handler)
            except ValueError:
                pass  # not the main thread (tests, the demo)

    def run_once(self) -> None:
        """Run due tasks, send the heartbeat and check for clock jumps."""
        now = clock.now()
        for task in self.tasks:
            if task.due(now):
                task.run(now, self.log)
                now = clock.now()
        self._heartbeat.tick()
        drift = self._watch.check()
        if abs(drift) > clock.CLOCK_JUMP_SECONDS:
            self.log.event("warning", "app", "clock_jump",
                           "wall clock moved against the monotonic clock", seconds=round(drift, 1))
        self.passes += 1

    def run(self, max_passes: Optional[int] = None) -> str:
        """Run until stopped (or max_passes, for tests) and return the stop reason."""
        self._heartbeat = Heartbeat(self.notifier, monotonic=clock.monotonic)
        self._watch = clock.ClockWatch()
        self.running = True
        self.stop_reason = None
        self.passes = 0
        start = clock.now()
        for task in self.tasks:
            task.schedule(start)
        self.notifier.ready()
        try:
            while self.running:
                self.run_once()
                if max_passes is not None and self.passes >= max_passes:
                    self.stop("max_passes")
                    break
                clock.sleep(self.idle_sleep)
        finally:
            self.notifier.stopping()
        return self.stop_reason or "stopped"
