"""The once-a-minute calculation of what the panel and the Live tab show.

Averages of the last two raw rows (the collector's :00 and :30 beats), the
AQI from PM2.5 with its words,
the CO2 word, the three weather blocks (or "stale"), the three glyphs, and
the two flags the renderer acts on: ``warming_up`` (the collector reports a
sensor still in warm-up) and ``collector_silent`` (no raw row for 90 s, i.e.
two beats missed, or the collector's status document older than 90 s).
"""

from typing import Any, Dict, Optional

from collector.sampling import SAMPLE_INTERVAL
from manager import weather as weather_mod
from shared.aqi import aqi_category, aqi_from_pm25, co2_category

STATUS_STALE = 90.0      # collector_status older than this → the collector is silent
COLLECTOR_SILENT = 90.0  # no raw row for this long (two beats missed) → the collector is silent
# The frame runs on the minute and the collector's :00 row lands a few seconds
# later, so the minute averaged is the one that ends one beat ago: its two
# rows (:00 of the previous minute and :30) are certainly written by then.
AVERAGE_LAG = SAMPLE_INTERVAL


class FrameBuilder:
    def __init__(self, db, log, config):
        self.db = db
        self.log = log
        self.config = config
        self._weather_stale_logged = False
        self.last: Optional[Dict[str, Any]] = None

    def collector_state(self, now: float) -> Dict[str, Any]:
        """silent / warming_up / unhealthy, from the raw rows and the status document."""
        latest_raw = self.db.latest_raw_at()
        status = self.db.get_state("collector_status")
        fresh = status is not None and now - status["updated_at"] <= STATUS_STALE
        rows_silent = latest_raw is None or now - latest_raw > COLLECTOR_SILENT
        sensors = (status or {}).get("value", {}).get("sensors", {}) if fresh else {}
        warmup_left = max([int((s or {}).get("warmup_left", 0) or 0) for s in sensors.values()] or [0])
        warming = warmup_left > 0
        unhealthy = [name for name, s in sensors.items() if (s or {}).get("healthy") is False]
        return {
            "silent": rows_silent or not fresh,
            "rows_silent": rows_silent,
            "status_fresh": fresh,
            "warming_up": bool(warming) and not rows_silent,
            "warmup_left": warmup_left if (warming and not rows_silent) else 0,
            "unhealthy": unhealthy,
            "last_row_at": latest_raw,
        }

    def build(self, now: float, weather_doc: Optional[Dict[str, Any]],
              wifi_glyph: bool, power_glyph: bool) -> Dict[str, Any]:
        averages = self.db.minute_average(int(now) - AVERAGE_LAG)
        values = averages["values"]
        aqi = aqi_from_pm25(values.get("pm25"))
        full, short = aqi_category(aqi)
        weather = weather_mod.summarize(weather_doc, now, self.config.weather.block_hours)
        self._weather_stale_event(weather["stale"], weather_doc)
        state = self.collector_state(now)
        doc = {
            "updated_at": int(now),
            "warming_up": state["warming_up"],
            "warmup_left": state["warmup_left"],  # seconds until every sensor is past its warm-up
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
