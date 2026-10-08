"""Wrappers for the SHT41, SPS30 and SCD41, plus shared init/reset handling.

After each (re)start a sensor is left alone until ready_at (the first whole
minute at least QUIET_SECONDS away). Too many bad readings, or a minute of
silence, closes and reopens it.
"""

import math
import time
from typing import Tuple, Any, Dict, Optional

from shared.backoff import ReinitBackoff

QUIET_SECONDS = 60
BAD_STREAK_REINIT = 6        # bad readings in a row (one minute at 10 s)
BAD_WINDOW_S = 180           # or BAD_WINDOW_COUNT bad readings within this window
BAD_WINDOW_COUNT = 6
SILENCE_REINIT = 60.0        # seconds without data after ready_at


def ready_after(now: float) -> int:
    """The first whole minute at least QUIET_SECONDS after now."""
    return int(math.ceil((now + QUIET_SECONDS) / 60.0) * 60)


class SensorHealth:
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
    """Base class: init with backoff, quiet time, bad-reading and silence resets."""

    name = "sensor"
    init_details: Dict[str, Any] = {}  # extra fields on the sensor_init event

    def __init__(self, log):
        self.log = log
        self.device: Any = None
        self.health = SensorHealth(self.name)
        self.backoff = ReinitBackoff()
        self.bad_streak = 0
        self.bad_times: list = []  # wall-clock stamps of bad readings inside the window
        self.reinit_count = 0
        self.init_failures_in_row = 0
        self.inited_at: Optional[float] = None
        self.ready_at: Optional[int] = None
        self.last_data_at: Optional[float] = None

    def _open(self) -> Any:
        raise NotImplementedError

    def _close(self, device: Any) -> None:
        pass

    def ensure(self, now: float) -> bool:
        """Open the device if missing and the backoff allows; True if present."""
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
        self.bad_times = []
        self.inited_at = now
        self.ready_at = ready_after(now)
        self.last_data_at = None
        self.health.ok(now)
        self.log.event("info", self.name, "sensor_init", f"{self.name} initialised",
                       ready_at=self.ready_at, id=self.health.id, **self.init_details)
        return True

    def reinit(self, now: float, reason: str) -> bool:
        self.reinit_count += 1
        self.log.event("warning", self.name, "sensor_reinit",
                       f"re-initialising {self.name}: {reason}", reason=reason,
                       count=self.reinit_count)
        self.stop()
        return self._init_once(now)

    def stop(self) -> None:
        device, self.device = self.device, None
        if device is not None:
            try:
                self._close(device)
            except Exception:
                pass  # we drop the device either way

    def ready(self, now: float) -> bool:
        return self.device is not None and self.ready_at is not None and now >= self.ready_at

    def note_ok(self, now: float) -> None:
        self.bad_streak = 0
        self.last_data_at = now
        self.health.ok(now)

    def note_bad(self, now: float, error: str) -> bool:
        """Record a bad reading; returns True if it triggered a reinit."""
        self.bad_streak += 1
        self.bad_times = [t for t in self.bad_times if now - t < BAD_WINDOW_S] + [now]
        self.health.failed(error)
        self.log.debug(self.name, "bad", streak=self.bad_streak, in_window=len(self.bad_times),
                       reinit_at=f"{BAD_STREAK_REINIT} in a row or {BAD_WINDOW_COUNT} in {BAD_WINDOW_S} s",
                       reason=error)
        if self.bad_streak >= BAD_STREAK_REINIT:
            self.reinit(now, f"{self.bad_streak} bad readings in a row")
            return True
        if len(self.bad_times) >= BAD_WINDOW_COUNT:
            self.reinit(now, f"{len(self.bad_times)} bad readings in {BAD_WINDOW_S} s")
            return True
        return False

    def check_silence(self, now: float) -> bool:
        """Reinit if there has been no data for SILENCE_REINIT seconds; True if it did."""
        if self.device is None or self.ready_at is None:
            return False
        quiet_since = max(self.last_data_at or 0, self.ready_at)
        if now - quiet_since >= SILENCE_REINIT / 2:
            self.log.debug(self.name, "silent", silent_s=int(now - quiet_since), reinit_at=SILENCE_REINIT)
        if now - quiet_since >= SILENCE_REINIT:
            self.health.failed(f"no reading for {int(now - quiet_since)} s")
            self.reinit(now, f"silent for {int(now - quiet_since)} s")
            return True
        return False

    def status(self) -> Dict[str, Any]:
        return {
            "available": self.health.available,
            "healthy": self.health.healthy,
            "last_error": self.health.last_error,
            "last_ok_at": self.health.last_ok_at,
            "ready_at": self.ready_at if self.device is not None else None,
            "reinit_count": self.reinit_count,
            "id": self.health.id,
        }


