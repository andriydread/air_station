"""The three sensor wrappers and the bookkeeping they share.

Every sensor: is it there, is it healthy, when to try again, when to give up
and restart it. Two things trigger a re-init — six bad readings in a row, or
two minutes without any reading after warm-up. Warm-up after a (re)start
(60 s CO2, 30 s dust) is not counted against the sensor at all.
"""

import time
from typing import Any, Dict, Optional

from shared.backoff import ReinitBackoff

BAD_STREAK_REINIT = 6        # bad readings in a row (three minutes at 30 s) → re-init
BAD_WINDOW_S = 300           # … or this many bad readings inside this window, good ones
BAD_WINDOW_COUNT = 6         # in between or not (a sensor alternating zeros and values)
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
    init_details: Dict[str, Any] = {}  # extra fields on the sensor_init event

    def __init__(self, log):
        self.log = log
        self.device: Any = None
        self.health = SensorHealth(self.name)
        self.backoff = ReinitBackoff()
        self.bad_streak = 0
        self.bad_times: list = []  # monotonic-free: wall-clock stamps of bad readings in the window
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
        self.bad_times = []
        self.warmup_started_at = now
        self.last_data_at = now
        self.health.ok(now)
        self.log.event("info", self.name, "sensor_init", f"{self.name} initialised",
                       warmup_s=self.warmup_seconds, id=self.health.id, **self.init_details)
        self._readback(now)
        return True

    def _readback(self, now: float) -> None:
        """Ask the sensor what it holds and write it down (never raises, never blocks a start)."""
        try:
            held = self.config_readback()
        except Exception as exc:
            self.log.warning(self.name, "readback_failed", error=str(exc))
            return
        if held:
            self.log.event("info", self.name, "sensor_config", f"{self.name} settings as the sensor reports them", **held)

    def config_readback(self) -> Dict[str, Any]:
        """What the sensor says it holds — read back, not assumed. Default: nothing."""
        return {}

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

    def warmup_beat(self, now: float) -> None:
        """A beat that falls inside the warm-up (nothing is stored); default: nothing."""

    def warmup_left(self, now: float) -> float:
        if self.device is None or self.warmup_started_at is None:
            return 0.0
        return max(0.0, self.warmup_started_at + self.warmup_seconds - now)

    def note_ok(self, now: float) -> None:
        self.bad_streak = 0
        self.last_data_at = now
        self.health.ok(now)

    def note_bad(self, now: float, error: str) -> bool:
        """A garbage value or a failed read; True when this one triggered a re-init.

        Two rules, either fires: ``BAD_STREAK_REINIT`` bad in a row, or
        ``BAD_WINDOW_COUNT`` bad inside the last ``BAD_WINDOW_S`` seconds with
        good readings in between (the 2026-09-04 fault: zeros every second or
        third beat for 16 minutes, never six in a row).
        """
        self.bad_streak += 1
        self.bad_times = [t for t in self.bad_times if now - t < BAD_WINDOW_S] + [now]
        self.health.failed(error)
        if self.bad_streak >= BAD_STREAK_REINIT:
            self.reinit(now, f"{self.bad_streak} bad readings in a row")
            return True
        if len(self.bad_times) >= BAD_WINDOW_COUNT:
            self.reinit(now, f"{len(self.bad_times)} bad readings in {BAD_WINDOW_S} s")
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


# --- SHT41 ------------------------------------------------------------------------

def _mode_name(mode: Any) -> Any:
    """adafruit_sht4x.Mode values are ints with a string table; keep it readable."""
    try:
        import adafruit_sht4x
        return adafruit_sht4x.Mode.string.get(mode, mode)
    except Exception:
        return mode


class Sht41(Sensor):
    """Temperature and humidity: high precision, heater off, no warm-up.

    The SHT4x heater exists for drying the sensor after condensation; it only
    runs when a heater command is sent and switches itself off within 1 s.
    Indoors it is never needed, so every measurement is a no-heater command.
    """

    name = "sht41"
    warmup_seconds = SHT41_WARMUP
    init_details = {"heater": "off", "precision": "high"}

    def __init__(self, i2c, config, log):
        super().__init__(log)
        self.i2c = i2c
        self.offset = float(config.sensors.sht41_temp_offset_c)

    def _open(self):
        import adafruit_sht4x  # only the collector has the library

        device = adafruit_sht4x.SHT4x(self.i2c)
        device.mode = adafruit_sht4x.Mode.NOHEAT_HIGHPRECISION
        serial = getattr(device, "serial_number", None)
        if serial is not None:
            self.health.id = f"{int(serial):08x}" if isinstance(serial, int) else str(serial)
        return device

    def config_readback(self) -> Dict[str, Any]:
        mode = getattr(self.device, "mode", None)
        return {"serial": self.health.id, "mode": _mode_name(mode), "heater": "off",
                "temp_offset_c": self.offset}

    def read(self, now: float) -> Optional[Dict[str, float]]:
        """{"temp", "humid"} with the configured offset applied; errors propagate."""
        if self.device is None:
            return None
        return {
            "temp": float(self.device.temperature) + self.offset,
            "humid": float(self.device.relative_humidity),
        }


