"""Plausibility checks for sensor values.

Stored rows are never filtered. The collector uses these checks to count bad
readings for sensor resets, and the manager drops implausible values from the
minute average. Limits come from the datasheets: the SCD4x tops out at
40000 ppm (anything above is a corrupt transfer such as 0xFFFF), CO2 under
10 ppm means a dead sensor (a freshly calibrated one can read a bit below
outdoor air, so the floor is low), plus the SHT4x/SCD4x temperature and
humidity ranges. Particle values are never negative.
"""

import math
from typing import Any, Optional

CO2_MIN = 10
CO2_MAX = 40_000
TEMP_RANGE = (-40.0, 85.0)
HUMID_RANGE = (0.0, 100.0)

REASON_NONFINITE = "nonfinite"
REASON_RANGE = "range"
REASON_NEGATIVE = "negative"


def implausible(metric: str, value: Any) -> Optional[str]:
    """Return why a value is implausible, or None if it is fine or missing."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return REASON_NONFINITE
    if not math.isfinite(number):
        return REASON_NONFINITE
    if metric == "co2":
        return None if CO2_MIN <= number <= CO2_MAX else REASON_RANGE
    if metric in ("temp", "co2_temp"):
        return None if TEMP_RANGE[0] <= number <= TEMP_RANGE[1] else REASON_RANGE
    if metric in ("humid", "co2_humid"):
        return None if HUMID_RANGE[0] <= number <= HUMID_RANGE[1] else REASON_RANGE
    return REASON_NEGATIVE if number < 0 else None  # mass, counts, typical particle size


def plausible(metric: str, value: Any) -> bool:
    """True if the value is present and plausible."""
    return value is not None and implausible(metric, value) is None
