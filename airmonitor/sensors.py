"""Sensor wrappers.

Each class hides one piece of hardware behind two simple ideas:

- ``read()`` returns fresh values, or ``None`` when nothing valid is available.
  It never raises: failures are logged and reflected in ``health``.
- ``health`` is a small status dict (available / healthy / last_error / ...)
  that the dashboard displays.

The SCD41 wrapper also re-initializes the sensor automatically after a long
streak of invalid readings — the sensor can get stuck returning 0 ppm until
it is restarted (this happened in production in July 2026).
"""

import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import adafruit_scd4x
import adafruit_sht4x

from airmonitor.validation import VALID_HUMIDITY, VALID_TEMPERATURE
from lib.sps30_i2c import SPS30

LOGGER = logging.getLogger("airmonitor")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Hard read errors in a row before a present-but-broken device is re-created.
READ_FAILURE_REINIT_THRESHOLD = 30

# The SPS30 fan cleaning runs ~10s at full speed; readings taken during it
# are not representative air, so they are blanked (with margin).
FAN_CLEAN_BLANK_SECONDS = 15.0


class ReinitBackoff:
    """Paces re-initialization attempts: 30s doubling up to 5 minutes."""

    INITIAL_DELAY = 30.0
    MAX_DELAY = 300.0

    def __init__(self):
        self.delay = self.INITIAL_DELAY
        self.next_attempt = 0.0  # monotonic timestamp; 0 = due immediately

    def due(self) -> bool:
        return time.monotonic() >= self.next_attempt

    def failed(self) -> None:
        self.next_attempt = time.monotonic() + self.delay
        self.delay = min(self.delay * 2, self.MAX_DELAY)

    def reset(self) -> None:
        self.delay = self.INITIAL_DELAY
        self.next_attempt = 0.0


class SensorHealth:
    """Tracks availability/health of one sensor and logs state changes."""

    def __init__(self, name: str, events):
        self.name = name
        self.events = events
        self.state: Dict[str, Any] = {
            "available": False,
            "healthy": False,
            "last_error": None,
        }

    def update(self, *, available: bool, healthy: bool, error: Optional[str] = None) -> None:
        changed = (
            self.state["available"] != available
            or self.state["healthy"] != healthy
            or self.state["last_error"] != error
        )
        self.state.update(available=available, healthy=healthy, last_error=error)
        self.state["last_event_at"] = utc_now_iso()
        if changed:
            level = logging.INFO if healthy else logging.WARNING
            message = f"{self.name} state changed: available={available} healthy={healthy}"
            if error:
                message += f"; error={error}"
            self.events.log(level, self.name, "state_change", message, dict(self.state))

    def ok(self) -> None:
        self.update(available=True, healthy=True)

    def failed(self, error: str, *, available: bool = True) -> None:
        self.update(available=available, healthy=False, error=error)


