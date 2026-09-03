"""Open-Meteo forecast → the three rolling 3-hour columns, and the air pressure for the CO2 sensor.

The manager fetches every 30 min and stores the 48-hour hourly arrays under
``last_weather``. The three blocks are derived from those arrays every
minute, so the columns shift when a block ends without a new fetch. A
forecast older than 6 hours is stale and painted as "—".
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from shared import clock

WEATHER_EVERY = 1800
WEATHER_RETRY = 120
WEATHER_STALE = 6 * 3600
WEATHER_TIMEOUT = 10
FORECAST_DAYS = 2
HOURLY_FIELDS = ("temperature_2m", "precipitation_probability", "weathercode", "surface_pressure", "is_day")
USER_AGENT = "AirStation/2.0 (RaspberryPi)"

# WMO weather codes are NOT ordered by severity (85 "slight snow showers"
# outranks 82 "violent rain showers" numerically). Rank them so a block's
# icon shows its worst weather, with the raw code as tiebreaker.
_WMO_SEVERITY_RANKS = (
    ({0, 1}, 0),                    # clear
    ({2}, 1),                       # partly cloudy
    ({3}, 2),                       # overcast
    ({45, 48}, 3),                  # fog
    ({51, 53, 55, 56, 57}, 4),      # drizzle
    ({61, 63, 80}, 5),              # light/moderate rain
    ({65, 66, 67, 81, 82}, 6),      # heavy/freezing rain
    ({71, 73, 75, 77, 85, 86}, 7),  # snow
    ({95, 96, 99}, 8),              # thunderstorms
)


class WeatherError(RuntimeError):
    """The forecast could not be fetched or did not have the expected shape."""


def wmo_severity(code: int):
    for codes, rank in _WMO_SEVERITY_RANKS:
        if code in codes:
            return (rank, code)
    return (2, code)  # unknown codes: treat like plain clouds


def build_url(config) -> str:
    query = urllib.parse.urlencode({
        "latitude": config.location.latitude,
        "longitude": config.location.longitude,
        "hourly": ",".join(HOURLY_FIELDS),
        "timezone": "auto",
        "forecast_days": FORECAST_DAYS,
    })
    return f"https://api.open-meteo.com/v1/forecast?{query}"


def fetch(config, opener=urllib.request.urlopen, now: Optional[float] = None,
          timeout: float = WEATHER_TIMEOUT) -> Dict[str, Any]:
    """Fetch and parse; returns the ``last_weather`` document or raises WeatherError."""
    request = urllib.request.Request(build_url(config), headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read()
    except Exception as exc:
        raise WeatherError(f"{exc.__class__.__name__}: {exc}") from exc
    if status != 200:
        raise WeatherError(f"HTTP {status}")
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise WeatherError(f"bad JSON: {exc}") from exc
    return parse(data, now=now, bytes_=len(body))


def parse(data: Dict[str, Any], now: Optional[float] = None, bytes_: int = 0) -> Dict[str, Any]:
    hourly = data.get("hourly") if isinstance(data, dict) else None
    if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
        raise WeatherError("missing hourly payload")
    times = hourly["time"]
    arrays = {"time": list(times)}
    for field in HOURLY_FIELDS:
        values = hourly.get(field)
        if not isinstance(values, list) or len(values) != len(times):
            raise WeatherError(f"missing or ragged hourly field {field}")
        arrays[field] = list(values)
    fetched_at = int(now if now is not None else clock.now())
    return {
        "fetched_at": fetched_at,
        "timezone": data.get("timezone"),
        "utc_offset_seconds": data.get("utc_offset_seconds"),
        "pressure_hpa": _current_pressure(arrays, fetched_at),
        "bytes": bytes_,
        "hourly": arrays,
    }


def _hour_key(ts: float) -> str:
    return clock.local_now(ts).strftime("%Y-%m-%dT%H:00")


def _current_pressure(arrays: Dict[str, list], now_ts: float) -> Optional[float]:
    key = _hour_key(now_ts)
    values = arrays.get("surface_pressure", [])
    for index, stamp in enumerate(arrays["time"]):
        if stamp == key and values[index] is not None:
            return round(float(values[index]), 1)
    for value in values:
        if value is not None:
            return round(float(value), 1)
    return None


def is_stale(last_weather: Optional[Dict[str, Any]], now_ts: float) -> bool:
    if not last_weather or not isinstance(last_weather.get("fetched_at"), (int, float)):
        return True
    return now_ts - last_weather["fetched_at"] > WEATHER_STALE


def blocks(last_weather: Optional[Dict[str, Any]], now_ts: float, block_hours: int = 3) -> List[Dict[str, Any]]:
    """Three rolling blocks starting with the one containing ``now`` (local clock)."""
    local = clock.local_now(now_ts)
    start_hour = (local.hour // block_hours) * block_hours
    start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    arrays = (last_weather or {}).get("hourly") or {}
    index_by_time = {stamp: i for i, stamp in enumerate(arrays.get("time", []))}
    out = []
    for i in range(3):
        block_start = start + timedelta(hours=block_hours * i)
        block_end = block_start + timedelta(hours=block_hours)
        label = f"{block_start.hour:02d}–{block_end.hour % 24:02d}"
        temps, rains, codes, days = [], [], [], []
        for h in range(block_hours):
            stamp = (block_start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00")
            idx = index_by_time.get(stamp)
            if idx is None:
                continue
            t = arrays["temperature_2m"][idx]
            r = arrays["precipitation_probability"][idx]
            c = arrays["weathercode"][idx]
            d = arrays["is_day"][idx]
            if t is not None:
                temps.append(float(t))
            if r is not None:
                rains.append(float(r))
            if c is not None:
                codes.append(int(c))
            if d is not None:
                days.append(int(d))
        out.append({
            "label": label,
            "t_max": round(max(temps), 1) if temps else None,
            "t_min": round(min(temps), 1) if temps else None,
            "rain": int(round(max(rains))) if rains else None,
            "wmo": max(codes, key=wmo_severity) if codes else None,
            "is_night": bool(days) and sum(days) * 2 < len(days),  # more night hours than day hours
        })
    return out


def summarize(last_weather: Optional[Dict[str, Any]], now_ts: float, block_hours: int = 3) -> Dict[str, Any]:
    """The ``weather`` part of ``display_data``."""
    stale = is_stale(last_weather, now_ts)
    return {
        "stale": stale,
        "fetched_at": (last_weather or {}).get("fetched_at"),
        "blocks": blocks(last_weather, now_ts, block_hours) if not stale else [],
    }
