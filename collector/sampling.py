"""One beat: ask each ready sensor, store what it said, keep the books, one line.

Every 10 s on the wall clock, in a fixed order: the SHT41 measures (8 ms),
the SPS30 hands over its newest 1 s value, the SCD41 its newest 5 s value.
The row carries the mark's timestamp and the values **as the sensors gave
them** (rounded to the row's precision) — nothing is dropped. The "cannot
be air" rule (``shared/filters.py``) only counts a reading as bad for the
sensor's reset ladder. A sensor inside its quiet time is not asked; when no
sensor was asked (a start) nothing is written and nothing is logged. When
every sensor asked raised in the same beat the I2C bus itself is re-created.
"""

import math
import time
from typing import Any, Dict, Optional

from shared import clock
from shared.db import METRICS, round_metric
from shared.filters import implausible

SAMPLE_INTERVAL = 10       # six rows a minute; the manager averages them for the panel

# which metrics belong to which sensor (a bad value counts against its sensor)
SENSOR_METRICS = {
    "scd41": ("co2", "co2_temp", "co2_humid"),
    "sht41": ("temp", "humid"),
    "sps30": ("pm1", "pm25", "pm4", "pm10", "tps", "nc05", "nc1", "nc25", "nc4", "nc10"),
}


def _cell(metric: str, value: Any) -> Any:
    """The stored form: rounded; a non-number cannot be stored, so NULL."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round_metric(metric, number) if math.isfinite(number) else None


class Sampler:
    def __init__(self, db, log, scd41, sht41, sps30, i2c_factory=None, monotonic=time.monotonic):
        self.db = db
        self.log = log
        self.scd41 = scd41
        self.sht41 = sht41
        self.sps30 = sps30
        self.sensors = [sht41, sps30, scd41]  # the order of the beat, see the module docstring
        self.i2c_factory = i2c_factory
        self.monotonic = monotonic
        self.sample_count = 0
        self.storage_failures = 0
        self.bus_reinits = 0
        self.last_record: Optional[Dict[str, Any]] = None

    def next_due(self, now: float) -> float:
        return clock.next_aligned(SAMPLE_INTERVAL, now)

    # --- one beat -----------------------------------------------------------------------

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
            self.log.info("sample", "row", ts=ts, **row,
                          bad=",".join(f"{m}:{r}" for m, r in record["bad"].items()) or None,
                          raised=",".join(record["raised"]) or None)
            self.sample_count += 1
        if record["raised"] and len(record["raised"]) == len(record["asked"]):
            self._reinit_bus(now, record["errors"])
        self.last_record = record
        return record

    # --- helpers ------------------------------------------------------------------------------

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
        """Per sensor that answered: all plausible → ok, else bad. Returns {metric: reason}."""
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
