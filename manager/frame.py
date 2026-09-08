"""The once-a-minute calculation of what the panel and the Live tab show.

Averages of the raw rows of the minute that just ended (only values that
could be air), the AQI from PM2.5 with its words, the CO2 word, the three
weather blocks (or "stale"), the three glyphs, and the two flags the renderer
acts on: ``warming_up`` (the collector's ``ready_at`` is ahead or less than a
minute behind — its first full minute is not averaged yet — or the manager
itself is in its first two minutes with nothing from the collector yet: the
start-up screen) and ``collector_silent`` (no raw row for 90 s, or
the collector's status document older than 90 s).
"""

from typing import Any, Dict, Optional

from manager import weather as weather_mod
from shared.aqi import aqi_category, aqi_from_pm25, co2_category

STATUS_STALE = 90.0      # collector_status older than this → the collector is silent
COLLECTOR_SILENT = 90.0  # no raw row for this long → the collector is silent
STARTUP_GRACE = 120.0    # the manager's first seconds: no status / no row yet is "starting", not "silent"


class FrameBuilder:
    def __init__(self, db, log, config, started_at: Optional[float] = None):
        self.db = db
        self.log = log
        self.config = config
        self.started_at = started_at
        self._weather_stale_logged = False
        self.last: Optional[Dict[str, Any]] = None
        self.last_state: Dict[str, Any] = {}

    def collector_state(self, now: float) -> Dict[str, Any]:
        """silent / warming_up / unhealthy, from the raw rows and the status document."""
        latest_raw = self.db.latest_raw_at()
        status = self.db.get_state("collector_status")
        fresh = status is not None and now - status["updated_at"] <= STATUS_STALE
        # The collector publishes its status once more on shutdown, so after a
        # joint restart or a reboot the manager's first frames would see a
        # "fresh" status from the dead process (with an old ready_at) and paint
        # its last minute before saying "Starting up". During the grace a status
        # written at or before the manager's own start is not fresh.
        in_grace = self.started_at is not None and now - self.started_at < STARTUP_GRACE
        if fresh and in_grace and status["updated_at"] <= self.started_at:
            fresh = False
        value = (status or {}).get("value", {}) if fresh else {}
        ready_at = value.get("ready_at")
        has_ready = isinstance(ready_at, (int, float))
        warmup_left = int(ready_at - now) if has_ready and ready_at > now else 0
        # the first whole minute after ready_at is not averaged before the next :00
        first_minute = has_ready and now < ready_at + 60
        starting = in_grace and (not fresh or latest_raw is None)
        warming = first_minute or starting
        rows_silent = latest_raw is None or now - latest_raw > COLLECTOR_SILENT
        sensors = value.get("sensors", {})
        unhealthy = [name for name, s in sensors.items() if (s or {}).get("healthy") is False]
        silent = (rows_silent or not fresh) and not warming
        # why the panel says "Starting up" or shows the sensor glyph — for the frame line
        if warming:
            because = ("quiet_time" if warmup_left else "first_minute" if first_minute
                       else "no_status" if status is None else "status_before_start" if not fresh
                       else "no_rows")
        elif silent:
            because = "status_stale" if not fresh else "no_rows" if latest_raw is None else "rows_old"
        else:
            because = None
        return {
            "silent": silent,
            "because": because,
            "rows_silent": rows_silent,
            "status_fresh": fresh,
            "warming_up": warming,
            "warmup_left": warmup_left,
            "unhealthy": unhealthy,
            "last_row_at": latest_raw,
        }

    def build(self, now: float, weather_doc: Optional[Dict[str, Any]],
              wifi_glyph: bool, power_glyph: bool) -> Dict[str, Any]:
        averages = self.db.minute_average(int(now))
        values = averages["values"]
        aqi = aqi_from_pm25(values.get("pm25"))
        full, short = aqi_category(aqi)
        weather = weather_mod.summarize(weather_doc, now, self.config.weather.block_hours)
        self._weather_stale_event(weather["stale"], weather_doc)
        state = self.collector_state(now)
        self.last_state = state
        rows = averages.get("rows", 0)
        dropped = {m: rows - n for m, n in averages["samples"].items() if rows - n > 0}
        self.log.debug("display", "minute", window=f"[{int(now) - 60},{int(now)})", rows=rows,
                       dropped=",".join(f"{m}:{n}" for m, n in dropped.items()) or None,
                       because=state["because"])
        doc = {
            "updated_at": int(now),
            "warming_up": state["warming_up"],
            "warmup_left": state["warmup_left"],  # seconds until every sensor is past its quiet time
            "collector_silent": state["silent"],
            "values": values,
            "samples": averages["samples"],
            "aqi": aqi,
            "aqi_category": full,
            "aqi_short": short,
            "co2_category": co2_category(values.get("co2")),
            "weather": weather,
            "glyphs": {
                "wifi": bool(wifi_glyph),
                "power": bool(power_glyph),
                "sensor": bool(state["silent"] or state["unhealthy"]),
            },
            "unhealthy": state["unhealthy"],
        }
        self.last = doc
        return doc

    def _weather_stale_event(self, stale: bool, weather_doc) -> None:
        if stale and not self._weather_stale_logged:
            if weather_doc is not None:  # never fetched yet is not "stale", just absent
                self.log.event("warning", "weather", "weather_stale",
                               "forecast older than 6 hours; painting dashes",
                               fetched_at=(weather_doc or {}).get("fetched_at"))
                self._weather_stale_logged = True
        elif not stale:
            self._weather_stale_logged = False
