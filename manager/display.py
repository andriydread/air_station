"""The e-paper wrapper: create the screen, show a picture, choose partial or
full refresh, recover when it hangs.

Partial refresh every minute, a full one every 5 minutes against ghosting.
A BUSY-pin timeout (15 s in the driver) closes the driver and re-creates it
with the shared backoff; the first frame after a recovery is a full one.
The picture itself comes from ``shared/render.py``; this module only pushes
it over SPI, inline, on the manager's single thread.
"""

import time
from typing import Any, Callable, Dict, Optional

from shared.backoff import ReinitBackoff

FULL_REFRESH_EVERY = 300.0
BUSY_TIMEOUT = 15.0  # the driver's own limit; documented here, set there


def _default_driver():
    from drivers.uc8253c import UC8253C_SPI  # RPi.GPIO / spidev: only on the Pi (or faked)
    return UC8253C_SPI(rotation=90)


class Panel:
    def __init__(self, log, driver_factory: Optional[Callable[[], Any]] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.log = log
        self.factory = driver_factory or _default_driver
        self.monotonic = monotonic
        self.driver: Any = None
        self.backoff = ReinitBackoff()
        self.available = False
        self.healthy = False
        self.last_error: Optional[str] = None
        self.last_full_at: Optional[int] = None
        self.last_partial_at: Optional[int] = None
        self.next_full_at: Optional[float] = None
        self.force_full = False
        self.busy_ms: Optional[float] = None
        self.render_ms: Optional[float] = None
        self.frames = 0
        self.failures = 0
        self.reinit_count = 0

    def ensure(self, now: float) -> bool:
        if self.driver is not None:
            return True
        if not self.backoff.due(now):
            return False
        try:
            self.driver = self.factory()
        except Exception as exc:
            self.driver = None
            self.available = False
            self.healthy = False
            self.last_error = f"init failed: {exc}"
            delay = self.backoff.failed(now)
            self.log.warning("display", "init_failed", error=str(exc), retry_in=delay)
            if self.backoff.failures == 1:
                self.log.event("error", "display", "display_error",
                               f"e-paper did not initialise: {exc}", error=str(exc))
            return False
        self.backoff.reset()
        self.available = True
        self.healthy = True
        self.last_error = None
        if self.failures:
            self.reinit_count += 1
            self.log.event("info", "display", "display_reinit", "e-paper re-initialised",
                           count=self.reinit_count)
        self.force_full = True  # unknown panel contents: start with a clean full frame
        return True

    def show(self, image, now: float, full: Optional[bool] = None) -> Optional[str]:
        """Push a frame; returns "full" / "partial", or None when the panel is unavailable."""
        if not self.ensure(now):
            return None
        if full is None:
            full = self.force_full or self.next_full_at is None or now >= self.next_full_at
        mode = self.driver.MODE_FULL if full else self.driver.MODE_PARTIAL
        started = self.monotonic()
        try:
            self.driver.display_image(image, mode=mode, auto_sleep=True)
        except Exception as exc:
            self.busy_ms = round((self.monotonic() - started) * 1000)
            self._failed(exc, now)
            return None
        self.busy_ms = round((self.monotonic() - started) * 1000)
        self.frames += 1
        self.healthy = True
        self.last_error = None
        self.force_full = False
        if full:
            self.last_full_at = int(now)
            self.next_full_at = now + FULL_REFRESH_EVERY
        else:
            self.last_partial_at = int(now)
        return "full" if full else "partial"

    def _failed(self, exc: Exception, now: float) -> None:
        self.failures += 1
        self.healthy = False
        self.last_error = f"{exc.__class__.__name__}: {exc}"
        self.log.event("error", "display", "display_error", f"e-paper refresh failed: {exc}",
                       error=self.last_error, failures=self.failures)
        driver, self.driver = self.driver, None
        try:
            driver.close()
        except Exception:
            pass
        self.backoff.failed(now)
        self.force_full = True

    def sleep(self) -> None:
        if self.driver is not None:
            try:
                self.driver.sleep()
            except Exception as exc:
                self.log.warning("display", "sleep_failed", error=str(exc))

    def close(self) -> None:
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.available, "healthy": self.healthy, "last_error": self.last_error,
            "last_full_at": self.last_full_at, "last_partial_at": self.last_partial_at,
            "render_ms": self.render_ms, "busy_ms": self.busy_ms,
            "frames": self.frames, "failures": self.failures, "reinit_count": self.reinit_count,
        }