# --- SHT41 ------------------------------------------------------------------------

class Sht41(Sensor):
    """Temperature and humidity, high precision, heater off.

    The heater is only for drying out after condensation, which indoors never happens.
    """

    name = "sht41"
    init_details = {"heater": "off", "precision": "high"}

    def __init__(self, i2c, config, log):
        super().__init__(log)
        self.i2c = i2c
        self.offset = float(config.sensors.sht41_temp_offset_c)

    def _open(self):
        import adafruit_sht4x

        device = adafruit_sht4x.SHT4x(self.i2c)
        device.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION
        serial = getattr(device, "serial_number", None)
        if serial is not None:
            self.health.id = f"{int(serial):08x}" if isinstance(serial, int) else str(serial)
        return device

    def read(self, now: float) -> Optional[Dict[str, float]]:
        if self.device is None:
            return None
        return {
            "temp": float(self.device.temperature) + self.offset,
            "humid": float(self.device.relative_humidity),
        }


# --- SPS30 ------------------------------------------------------------------------

FAN_CLEAN_BLANK = 15.0               # the fan runs ~10 s at full speed; ignore readings meanwhile
FAN_CLEAN_COOLDOWN = 600.0           # minimum gap between manual cleans

# driver key -> row column. The driver names number counts in tenths of a um
# ("nc10" is the 1 um count), the row uses whole um ("nc1").
SPS30_ROW_KEYS = {"pm1": "pm1", "pm25": "pm25", "pm4": "pm4", "pm10": "pm10", "tps": "tps",
                  "nc05": "nc05", "nc10": "nc1", "nc25": "nc25", "nc40": "nc4", "nc100": "nc10"}


class Sps30(Sensor):
    """Particulates. The fan runs continuously and the sensor updates every second."""

    name = "sps30"
    init_details = {"fan": "on", "autoclean": "off"}

    def __init__(self, i2c, config, log, device_factory=None):
        super().__init__(log)
        self.i2c = i2c
        self.config = config
        self._factory = device_factory
        self.blank_until: Optional[float] = None
        self.last_clean_at: Optional[float] = None
        self.firmware: Optional[tuple] = None

    def _open(self):
        if self._factory is None:
            from drivers.sps30_i2c import SPS30
            factory = SPS30
        else:
            factory = self._factory
        device = factory(self.i2c)
        device.wakeup()
        device.start_measurement()
        firmware = getattr(device, "firmware_version", None) or device.read_firmware()
        self.firmware = tuple(firmware) if firmware else None
        self.health.id = f"{self.firmware[0]}.{self.firmware[1]}" if self.firmware else None
        # The collector schedules the weekly clean itself. The sensor's own
        # timer restarts at every power-up, so it is turned off.
        if int(device.auto_cleaning_interval) != 0:
            device.auto_cleaning_interval = 0
        self.blank_until = None
        return device

    def _close(self, device) -> None:
        device.stop_measurement()

    def is_blanked(self, now: float) -> bool:
        if self.blank_until is None:
            return False
        if now < self.blank_until:
            return True
        self.blank_until = None
        return False

    def read(self, now: float):
        """The ten row values, or None when there is no new data or a clean is running."""
        if self.device is None or self.is_blanked(now):
            return None
        if not self.device.data_ready:
            return None
        data = self.device.read()
        return {column: float(data[key]) for key, column in SPS30_ROW_KEYS.items() if key in data}

    def force_clean(self, now: float, manual: bool = True) -> Dict[str, Any]:
        """Start a fan clean and ignore readings for FAN_CLEAN_BLANK seconds."""
        if self.device is None:
            raise RuntimeError("SPS30 is not initialised")
        if manual and self.last_clean_at is not None and now - self.last_clean_at < FAN_CLEAN_COOLDOWN:
            remaining = int(FAN_CLEAN_COOLDOWN - (now - self.last_clean_at))
            raise RuntimeError(f"Fan cleaning is rate-limited; wait another {remaining} s")
        self.device.force_clean()
        self.last_clean_at = now
        self.blank_until = now + FAN_CLEAN_BLANK
        self.log.event("info", self.name, "fan_clean", "fan cleaning started",
                       manual=manual, blank_s=int(FAN_CLEAN_BLANK))
        return {"blank_s": int(FAN_CLEAN_BLANK), "manual": manual}


