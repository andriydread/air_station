import logging
from typing import Any, Dict, Optional

import requests


LOGGER = logging.getLogger("airmonitor.weather")

# WMO weather codes are NOT ordered by severity (85 "slight snow showers"
# outranks 82 "violent rain showers" numerically). Rank them so a block's
# icon shows its worst weather, with the raw code as tiebreaker.
_WMO_SEVERITY_RANKS = (
    ({0, 1}, 0),                                    # clear
    ({2}, 1),                                       # partly cloudy
    ({3}, 2),                                       # overcast
    ({45, 48}, 3),                                  # fog
    ({51, 53, 55, 56, 57}, 4),                      # drizzle
    ({61, 63, 80}, 5),                              # light/moderate rain
    ({65, 66, 67, 81, 82}, 6),                      # heavy/freezing rain
    ({71, 73, 75, 77, 85, 86}, 7),                  # snow
    ({95, 96, 99}, 8),                              # thunderstorms
)


def _wmo_severity(code: int):
    for codes, rank in _WMO_SEVERITY_RANKS:
        if code in codes:
            return (rank, code)
    return (2, code)  # unknown codes: treat like plain clouds


def get_weather_forecast(
    lat: float, lon: float, session: Optional[requests.Session] = None
) -> Dict[int, Any]:
    """
    Fetch today's hourly weather forecast and aggregate it into three blocks.
    Returns an empty dict when the upstream request fails or the payload is incomplete.
    """
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=temperature_2m,precipitation_probability,weathercode"
        "&timezone=auto&forecast_days=1"
    )

    try:
        fetcher = session if session else requests
        response = fetcher.get(
            url,
            headers={"User-Agent": "AirStation/1.0 (RaspberryPi)"},
            timeout=5.0,
        )
        response.raise_for_status()
        data = response.json()
        hourly = data.get("hourly")
        if not isinstance(hourly, dict):
            raise ValueError("missing hourly weather payload")

        weather_dict: Dict[int, Any] = {}
        blocks = [
            (9, 12, "09:00-12:00"),
            (13, 17, "13:00-17:00"),
            (18, 22, "18:00-22:00"),
        ]

        def safe_slice(key: str, start: int, end: int):
            arr = hourly.get(key, [])
            if not isinstance(arr, list):
                return []
            return [value for value in arr[start : end + 1] if value is not None]

        for index, (start_h, end_h, time_str) in enumerate(blocks, start=1):
            wmo_slice = safe_slice("weathercode", start_h, end_h)
            temp_slice = safe_slice("temperature_2m", start_h, end_h)
            precip_slice = safe_slice("precipitation_probability", start_h, end_h)
            weather_dict[index] = [
                time_str,
                round(max(temp_slice), 1) if temp_slice else None,
                round(min(temp_slice), 1) if temp_slice else None,
                max(precip_slice) if precip_slice else None,
                max(wmo_slice, key=_wmo_severity) if wmo_slice else None,
            ]

        return weather_dict
    except Exception as exc:
        LOGGER.warning("Weather fetch failed: %s", exc)
        return {}
