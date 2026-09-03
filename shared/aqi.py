"""Air-quality index from PM2.5 (US EPA, May-2024 breakpoints) and the
category words for the index and for CO2.

PM10 no longer takes part: the SPS30 only measures particles up to about
2.5 µm and calculates PM10 (±25 µg/m³), so it must not drive the headline
number (redesign.md §4). CO2 uses the German UBA indoor-air scale.
"""

from typing import Optional, Tuple

# (concentration high, index low, index high) per EPA bracket, PM2.5 µg/m³.
_PM25_BRACKETS = (
    (9.0, 0, 50),
    (35.4, 51, 100),
    (55.4, 101, 150),
    (125.4, 151, 200),
    (225.4, 201, 300),
    (325.4, 301, 500),
)

_AQI_CATEGORIES = (
    (50, "Good", "Good"),
    (100, "Moderate", "Moderate"),
    (150, "Unhealthy for Sensitive Groups", "Sensitive"),
    (200, "Unhealthy", "Unhealthy"),
    (300, "Very Unhealthy", "Very Unhealthy"),
)
_AQI_TOP = ("Hazardous", "Hazardous")

CO2_GUIDES = (1000, 2000)  # ppm: Good below the first, Elevated below the second, Poor above


def aqi_from_pm25(pm25: Optional[float]) -> Optional[int]:
    """EPA index for a PM2.5 mass concentration; None for None or non-finite."""
    if pm25 is None:
        return None
    try:
        value = float(pm25)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    # EPA truncates to one decimal before the lookup; without it a gap value
    # like 35.45 selects the bracket above and interpolates to 101, not 100.
    value = int(max(0.0, value) * 10) / 10.0
    low_conc = 0.0
    for high_conc, low_index, high_index in _PM25_BRACKETS:
        if value <= high_conc:
            span = high_conc - low_conc
            index = (high_index - low_index) / span * (value - low_conc) + low_index
            return int(round(index))
        low_conc = high_conc + 0.1
    return 500


def aqi_category(aqi: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """(full name, short word for the panel)."""
    if aqi is None:
        return None, None
    for limit, full, short in _AQI_CATEGORIES:
        if aqi <= limit:
            return full, short
    return _AQI_TOP


def co2_category(ppm: Optional[float]) -> Optional[str]:
    if ppm is None:
        return None
    try:
        value = float(ppm)
    except (TypeError, ValueError):
        return None
    if value < CO2_GUIDES[0]:
        return "Good"
    if value < CO2_GUIDES[1]:
        return "Elevated"
    return "Poor"
