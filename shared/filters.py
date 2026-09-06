"""What cannot be air.

The collector stores every value the sensors give (decided 2026-09-06);
this rule never touches a row. It is used in two places only: the collector
counts a reading as *bad* for the reset ladder, and the manager leaves the
value out of the panel's minute average. Limits follow the datasheets: the
SCD4x output range tops out at 40 000 ppm (above it is a corrupt transfer,
the classic 0xFFFF word), CO2 below 350 ppm is not indoor air, the SHT4x /
SCD4x temperature and humidity ranges, and particle numbers are never
negative.
"""

import math
from typing import Any, Optional

CO2_MIN = 350
CO2_MAX = 40_000
TEMP_RANGE = (-40.0, 85.0)
HUMID_RANGE = (0.0, 100.0)

REASON_NONFINITE = "nonfinite"
REASON_RANGE = "range"
REASON_NEGATIVE = "negative"


def implausible(metric: str, value: Any) -> Optional[str]:
    """The reason a value cannot be air, or None. ``None`` for a value not read."""
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
    """True for a value that was read and could be air."""
    return value is not None and implausible(metric, value) is None