# --- SPS30 ------------------------------------------------------------------------

SPS30_STATUS_MIN_FIRMWARE = (2, 2)   # the Device Status Register exists from FW 2.2
FAN_CLEAN_BLANK = 15.0               # the fan runs ~10 s at full speed; readings are not air
FAN_CLEAN_COOLDOWN = 600.0           # a manual clean at most every 10 minutes

# driver key -> raw row column (the driver names the counts by tenths of a µm:
# "nc10" is the 1 µm count, the row calls it "nc1"; "nc40" → "nc4", "nc100" → "nc10")
SPS30_ROW_KEYS = {"pm1": "pm1", "pm25": "pm25", "pm4": "pm4", "pm10": "pm10", "tps": "tps",
                  "nc05": "nc05", "nc10": "nc1", "nc25": "nc25", "nc40": "nc4", "nc100": "nc10"}


class Sps30(Sensor):
    name = "sps30"
    warmup_seconds = SPS30_WARMUP

    def __init__(self, i2c, config, log, device_factory=None):
        super().__init__(log)
        self.i2c = i2c
        self.config = config
        self._factory = device_factory
        self.blank_until: Optional[float] = None
        self.last_clean_at: Optional[float] = None
        self.firmware: Optional[tuple] = None
        self._status_unsupported_logged = False

    def _open(self):
        if self._factory is None:
            from drivers.sps30_i2c import SPS30  # the hand-written driver
            factory = SPS30
        else:
            factory = self._factory
        device = factory(self.i2c)
        device.wakeup()
        device.start_measurement()
        firmware = getattr(device, "firmware_version", None) or device.read_firmware()
        self.firmware = tuple(firmware) if firmware else None
        self.health.id = f"{self.firmware[0]}.{self.firmware[1]}" if self.firmware else None
        # The collector schedules the weekly clean itself (Sunday 04:00); the
        # sensor's own timer restarts at every power-up and is switched off.
        try:
            if int(device.auto_cleaning_interval) != 0:
                device.auto_cleaning_interval = 0
                self.log.info(self.name, "autoclean_disabled")
        except Exception as exc:
            self.log.warning(self.name, "autoclean_read_failed", error=str(exc))
        self.blank_until = None
        return device

    def _close(self, device) -> None:
        device.stop_measurement()

    def config_readback(self) -> Dict[str, Any]:
        device = self.device
        held: Dict[str, Any] = {"firmware": self.health.id}
        try:
            held["autoclean_interval_s"] = int(device.auto_cleaning_interval)
        except Exception as exc:
            held["autoclean_interval_s"] = f"error: {exc}"
        status = self.status_word()
        if status:
            held.update({f"status_{key}": value for key, value in status.items()})
        return held

    def is_blanked(self, now: float) -> bool:
        if self.blank_until is None:
            return False
        if now < self.blank_until:
            return True
        self.blank_until = None
        return False

    def read(self, now: float):
        """The ten row values, or None when no data / blanked; errors propagate."""
        if self.device is None or self.is_blanked(now):
            return None
        if not self.device.data_ready:
            return None
        data = self.device.read()
        return {column: float(data[key]) for key, column in SPS30_ROW_KEYS.items() if key in data}

    def force_clean(self, now: float, manual: bool = True) -> Dict[str, Any]:
        """Start a fan clean; blank the readings for 15 s; one ``fan_clean`` event."""
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

    def status_word(self) -> Optional[Dict[str, Any]]:
        """The sensor's own fan/laser verdict (FW ≥ 2.2); None when unavailable."""
        if self.device is None:
            return None
        if self.firmware is not None and self.firmware < SPS30_STATUS_MIN_FIRMWARE:
            if not self._status_unsupported_logged:
                self._status_unsupported_logged = True
                self.log.info(self.name, "status_register_unsupported",
                              firmware=f"{self.firmware[0]}.{self.firmware[1]}")
            return None
        try:
            return dict(self.device.read_device_status())
        except Exception as exc:
            self.log.warning(self.name, "status_read_failed", error=str(exc))
            return None


# --- SCD41 ------------------------------------------------------------------------