class Scd41:
    """SCD41 CO2 sensor (I2C)."""

    def __init__(self, i2c, config, events):
        self.i2c = i2c
        self.config = config
        self.events = events
        self.health = SensorHealth("scd41", events)
        self.device = None
        self.asc_enabled = config.scd41_asc_enabled
        self.invalid_streak = 0
        self.failure_streak = 0
        # The SCD41's own ambient readings, refreshed on every valid CO2
        # read; the cross-check compares them against the SHT41.
        self.last_temperature: Optional[float] = None
        self.last_humidity: Optional[float] = None
        self.measurement_started_at: Optional[float] = None
        # (monotonic time, ppm) pairs used by the calibration safety checks
        self.recent_valid_samples: deque = deque()
        self._backoff = ReinitBackoff()
        self._try_init()

    def _try_init(self) -> bool:
        try:
            self.device = adafruit_scd4x.SCD4X(self.i2c)
            self._start_measurement()
            self.failure_streak = 0
            self.health.ok()
            self._backoff.reset()
            return True
        except Exception as exc:
            LOGGER.exception("Failed to initialize SCD41")
            self.device = None
            self.health.failed(str(exc), available=False)
            self._backoff.failed()
            return False

    def ensure(self) -> None:
        """Retry initialization (with backoff) if the device is missing."""
        if self.device is None and self._backoff.due():
            LOGGER.info("Attempting SCD41 initialization")
            self._try_init()

    def _start_measurement(self) -> None:
        try:
            self.device.altitude = self.config.scd41_altitude_m
        except Exception:
            # Altitude compensation is an accuracy improvement, never a
            # reason to refuse to measure.
            LOGGER.warning("Failed to set SCD41 altitude", exc_info=True)
        self.device.self_calibration_enabled = self.asc_enabled
        self.asc_enabled = bool(self.device.self_calibration_enabled)
        self.device.start_periodic_measurement()
        self.measurement_started_at = time.monotonic()
        self.recent_valid_samples.clear()
        self.invalid_streak = 0

    def read(self) -> Optional[float]:
        """Return a valid CO2 reading in ppm, or None."""
        if self.device is None:
            return None
        try:
            if not self.device.data_ready:
                self.failure_streak = 0
                return None
            co2 = float(self.device.CO2)
            # The transaction itself worked, so the bus/device are alive —
            # even when the value turns out to be implausible.
            self.failure_streak = 0
            if co2 < self.config.min_valid_co2_ppm:
                self._handle_invalid_reading(co2)
                return None
            self.invalid_streak = 0
            now = time.monotonic()
            self.recent_valid_samples.append((now, co2))
            self._trim_recent_samples(now)
            try:
                self.last_temperature = float(self.device.temperature)
                self.last_humidity = float(self.device.relative_humidity)
            except Exception:
                self.last_temperature = None
                self.last_humidity = None
            self.health.ok()
            return co2
        except Exception as exc:
            LOGGER.exception("Failed to read SCD41")
            self.health.failed(str(exc))
            self.events.log(logging.ERROR, "scd41", "read_failed", f"Failed to read SCD41: {exc}")
            self._register_read_failure()
            return None

    def _register_read_failure(self) -> None:
        """Re-create the device after a long streak of hard read errors.

        Distinct from the invalid-value path (`invalid_streak`), which covers
        a working bus returning implausible ppm: this one covers I2C-level
        failures that otherwise persist until a service restart.
        """
        self.failure_streak += 1
        if self.failure_streak >= READ_FAILURE_REINIT_THRESHOLD:
            self.events.log(
                logging.WARNING, "scd41", "auto_reinit",
                f"Re-initializing SCD41 after {self.failure_streak} failed reads in a row",
            )
            self.device = None
            self._try_init()

    def _handle_invalid_reading(self, co2: float) -> None:
        self.invalid_streak += 1
        self.health.failed(f"Invalid CO2 reading: {co2:.1f} ppm")
        # Log the first bad reading of a streak, then once a minute, not every 10s.
        if self.invalid_streak == 1 or self.invalid_streak % 6 == 0:
            self.events.log(
                logging.WARNING,
                "scd41",
                "invalid_measurement",
                f"Invalid CO2 reading ignored: {co2:.1f} ppm ({self.invalid_streak} in a row)",
                {"co2": co2, "invalid_streak": self.invalid_streak},
            )
        if self.invalid_streak >= self.config.scd41_reinit_after_invalid:
            self.reinitialize()

    def reinitialize(self) -> None:
        """Restart the sensor after it gets stuck (e.g. keeps returning 0 ppm)."""
        self.events.log(
            logging.WARNING,
            "scd41",
            "auto_reinit",
            f"Re-initializing SCD41 after {self.invalid_streak} invalid readings in a row",
        )
        try:
            self.device.stop_periodic_measurement()
            time.sleep(1.0)
            self.device.reinit()
            time.sleep(0.1)
            self._start_measurement()
        except Exception as exc:
            LOGGER.exception("SCD41 re-initialization failed")
            self.health.failed(f"Re-initialization failed: {exc}")
            self.invalid_streak = 0  # avoid retrying every sample

    def _trim_recent_samples(self, now: float) -> None:
        window = self.config.calibration_window
        while self.recent_valid_samples and now - self.recent_valid_samples[0][0] > window:
            self.recent_valid_samples.popleft()

    # --- Commands from the dashboard -------------------------------------

    def runtime_seconds(self) -> Optional[int]:
        if self.measurement_started_at is None:
            return None
        return int(time.monotonic() - self.measurement_started_at)

    def calibration_readiness(self) -> Dict[str, Any]:
        """Live inputs for the dashboard's calibration checklist (never raises).

        Same numbers `check_calibration_preconditions` enforces, but as data:
        the UI shows which conditions pass and enables the button only when
        the sensor is actually ready.
        """
        self._trim_recent_samples(time.monotonic())
        samples = [ppm for _, ppm in self.recent_valid_samples]
        average = sum(samples) / len(samples) if samples else None
        spread = max(samples) - min(samples) if samples else None
        return {
            "runtime_seconds": self.runtime_seconds() or 0,
            "sample_count": len(samples),
            "average_co2": round(average, 1) if average is not None else None,
            "spread_co2": round(spread, 1) if spread is not None else None,
        }

    def check_calibration_preconditions(
        self, target_co2: int, allow_large_offset: bool = False
    ) -> Dict[str, Any]:
        """Refuse a forced calibration unless the sensor is warmed up and stable.

        The distance-to-target check can't tell "room air is genuinely high"
        (calibrating then would program a wrong offset) from "the sensor has
        drifted far off" (the exact thing forced calibration fixes), so
        ``allow_large_offset`` skips that one check; warm-up and stability
        are always enforced.
        """
        cfg = self.config
        runtime = self.runtime_seconds() or 0
        if runtime < cfg.calibration_min_runtime:
            raise RuntimeError(
                f"SCD41 must run for {cfg.calibration_min_runtime}s before calibration; "
                f"current runtime is {runtime}s"
            )
        self._trim_recent_samples(time.monotonic())
        samples = [ppm for _, ppm in self.recent_valid_samples]
        if len(samples) < cfg.calibration_min_samples:
            raise RuntimeError(
                f"Not enough recent valid samples: need {cfg.calibration_min_samples}, have {len(samples)}"
            )
        spread = max(samples) - min(samples)
        if spread > cfg.calibration_max_drift_ppm:
            raise RuntimeError(
                f"Readings not stable enough: spread is {spread:.1f} ppm, "
                f"limit is {cfg.calibration_max_drift_ppm} ppm"
            )
        average = sum(samples) / len(samples)
        delta = abs(average - target_co2)
        if delta > cfg.calibration_max_reference_delta_ppm and not allow_large_offset:
            raise RuntimeError(
                f"Readings average {average:.1f} ppm, more than "
                f"{cfg.calibration_max_reference_delta_ppm} ppm from target {target_co2} ppm. "
                "If the air here is NOT at the target level, ventilate the room or move "
                "the station to fresh outdoor air (~420 ppm), wait ~10 minutes for readings "
                "to settle, then retry. Only if the sensor itself has drifted far off, "
                "retry with the drift override enabled."
            )
        return {
            "runtime_seconds": runtime,
            "sample_count": len(samples),
            "average_co2": round(average, 1),
            "spread_co2": round(spread, 1),
            "reference_delta_ppm": round(delta, 1),
            "large_offset_allowed": allow_large_offset,
        }

    def force_calibration(self, target_co2: int, persist: bool) -> int:
        """Run forced recalibration. Returns the correction offset from the sensor."""
        if self.device is None:
            raise RuntimeError("SCD41 is not initialized")
        self.device.stop_periodic_measurement()
        time.sleep(1.0)
        try:
            correction = self.device.force_calibration(target_co2)
            if correction == 0xFFFF:
                raise RuntimeError("SCD41 rejected the forced calibration command (0xFFFF)")
            if persist:
                self.device.persist_settings()
            return correction
        finally:
            self._start_measurement()

    def set_asc(self, enabled: bool, persist: bool) -> bool:
        """Enable/disable automatic self-calibration. Returns the applied value."""
        if self.device is None:
            raise RuntimeError("SCD41 is not initialized")
        self.device.stop_periodic_measurement()
        time.sleep(1.0)
        try:
            self.asc_enabled = enabled
            self.device.self_calibration_enabled = enabled
            self.asc_enabled = bool(self.device.self_calibration_enabled)
            if persist:
                self.device.persist_settings()
            return self.asc_enabled
        finally:
            self._start_measurement()

    def stop(self) -> None:
        if self.device is None:
            return
        try:
            self.device.stop_periodic_measurement()
        except Exception:
            LOGGER.exception("Failed to stop SCD41")


