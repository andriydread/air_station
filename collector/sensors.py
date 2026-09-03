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


# --- SCD41 ------------------------------------------------------------------------

SCD41_DATA_READY_WAIT = 6.0      # the sensor produces a value every 5 s
SCD41_DATA_READY_POLL = 0.5
SCD41_REINIT_SETTLE = 1.0        # datasheet: up to 1000 ms after reinit before commands
PRESSURE_MIN_DELTA_HPA = 1.0
CAL_MIN_RUNTIME = 180            # seconds the sensor must run before a forced calibration
CAL_MIN_SAMPLES = 3
CAL_MAX_SPREAD = 30              # ppm between the highest and lowest recent reading
CAL_MAX_DELTA = 200              # ppm between the recent average and the target
CAL_WINDOW = 300                 # seconds of recent readings considered
CAL_REJECTED = 0xFFFF


class CalibrationRefused(RuntimeError):
    """A forced calibration was not attempted because a safety check failed."""


class Scd41(Sensor):
    name = "scd41"
    warmup_seconds = SCD41_WARMUP

    def __init__(self, i2c, config, log, sleep=time.sleep, monotonic=time.monotonic):
        super().__init__(log)
        self.i2c = i2c
        self.config = config
        self.sleep = sleep
        self.monotonic = monotonic
        self.asc = bool(config.sensors.asc)
        self.pressure_hpa: Optional[float] = None
        self.recent: list = []  # (ts, ppm) of accepted readings inside CAL_WINDOW

    def _open(self):
        import adafruit_scd4x  # imported here: only the collector has the library

        device = adafruit_scd4x.SCD4X(self.i2c)
        try:
            device.stop_periodic_measurement()  # a previous run may have left it measuring
        except Exception:
            pass
        device.reinit()
        self.sleep(SCD41_REINIT_SETTLE)
        serial = getattr(device, "serial_number", None)
        if serial is not None:
            try:
                self.health.id = "".join(f"{int(word):04x}" for word in serial)
            except (TypeError, ValueError):
                self.health.id = str(serial)
        self._configure_and_start(device)
        return device

    def _configure_and_start(self, device) -> None:
        # Settings live in RAM only (no EEPROM wear) and must be written in
        # idle mode, i.e. before start_periodic_measurement.
        device.altitude = int(self.config.location.altitude_m)
        device.temperature_offset = float(self.config.sensors.scd41_temp_offset_c)
        device.self_calibration_enabled = bool(self.config.sensors.asc)
        self.asc = bool(device.self_calibration_enabled)
        if self.pressure_hpa is not None:
            device.set_ambient_pressure(int(round(self.pressure_hpa)))
        device.start_periodic_measurement()
        self.recent.clear()

    def _close(self, device) -> None:
        device.stop_periodic_measurement()

    def read(self, now: float, deadline_s: float = SCD41_DATA_READY_WAIT) -> Optional[Dict[str, float]]:
        """Wait up to ``deadline_s`` for a fresh value; None when none came.

        Returns the raw numbers as the sensor gave them — CO2 plus the SCD41's
        own temperature and humidity. I2C errors propagate to the sampler.
        """
        if self.device is None:
            return None
        started = self.monotonic()
        while True:
            if self.device.data_ready:
                return {
                    "co2": float(self.device.CO2),
                    "co2_temp": float(self.device.temperature),
                    "co2_humid": float(self.device.relative_humidity),
                }
            if self.monotonic() - started >= deadline_s:
                return None
            self.sleep(SCD41_DATA_READY_POLL)

    def record_valid(self, now: float, co2: float) -> None:
        self.recent.append((now, float(co2)))
        self.recent = [(ts, ppm) for ts, ppm in self.recent if now - ts <= CAL_WINDOW]

    def set_ambient_pressure(self, hpa: float) -> bool:
        """Pass the live air pressure on when it moved by ≥ 1 hPa; True when sent."""
        if hpa is None:
            return False
        if self.pressure_hpa is not None and abs(hpa - self.pressure_hpa) < PRESSURE_MIN_DELTA_HPA:
            return False
        if self.device is not None:
            self.device.set_ambient_pressure(int(round(hpa)))
        self.pressure_hpa = float(hpa)
        return True

    def runtime_seconds(self, now: float) -> int:
        if self.device is None or self.warmup_started_at is None:
            return 0
        return int(now - self.warmup_started_at)

    def calibration_readiness(self, now: float) -> Dict[str, Any]:
        samples = [ppm for ts, ppm in self.recent if now - ts <= CAL_WINDOW]
        average = sum(samples) / len(samples) if samples else None
        spread = max(samples) - min(samples) if samples else None
        return {
            "runtime_seconds": self.runtime_seconds(now),
            "sample_count": len(samples),
            "average_co2": round(average, 1) if average is not None else None,
            "spread_co2": round(spread, 1) if spread is not None else None,
        }

    def check_preconditions(self, now: float, target_ppm: int, allow_large_offset: bool = False) -> Dict[str, Any]:
        if self.device is None:
            raise CalibrationRefused("SCD41 is not initialised")
        runtime = self.runtime_seconds(now)
        if runtime < CAL_MIN_RUNTIME:
            raise CalibrationRefused(
                f"SCD41 must run for {CAL_MIN_RUNTIME} s before calibration; current runtime is {runtime} s")
        samples = [ppm for ts, ppm in self.recent if now - ts <= CAL_WINDOW]
        if len(samples) < CAL_MIN_SAMPLES:
            raise CalibrationRefused(
                f"Not enough recent valid samples: need {CAL_MIN_SAMPLES}, have {len(samples)}")
        spread = max(samples) - min(samples)
        if spread > CAL_MAX_SPREAD:
            raise CalibrationRefused(
                f"Readings not stable enough: spread is {spread:.1f} ppm, limit is {CAL_MAX_SPREAD} ppm")
        average = sum(samples) / len(samples)
        delta = abs(average - target_ppm)
        if delta > CAL_MAX_DELTA and not allow_large_offset:
            raise CalibrationRefused(
                f"Readings average {average:.1f} ppm, more than {CAL_MAX_DELTA} ppm from target "
                f"{target_ppm} ppm. If the air here is NOT at the target level, ventilate the room "
                "or move the station to fresh outdoor air (~420 ppm), wait ~10 minutes, then retry. "
                "Only if the sensor itself has drifted far off, retry with the drift override enabled.")
        return {
            "runtime_seconds": runtime, "sample_count": len(samples),
            "average_co2": round(average, 1), "spread_co2": round(spread, 1),
            "reference_delta_ppm": round(delta, 1), "large_offset_allowed": allow_large_offset,
        }

    def force_calibration(self, now: float, target_ppm: int, allow_large_offset: bool = False,
                          persist: bool = False) -> Dict[str, Any]:
        """Forced recalibration after the safety checks; restarts measurement (new warm-up)."""
        checks = self.check_preconditions(now, target_ppm, allow_large_offset)
        device = self.device
        device.stop_periodic_measurement()
        self.sleep(1.0)
        try:
            correction = device.force_calibration(int(target_ppm))
            if correction == CAL_REJECTED:
                raise RuntimeError("SCD41 rejected the forced calibration command (0xFFFF)")
            if persist:
                device.persist_settings()
        finally:
            self._configure_and_start(device)
            self.warmup_started_at = now
            self.last_data_at = now
        return {"correction_ppm": int(correction), "target_ppm": int(target_ppm),
                "persisted": bool(persist), **checks}
