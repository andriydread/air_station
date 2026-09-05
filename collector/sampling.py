"""One beat: ask each sensor, drop garbage, remember what was dropped, write one row.

Every 30 s on the wall clock, ``BEAT_OFFSET`` seconds after the :00/:30 mark
(the panel refresh at :00 is over by then), in a fixed order so no two
sensors draw at once: the SHT41 measures (8 ms), the SPS30 hands over its
latest numbers (its fan runs all the time), then the SCD41 is told to
measure once (single shot, ~5 s, its 175 mA pulse happens now and only
now) and read. The row carries the mark's timestamp. Metrics of a sensor
still in warm-up stay empty (one ``warming_up`` event per warm-up); the
sensor's ``warmup_beat`` still runs (the SCD41 conditions itself with two
discarded shots). A dropped value
is NULL in its cell and a ``value_dropped`` event on the first drop of a
streak, then every 6th. Six bad beats in a row re-init that sensor (the
base class does it); when every present sensor raised in the same beat the
I2C bus itself is re-created.

``SAMPLING = "beat"`` is the seam for the alternative "one row per
data-ready" mode: only ``next_due`` would change.
"""

import time
from typing import Any, Dict, List, Optional

from collector.filters import clean_row
from shared import clock

SAMPLE_INTERVAL = 30       # two rows a minute; the manager averages the pair
BEAT_OFFSET = 5            # seconds after the mark: clear of the panel refresh at :00
SAMPLING = "beat"          # or "on_ready" (not implemented; see next_due)
DROP_EVENT_EVERY = 6       # value_dropped events: 1st of a streak, then every 6th

# which metrics belong to which sensor (a dropped value counts against its sensor)
SENSOR_METRICS = {
    "scd41": ("co2", "co2_temp", "co2_humid"),
    "sht41": ("temp", "humid"),
    "sps30": ("pm1", "pm25", "pm10", "tps", "nc05", "nc1", "nc25"),
}


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
        self.drop_streaks: Dict[str, int] = {}
        self.warmups_logged: set = set()
        self.sample_count = 0
        self.storage_failures = 0
        self.bus_reinits = 0
        self.last_record: Optional[Dict[str, Any]] = None

    def next_due(self, now: float) -> float:
        """When the next row is due. ``beat``: the next 30 s wall-clock mark."""
        return clock.next_aligned(SAMPLE_INTERVAL, now)

    # --- one beat -----------------------------------------------------------------------

    def beat(self, now: float) -> Dict[str, Any]:
        ts = clock.aligned_stamp(SAMPLE_INTERVAL, now)
        raw: Dict[str, float] = {}
        extra: Dict[str, float] = {}
        record: Dict[str, Any] = {
            "ts": ts, "read_ms": {}, "data_ready": {}, "warmup_left": {},
            "errors": {}, "errno": {}, "present": [], "raised": [],
        }
        for sensor in self.sensors:
            name = sensor.name
            if not sensor.ensure(now):
                continue
            record["present"].append(name)
            left = sensor.warmup_left(now)
            record["warmup_left"][name] = int(round(left))
            if left > 0:
                self._log_warmup_once(sensor, left)
                try:
                    sensor.warmup_beat(now)
                except Exception as exc:
                    record["errors"][name] = f"{exc.__class__.__name__}: {exc}"
                    self._sensor_error(sensor, exc, now)
                continue
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
                record["data_ready"][name] = False
                blanked = name == "sps30" and self.sps30.is_blanked(now)
                if not blanked:
                    sensor.check_silence(now)
                continue
            record["data_ready"][name] = True
            if isinstance(result, tuple):
                values, more = result
                extra.update(more)
            else:
                values = result
            raw.update(values)

        row, dropped = clean_row(raw)
        record.update(raw=raw, extra=extra, row=row, dropped=dropped)
        self._account_values(now, raw, dropped)
        self._drop_events(dropped)
        self._write(ts, row)
        if record["raised"] and len(record["raised"]) == len(record["present"]) and record["present"]:
            self._reinit_bus(now, record["errors"])
        if self.log.level == "debug" and "sps30" in record["data_ready"]:
            record["sps30_status"] = self.sps30.status_word()
        self.sample_count += 1
        self.last_record = record
        return record

    # --- helpers ------------------------------------------------------------------------------

    def _log_warmup_once(self, sensor, left: float) -> None:
        key = (sensor.name, sensor.warmup_started_at)
        if key in self.warmups_logged:
            return
        self.warmups_logged.add(key)
        self.log.event("info", sensor.name, "warming_up",
                       f"{sensor.name} warming up, {int(round(left))} s to go",
                       seconds=int(round(left)))

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

    def _account_values(self, now: float, raw: Dict[str, float], dropped: Dict[str, Any]) -> None:
        for sensor in self.sensors:
            metrics = SENSOR_METRICS[sensor.name]
            read_any = any(m in raw for m in metrics)
            if not read_any:
                continue
            bad = [m for m in metrics if m in dropped]
            if bad:
                sensor.note_bad(now, f"dropped {', '.join(bad)}")
            else:
                sensor.note_ok(now)
                if sensor is self.scd41 and "co2" in raw:
                    self.scd41.record_valid(now, raw["co2"])

    def _drop_events(self, dropped: Dict[str, Any]) -> None:
        for metric in list(self.drop_streaks):
            if metric not in dropped:
                self.drop_streaks.pop(metric)
        for metric, (value, reason) in dropped.items():
            streak = self.drop_streaks.get(metric, 0) + 1
            self.drop_streaks[metric] = streak
            source = next(name for name, metrics in SENSOR_METRICS.items() if metric in metrics)
            if (streak - 1) % DROP_EVENT_EVERY == 0:
                self.log.event("warning", source, "value_dropped",
                               f"{metric} dropped ({reason}): {value}",
                               metric=metric, value=value, reason=reason, streak=streak)
            else:
                self.log.info(source, "value_dropped", metric=metric, value=value, reason=reason,
                              streak=streak)

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
