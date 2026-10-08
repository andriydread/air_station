"""The 10 s sampling beat: read each ready sensor and write one raw row.

Values are stored as the sensors reported them (rounded only). Implausible
values are kept but count as bad readings for the sensor's reset logic.
"""

import math
import time
from typing import Any, Dict, Optional

from shared import clock
from shared.db import METRICS, round_metric
from shared.filters import implausible

SAMPLE_INTERVAL = 10       # the manager averages the six rows of each minute

# a bad value counts against the sensor that produced it
SENSOR_METRICS = {
    "scd41": ("co2", "co2_temp", "co2_humid"),
    "sht41": ("temp", "humid"),
    "sps30": ("pm1", "pm25", "pm4", "pm10", "tps", "nc05", "nc1", "nc25", "nc4", "nc10"),
}


def _cell(metric: str, value: Any) -> Any:
    """Rounded value for the row, or None for anything non-numeric."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round_metric(metric, number) if math.isfinite(number) else None


def _per_sensor(values: Dict[str, Any]) -> Optional[str]:
    """Format as "sht41:8.1,sps30:2.3,scd41:4.0", or None if empty."""
    return ",".join(f"{name}:{value}" for name, value in values.items()) or None


class Sampler:
    def __init__(self, db, log, scd41, sht41, sps30, i2c_factory=None, monotonic=time.monotonic):
        self.db = db
        self.log = log
        self.scd41 = scd41
        self.sht41 = sht41
        self.sps30 = sps30
        self.sensors = [sht41, sps30, scd41]  # SHT41 first: it measures on demand (~8 ms)
        self.i2c_factory = i2c_factory
        self.monotonic = monotonic
        self.sample_count = 0
        self.storage_failures = 0
        self.bus_reinits = 0
        self.last_record: Optional[Dict[str, Any]] = None

    def next_due(self, now: float) -> float:
        return clock.next_aligned(SAMPLE_INTERVAL, now)

    def beat(self, now: float) -> Dict[str, Any]:
        ts = clock.aligned_stamp(SAMPLE_INTERVAL, now)
        raw: Dict[str, float] = {}
        record: Dict[str, Any] = {
            "ts": ts, "present": [], "asked": [], "answered": [], "raised": [],
            "errors": {}, "errno": {}, "read_ms": {}, "raw": raw, "row": None, "bad": {},
        }
        for sensor in self.sensors:
            name = sensor.name
            if not sensor.ensure(now):
                continue
            record["present"].append(name)
            if not sensor.ready(now):
                continue
            record["asked"].append(name)
            started = self.monotonic()
            try:
                result = sensor.read(now)
            except Exception as exc:
                record["read_ms"][name] = round((self.monotonic() - started) * 1000, 1)
                record["raised"].append(name)
                record["errors"][name] = f"{exc.__class__.__name__}: {exc}"
                record["errno"][name] = getattr(exc, "errno", None)
                self._sensor_error(sensor, exc, now)
                continue
            record["read_ms"][name] = round((self.monotonic() - started) * 1000, 1)
            if result is None:
                if not (name == "sps30" and self.sps30.is_blanked(now)):
                    sensor.check_silence(now)
                continue
            record["answered"].append(name)
            raw.update(result)

        if record["asked"]:
            record["bad"] = self._account(now, raw)
            row = {metric: _cell(metric, raw.get(metric)) for metric in METRICS}
            record["row"] = row
            self._write(ts, row)
            # Per-sensor read times help spot bus or supply trouble before reads
            # start failing. bad/raised are only logged when set.
            extra: Dict[str, Any] = {"ms": _per_sensor(record["read_ms"])}
            if record["bad"]:
                extra["bad"] = ",".join(f"{m}:{r}" for m, r in record["bad"].items())
            if record["raised"]:
                extra["raised"] = ",".join(record["raised"])
            self.log.info("sample", "row", ts=ts, **row, **extra)
            self.sample_count += 1
            no_data = [n for n in record["asked"] if n not in record["answered"] and n not in record["raised"]]
            self.log.debug("sample", "beat", asked=",".join(record["asked"]),
                           answered=",".join(record["answered"]) or None,
                           no_data=",".join(no_data) or None, raised=",".join(record["raised"]) or None,
                           ms=_per_sensor(record["read_ms"]))
        if record["raised"] and len(record["raised"]) == len(record["asked"]):
            self._reinit_bus(now, record["errors"])
        self.last_record = record
        return record

    def _sensor_error(self, sensor, exc: Exception, now: float) -> None:
        text = f"{exc.__class__.__name__}: {exc}"
        first_of_streak = sensor.bad_streak == 0
        sensor.note_bad(now, text)
        if first_of_streak:
            self.log.event("error", sensor.name, "sensor_error", f"{sensor.name} read failed: {exc}",
                           error=text, errno=getattr(exc, "errno", None))
        else:
            self.log.warning(sensor.name, "read_failed", error=text, streak=sensor.bad_streak,
                             errno=getattr(exc, "errno", None))

    def _account(self, now: float, raw: Dict[str, float]) -> Dict[str, str]:
        """Mark each answering sensor ok or bad; returns {metric: reason} for implausible values."""
        bad: Dict[str, str] = {}
        for sensor in self.sensors:
            metrics = SENSOR_METRICS[sensor.name]
            if not any(m in raw for m in metrics):
                continue
            reasons = {m: implausible(m, raw[m]) for m in metrics if m in raw}
            reasons = {m: r for m, r in reasons.items() if r}
            if reasons:
                sensor.note_bad(now, "cannot be air: " + ", ".join(f"{m} {raw[m]}" for m in reasons))
                bad.update(reasons)
            else:
                sensor.note_ok(now)
                if sensor is self.scd41 and "co2" in raw:
                    self.scd41.record_valid(now, raw["co2"])
        return bad

    def _write(self, ts: int, row: Dict[str, Any]) -> None:
        try:
            self.db.insert_raw(ts, row)
        except Exception:
            self.storage_failures += 1
            self.log.exception("storage", "insert_failed", ts=ts)

    def _reinit_bus(self, now: float, errors: Dict[str, str]) -> None:
        self.bus_reinits += 1
        self.log.event("error", "i2c", "sensor_reinit",
                       "every sensor failed in the same beat: re-creating the I2C bus",
                       count=self.bus_reinits, errors=errors)
        if self.i2c_factory is None:
            return
        for sensor in self.sensors:
            sensor.stop()
        try:
            bus = self.i2c_factory()
        except Exception:
            self.log.exception("i2c", "bus_open_failed")
            return
        for sensor in self.sensors:
            sensor.i2c = bus
            sensor.backoff.reset()