# --- SCD41 ------------------------------------------------------------------------

SCD41_REINIT_SETTLE = 1.0        # datasheet 3.10.5 asks for 30 ms after reinit
SCD41_SELF_TEST_CMD = 0x3639     # perform_self_test (datasheet 3.10.3)
SCD41_SELF_TEST_S = 10.0         # max command duration
SELF_TEST_TEXT = {               # sensor_error message per failure reason
    "word": "the sensor reports a malfunction (or an unstable supply — Sensirion testing guide)",
    "nack": "the sensor did not answer the self-test read",
    "crc": "the self-test answer was garbled (bad CRC)",
}


def _classify_self_test_error(exc: Exception) -> str:
    text = str(exc)
    if "Self test failed" in text:
        return "word"
    if "CRC" in text:
        return "crc"
    if "communicate" in text or "I2C" in text:
        return "nack"
    return text or exc.__class__.__name__
SCD41_SLEEP_S = 1.0              # power_down -> wake_up, the deepest reset available in software
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
    """CO2 in periodic mode (datasheet 3.5): the sensor measures every 5 s on its own.

    Each open does power_down / wake_up / reinit. The ~10 s self-test only runs
    on the first open in a process.
    """

    name = "scd41"
    init_details = {"mode": "periodic"}

    def __init__(self, i2c, config, log, sleep=time.sleep):
        super().__init__(log)
        self.i2c = i2c
        self.config = config
        self.sleep = sleep
        self.asc = bool(config.sensors.asc)
        self.pressure_hpa: Optional[float] = None
        self.recent: list = []  # (ts, ppm) of plausible readings inside CAL_WINDOW
        self.opens = 0
        self.self_test = "unavailable"
        self.self_test_word: Optional[int] = None
        self.self_test_reason: Optional[str] = None

    def _open(self):
        import adafruit_scd4x

        device = adafruit_scd4x.SCD4X(self.i2c)
        try:
            device.stop_periodic_measurement()  # may still be running from a previous process
        except Exception:
            pass
        self._sleep_and_wake(device)
        device.reinit()
        self.sleep(SCD41_REINIT_SETTLE)
        serial = getattr(device, "serial_number", None)
        if serial is not None:
            try:
                self.health.id = "".join(f"{int(word):04x}" for word in serial)
            except (TypeError, ValueError):
                self.health.id = str(serial)
        if self.opens == 0:
            self.self_test, self.self_test_word, self.self_test_reason = self._self_test(device)
            if self.self_test == "fail":
                self.log.event("error", self.name, "sensor_error",
                               "scd41 self-test failed: " + SELF_TEST_TEXT.get(
                                   self.self_test_reason, self.self_test_reason or "unknown"),
                               self_test=self.self_test, self_test_word=self.self_test_word,
                               self_test_reason=self.self_test_reason)
        self.opens += 1
        self.init_details = {
            **type(self).init_details, "self_test": self.self_test,
            "self_test_word": self.self_test_word, "self_test_reason": self.self_test_reason,
            "altitude_m": int(self.config.location.altitude_m),
            "temp_offset_c": float(self.config.sensors.scd41_temp_offset_c),
            "asc": bool(self.config.sensors.asc),
        }
        self._configure_and_start(device)
        return device

    def _sleep_and_wake(self, device) -> None:
        """power_down, wait, wake_up (datasheet 3.9.3/3.9.4), if the driver supports them."""
        down, up = getattr(device, "power_down", None), getattr(device, "wake_up", None)
        if down is None or up is None:
            return
        down()
        self.sleep(SCD41_SLEEP_S)
        try:
            up()
        except OSError:
            pass  # wake_up is not ACKed; some drivers raise on the NACK

    @staticmethod
    def _self_test(device) -> Tuple[str, Optional[int], Optional[str]]:
        """Run the self-test; returns (verdict, status word, reason).

        A non-zero word means a malfunction or an unstable supply (datasheet
        3.10.3). The Adafruit self_test() raises the same RuntimeError for a bad
        word, a NACK and a CRC error, so we read the word ourselves when the
        driver's send/read helpers are available.
        """
        send, read, buffer = (getattr(device, "_send_command", None), getattr(device, "_read_reply", None),
                              getattr(device, "_buffer", None))
        if callable(send) and callable(read) and buffer is not None:
            try:  # the self-test must run in idle mode, which it is here
                send(SCD41_SELF_TEST_CMD, cmd_delay=SCD41_SELF_TEST_S)
                read(buffer, 3)
            except OSError:
                return "fail", None, "nack"
            except RuntimeError as exc:
                return "fail", None, _classify_self_test_error(exc)
            word = (int(buffer[0]) << 8) | int(buffer[1])
            return ("ok" if word == 0 else "fail"), word, (None if word == 0 else "word")
        test = getattr(device, "self_test", None)
        if test is None:
            return "unavailable", None, None
        try:
            test()
        except OSError:
            return "fail", None, "nack"
        except RuntimeError as exc:
            return "fail", None, _classify_self_test_error(exc)
        return "ok", 0, None

    def _configure_and_start(self, device) -> None:
        # These settings are RAM-only (no EEPROM wear) and must be written
        # before periodic measurement starts.
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

    def read(self, now: float) -> Optional[Dict[str, float]]:
        """The newest measurement, or None if the sensor has nothing new."""
        if self.device is None or not self.device.data_ready:
            return None
        return {
            "co2": float(self.device.CO2),
            "co2_temp": float(self.device.temperature),
            "co2_humid": float(self.device.relative_humidity),
        }

    def record_valid(self, now: float, co2: float) -> None:
        self.recent.append((now, float(co2)))
        self.recent = [(ts, ppm) for ts, ppm in self.recent if now - ts <= CAL_WINDOW]

    def set_ambient_pressure(self, hpa: float) -> bool:
        """Send the air pressure to the sensor if it changed by >= 1 hPa; True if sent."""
        if hpa is None:
            return False
        if self.pressure_hpa is not None and abs(hpa - self.pressure_hpa) < PRESSURE_MIN_DELTA_HPA:
            return False
        if self.device is not None:
            self.device.set_ambient_pressure(int(round(hpa)))
        self.pressure_hpa = float(hpa)
        return True

    def runtime_seconds(self, now: float) -> int:
        if self.device is None or self.inited_at is None:
            return 0
        return int(now - self.inited_at)

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
        """Run a forced recalibration after the safety checks; measurement restarts afterwards."""
        checks = self.check_preconditions(now, target_ppm, allow_large_offset)
        device = self.device
        device.stop_periodic_measurement()  # the driver waits the datasheet's 500 ms
        try:
            correction = device.force_calibration(int(target_ppm))
            if correction == CAL_REJECTED:
                raise RuntimeError("SCD41 rejected the forced calibration command (0xFFFF)")
            if persist:
                device.persist_settings()
        finally:
            self._configure_and_start(device)
            self.inited_at = now
            self.ready_at = ready_after(now)
            self.last_data_at = None
        return {"correction_ppm": int(correction), "target_ppm": int(target_ppm),
                "persisted": bool(persist), **checks}