SCD41_DATA_READY_WAIT = 2.0      # after the 5 s single shot the value is there; this is the slack
SCD41_DATA_READY_POLL = 0.5
SCD41_REINIT_SETTLE = 1.0        # datasheet: up to 1000 ms after reinit before commands
SCD41_SLEEP_S = 1.0              # power_down → wake_up: the deepest reset software can give it
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
    """CO2 in single shot mode (datasheet 3.10): the sensor measures only when
    the beat tells it to, so its 175 mA pulse lands at a known moment, ~5 s
    into the beat, never together with the panel refresh or the SHT41. The
    idle current between shots is 0.15 mA; at two shots a minute the average
    is ~2.6 mA (15 mA in the default periodic mode). Two conditioning shots
    during the 60 s warm-up are discarded, as the datasheet asks.
    """

    name = "scd41"
    warmup_seconds = SCD41_WARMUP
    init_details = {"mode": "single_shot"}

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
        # Every open is the full reset ladder: sleep and wake (SCD41 only, the
        # nearest thing to a power-cycle without cutting VDD), soft reset, then
        # the sensor's own self-test — its verdict goes into the sensor_init
        # event, so a fault that a re-init does not clear says "hardware" in
        # the log instead of leaving us guessing (bench-2026-09-04 §1).
        self._sleep_and_wake(device)
        device.reinit()
        self.sleep(SCD41_REINIT_SETTLE)
        serial = getattr(device, "serial_number", None)
        if serial is not None:
            try:
                self.health.id = "".join(f"{int(word):04x}" for word in serial)
            except (TypeError, ValueError):
                self.health.id = str(serial)
        verdict = self._self_test(device)
        self.init_details = {**type(self).init_details, "self_test": verdict}
        if verdict == "fail":
            self.log.event("error", self.name, "sensor_error", "scd41 self-test failed: the sensor reports a malfunction",
                           self_test=verdict)
        self._configure_and_start(device)
        return device

    def config_readback(self) -> Dict[str, Any]:
        device = self.device
        held: Dict[str, Any] = {"serial": self.health.id, "mode": "single_shot",
                                "self_test": self.init_details.get("self_test")}
        for key, attr in (("variant", "sensor_variant_name"), ("altitude_m", "altitude"),
                          ("temp_offset_c", "temperature_offset"), ("asc", "self_calibration_enabled"),
                          ("pressure_hpa", "ambient_pressure")):
            try:
                value = getattr(device, attr)
            except AttributeError:
                continue  # an older driver without the getter
            held[key] = round(value, 2) if isinstance(value, float) else value
        return held

    def _sleep_and_wake(self, device) -> None:
        """power_down, a second, wake_up (datasheet 3.9.3/3.9.4); skipped on a driver without them."""
        down, up = getattr(device, "power_down", None), getattr(device, "wake_up", None)
        if down is None or up is None:
            return
        down()
        self.sleep(SCD41_SLEEP_S)
        try:
            up()
        except OSError:
            pass  # the sensor does not ACK wake_up; older drivers surface that NACK

    @staticmethod
    def _self_test(device) -> str:
        """"ok" | "fail" | "unavailable" — the driver raises RuntimeError on a non-zero word (~10 s)."""
        test = getattr(device, "self_test", None)
        if test is None:
            return "unavailable"
        try:
            test()
        except RuntimeError:
            return "fail"
        return "ok"

    def _configure_and_start(self, device) -> None:
        # Settings live in RAM only (no EEPROM wear) and must be written in
        # idle mode — which single shot never leaves.
        device.altitude = int(self.config.location.altitude_m)
        device.temperature_offset = float(self.config.sensors.scd41_temp_offset_c)
        device.self_calibration_enabled = bool(self.config.sensors.asc)
        self.asc = bool(device.self_calibration_enabled)
        if self.pressure_hpa is not None:
            device.set_ambient_pressure(int(round(self.pressure_hpa)))
        self.recent.clear()

    def _close(self, device) -> None:
        device.stop_periodic_measurement()  # harmless in idle; stops a periodic mode an old build left

    def warmup_beat(self, now: float) -> None:
        """A conditioning shot, result discarded (datasheet: skip the first two)."""
        if self.device is not None:
            self.device.measure_single_shot()

    def read(self, now: float, deadline_s: float = SCD41_DATA_READY_WAIT) -> Optional[Dict[str, float]]:
        """One single shot (~5 s, the driver waits), then the value; None when none came.

        Returns the raw numbers as the sensor gave them — CO2 plus the SCD41's
        own temperature and humidity. I2C errors propagate to the sampler.
        """
        if self.device is None:
            return None
        self.device.measure_single_shot()
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