class Sht41:
    """SHT41 temperature and humidity sensor (I2C)."""

    VALID_TEMPERATURE = VALID_TEMPERATURE
    VALID_HUMIDITY = VALID_HUMIDITY

    def __init__(self, i2c, events, temp_offset: float = 0.0):
        self.i2c = i2c
        self.events = events
        self.temp_offset = temp_offset
        self.health = SensorHealth("sht41", events)
        self.device = None
        self.failure_streak = 0
        self._backoff = ReinitBackoff()
        self._try_init()

    def _try_init(self) -> bool:
        try:
            self.device = adafruit_sht4x.SHT4x(self.i2c)
            self.failure_streak = 0
            self.health.ok()
            self._backoff.reset()
            return True
        except Exception as exc:
            LOGGER.exception("Failed to initialize SHT41")
            self.device = None
            self.health.failed(str(exc), available=False)
            self._backoff.failed()
            return False

    def ensure(self) -> None:
        """Retry initialization (with backoff) if the device is missing."""
        if self.device is None and self._backoff.due():
            LOGGER.info("Attempting SHT41 initialization")
            self._try_init()

    def read(self) -> Optional[Tuple[float, float]]:
        """Return (temperature C, relative humidity %), or None."""
        if self.device is None:
            return None
        try:
            temp = float(self.device.temperature) + self.temp_offset
            humid = float(self.device.relative_humidity)
            if not (self.VALID_TEMPERATURE[0] <= temp <= self.VALID_TEMPERATURE[1]):
                raise ValueError(f"Temperature out of range: {temp:.2f} C")
            if not (self.VALID_HUMIDITY[0] <= humid <= self.VALID_HUMIDITY[1]):
                raise ValueError(f"Humidity out of range: {humid:.2f} %")
            self.failure_streak = 0
            self.health.ok()
            return temp, humid
        except Exception as exc:
            LOGGER.exception("Failed to read SHT41")
            self.health.failed(str(exc))
            self.events.log(logging.ERROR, "sht41", "read_failed", f"Failed to read SHT41: {exc}")
            self._register_read_failure()
            return None

    def _register_read_failure(self) -> None:
        """Re-create the device after a long streak of failed reads."""
        self.failure_streak += 1
        if self.failure_streak >= READ_FAILURE_REINIT_THRESHOLD:
            self.events.log(
                logging.WARNING, "sht41", "auto_reinit",
                f"Re-initializing SHT41 after {self.failure_streak} failed reads in a row",
            )
            self.device = None
            self._try_init()


