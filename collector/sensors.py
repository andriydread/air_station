"""The three sensor wrappers and the bookkeeping they share.

Every sensor: is it there, is it healthy, when to try again, when to give up
and restart it. Two things trigger a re-init — six bad readings in a row, or
two minutes without any reading after warm-up. Warm-up after a (re)start
(60 s CO2, 30 s dust) is not counted against the sensor at all.
"""

import time
from typing import Any, Dict, Optional

from shared.backoff import ReinitBackoff

BAD_STREAK_REINIT = 6        # bad readings in a row (one minute at 10 s) → re-init
SILENCE_REINIT = 120.0       # seconds without any reading after warm-up → re-init
SCD41_WARMUP = 60
SPS30_WARMUP = 30
SHT41_WARMUP = 0


class SensorHealth:
    """The status dict the collector publishes for one sensor."""

    def __init__(self, name: str):
        self.name = name
        self.available = False
        self.healthy = False
        self.last_error: Optional[str] = None
        self.last_ok_at: Optional[int] = None
        self.id: Optional[str] = None

    def ok(self, now: float) -> None:
        self.available = True
        self.healthy = True
        self.last_error = None
        self.last_ok_at = int(now)

    def failed(self, error: str, available: bool = True) -> None:
        self.available = available
        self.healthy = False
        self.last_error = error


class Sensor:
    """Base for the wrappers: init with backoff, streaks, silence, status."""

    name = "sensor"
    warmup_seconds = 0

    def __init__(self, log):
        self.log = log
        self.device: Any = None
        self.health = SensorHealth(self.name)
        self.backoff = ReinitBackoff()
        self.bad_streak = 0
        self.reinit_count = 0
        self.init_failures_in_row = 0
        self.last_data_at: Optional[float] = None
        self.warmup_started_at: Optional[float] = None

    # --- hooks for the subclasses ------------------------------------------------

    def _open(self) -> Any:
        raise NotImplementedError

    def _close(self, device: Any) -> None:
        pass

    # --- lifecycle -----------------------------------------------------------------

    def ensure(self, now: float) -> bool:
        """Init the device when missing and the backoff allows; True when present."""
        if self.device is None and self.backoff.due(now):
            self._init_once(now)
        return self.device is not None

    def _init_once(self, now: float) -> bool:
        try:
            self.device = self._open()
        except Exception as exc:
            self.device = None
            delay = self.backoff.failed(now)
            self.init_failures_in_row += 1
            self.health.failed(f"init failed: {exc}", available=False)
            self.log.warning(self.name, "init_failed", error=str(exc), retry_in=delay,
                             attempt=self.init_failures_in_row)
            if self.init_failures_in_row == 1:
                self.log.event("error", self.name, "sensor_error",
                               f"{self.name} did not initialise: {exc}", error=str(exc))
            return False
        self.backoff.reset()
        self.init_failures_in_row = 0
        self.bad_streak = 0
        self.warmup_started_at = now
        self.last_data_at = now
        self.health.ok(now)
        self.log.event("info", self.name, "sensor_init", f"{self.name} initialised",
                       warmup_s=self.warmup_seconds, id=self.health.id)
        return True

    def reinit(self, now: float, reason: str) -> bool:
        self.reinit_count += 1
        self.log.event("warning", self.name, "sensor_reinit",
                       f"re-initialising {self.name}: {reason}", reason=reason,
                       count=self.reinit_count)
        device, self.device = self.device, None
        if device is not None:
            try:
                self._close(device)
            except Exception as exc:
                self.log.warning(self.name, "close_failed", error=str(exc))
        self.bad_streak = 0
        return self._init_once(now)

    def stop(self) -> None:
        device, self.device = self.device, None
        if device is not None:
            try:
                self._close(device)
            except Exception as exc:
                self.log.warning(self.name, "close_failed", error=str(exc))

    # --- per-beat bookkeeping ----------------------------------------------------------

    def warmup_left(self, now: float) -> float:
        if self.device is None or self.warmup_started_at is None:
            return 0.0
        return max(0.0, self.warmup_started_at + self.warmup_seconds - now)

    def note_ok(self, now: float) -> None:
        self.bad_streak = 0
        self.last_data_at = now
        self.health.ok(now)

    def note_bad(self, now: float, error: str) -> bool:
        """A garbage value or a failed read; True when this one triggered a re-init."""
        self.bad_streak += 1
        self.health.failed(error)
        if self.bad_streak >= BAD_STREAK_REINIT:
            self.reinit(now, f"{self.bad_streak} bad readings in a row")
            return True
        return False

    def check_silence(self, now: float) -> bool:
        """No reading for SILENCE_REINIT after warm-up → re-init; True when it fired."""
        if self.device is None or self.last_data_at is None:
            return False
        quiet_since = max(self.last_data_at, (self.warmup_started_at or 0) + self.warmup_seconds)
        if now - quiet_since >= SILENCE_REINIT:
            self.health.failed(f"no reading for {int(now - quiet_since)} s")
            self.reinit(now, f"silent for {int(now - quiet_since)} s")
            return True
        return False

    def status(self, now: float) -> Dict[str, Any]:
        return {
            "available": self.health.available,
            "healthy": self.health.healthy,
            "last_error": self.health.last_error,
            "last_ok_at": self.health.last_ok_at,
            "warmup_left": int(round(self.warmup_left(now))),
            "reinit_count": self.reinit_count,
            "id": self.health.id,
        }
