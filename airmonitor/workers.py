"""Background workers that keep slow subsystems off the sampling loop.

The e-paper refresh can busy-wait up to 15s and the network calls block
for seconds; on a single thread that starved the 10s sensor cadence and,
in the worst case, the whole loop. Two small primitives fix it:

- ``DisplayWorker``: renders frames on its own thread from a latest-wins
  slot — the main loop hands over a snapshot and returns instantly. A
  render stuck longer than ``wedge_timeout`` is reported (once) so a hung
  panel shows up in the events instead of silently freezing the clock.
- ``PeriodicWorker``: runs one function on a fixed interval on its own
  thread (weather fetch, connectivity probe). One failure never kills it.

Both assume everything they call is thread-safe: storage locks internally
(B1), health/event writes go through the locked database, and the shared
``weather``/``network_state`` dicts are swapped by reference, never
mutated in place.
"""

import logging
import queue
import threading
import time
from typing import Any, Callable, Optional, Tuple

LOGGER = logging.getLogger("airmonitor")


class DisplayWorker:
    def __init__(self, render: Callable[[Any, bool], None], events, wedge_timeout: float = 120.0):
        self._render = render
        self.events = events
        self.wedge_timeout = wedge_timeout
        self._slot: "queue.Queue[Tuple[Any, bool]]" = queue.Queue(maxsize=1)
        self._lock = threading.Lock()
        self._render_started_at: Optional[float] = None
        self._wedge_reported = False
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="display-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def submit(self, snapshot: Any, full_refresh: bool) -> None:
        """Queue a frame without blocking; a newer frame replaces an unrendered one."""
        item = (snapshot, full_refresh)
        while True:
            try:
                self._slot.put_nowait(item)
                return
            except queue.Full:
                try:
                    self._slot.get_nowait()
                except queue.Empty:
                    pass

    def check_wedged(self) -> bool:
        """Called from the main loop; reports a stuck render once per incident."""
        with self._lock:
            started = self._render_started_at
        if started is None:
            return False
        stuck_for = time.monotonic() - started
        if stuck_for > self.wedge_timeout and not self._wedge_reported:
            self._wedge_reported = True
            self.events.log(
                logging.ERROR, "display", "worker_wedged",
                f"Display render stuck for {int(stuck_for)}s; sampling continues unaffected",
            )
        return self._wedge_reported

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                snapshot, full_refresh = self._slot.get(timeout=0.5)
            except queue.Empty:
                continue
            with self._lock:
                self._render_started_at = time.monotonic()
            try:
                self._render(snapshot, full_refresh)
            except Exception:  # the render func handles its own errors; belt and braces
                LOGGER.exception("Display worker render failed")
            finally:
                with self._lock:
                    self._render_started_at = None
                    if self._wedge_reported:
                        self._wedge_reported = False
                        self.events.log(
                            logging.INFO, "display", "worker_recovered",
                            "Display render completed after being stuck",
                        )

    def stop(self, timeout: float = 20.0) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()


class PeriodicWorker:
    def __init__(self, name: str, interval: float, func: Callable[[], None], events):
        self.name = name
        self.interval = interval
        self.func = func
        self.events = events
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"{name}-worker", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.func()
            except Exception as exc:  # noqa: BLE001 - worker must survive anything
                LOGGER.exception("%s worker iteration failed", self.name)
                try:
                    self.events.log(
                        logging.ERROR, self.name, "task_failed",
                        f"{self.name} worker iteration failed: {exc}",
                    )
                except Exception:
                    LOGGER.exception("Failed to log %s worker failure", self.name)
            self._stop_event.wait(self.interval)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()