class Sps30:
    """SPS30 particulate matter sensor (I2C)."""

    FIELDS = ("pm1", "pm25", "pm4", "pm10", "tps")

    def __init__(self, i2c, config, events):
        self.i2c = i2c
        self.config = config
        self.events = events
        self.health = SensorHealth("sps30", events)
        self.device = None
        self.auto_cleaning_interval: Optional[int] = None
        self.last_manual_clean_at: Optional[float] = None
        self._blank_until: Optional[float] = None
        self.failure_streak = 0
        self._backoff = ReinitBackoff()
        self._try_init()

    def _try_init(self) -> bool:
        try:
            device = SPS30(self.i2c)
            device.wakeup()
            device.start_measurement()
            self.device = device
            self.auto_cleaning_interval = device.auto_cleaning_interval
            self.failure_streak = 0
            self.health.ok()
            self._backoff.reset()
            return True
        except Exception as exc:
            LOGGER.exception("Failed to initialize SPS30")
            self.device = None
            self.health.failed(str(exc), available=False)
            self._backoff.failed()
            return False

    def ensure(self) -> None:
        """Retry initialization (with backoff) if the device is missing."""
        if self.device is None and self._backoff.due():
            LOGGER.info("Attempting SPS30 initialization")
            self._try_init()

    def read(self) -> Optional[Dict[str, float]]:
        """Return {"pm1": ..., "pm25": ..., "pm4": ..., "pm10": ..., "tps": ...}, or None."""
        if self.device is None:
            return None
        if self._blank_until is not None:
            if time.monotonic() < self._blank_until:
                return None  # fan cleaning in progress: not representative air
            self._blank_until = None
        try:
            if not self.device.data_ready:
                return None
            data = self.device.read()
            values = {}
            for field in self.FIELDS:
                value = float(data[field])
                if value < 0:
                    raise ValueError(f"{field} must not be negative")
                values[field] = value
            self.failure_streak = 0
            self.health.ok()
            return values
        except Exception as exc:
            LOGGER.exception("Failed to read SPS30")
            self.health.failed(str(exc))
            self.events.log(logging.ERROR, "sps30", "read_failed", f"Failed to read SPS30: {exc}")
            self._register_read_failure()
            return None

    def _register_read_failure(self) -> None:
        """Re-create the device after a long streak of failed reads.

        The SCD41 has had this since the July 2026 stuck-sensor incident;
        the SPS30 gets the same treatment for hard errors (CRC failures,
        bus errors), which previously persisted until a service restart.
        """
        self.failure_streak += 1
        if self.failure_streak >= READ_FAILURE_REINIT_THRESHOLD:
            self.events.log(
                logging.WARNING, "sps30", "auto_reinit",
                f"Re-initializing SPS30 after {self.failure_streak} failed reads in a row",
            )
            self.device = None
            self._try_init()

    def force_clean(self) -> None:
        """Start a manual fan cleaning (rate-limited)."""
        if self.device is None:
            raise RuntimeError("SPS30 is not initialized")
        now = time.monotonic()
        cooldown = self.config.sps30_manual_clean_cooldown
        if self.last_manual_clean_at is not None and now - self.last_manual_clean_at < cooldown:
            remaining = int(cooldown - (now - self.last_manual_clean_at))
            raise RuntimeError(f"Fan cleaning is rate-limited; wait another {remaining}s")
        self.device.force_clean()
        self.last_manual_clean_at = now
        self._blank_until = now + FAN_CLEAN_BLANK_SECONDS
        self.health.state["last_manual_clean_at"] = utc_now_iso()

    def set_auto_cleaning_interval(self, seconds: int) -> None:
        if self.device is None:
            raise RuntimeError("SPS30 is not initialized")
        self.device.auto_cleaning_interval = seconds
        self.auto_cleaning_interval = seconds

    def stop(self) -> None:
        if self.device is None:
            return
        try:
            self.device.stop_measurement()
            self.device.sleep()
        except Exception:
            LOGGER.exception("Failed to stop SPS30")
