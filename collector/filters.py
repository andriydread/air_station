"""The rules that decide a number is garbage — the only judging the collector does.

A dropped value becomes an empty cell (NULL) in its row; the row is still
written. Limits follow the datasheets: the SCD4x output range tops out at
40 000 ppm (a reading above it is a corrupt transfer, the classic 0xFFFF
word), CO2 below 350 ppm is not indoor air, the SHT4x/SCD4x temperature
and humidity ranges, and particle numbers can never be negative.
"""

import math
from typing import Any, Dict, Optional, Tuple

from shared.db import METRICS, round_metric

CO2_MIN = 350
CO2_MAX = 40_000
TEMP_RANGE = (-40.0, 85.0)
HUMID_RANGE = (0.0, 100.0)

REASON_NONFINITE = "nonfinite"
REASON_RANGE = "range"
REASON_NEGATIVE = "negative"


def clean(field: str, value: Any) -> Tuple[Optional[float], Optional[str]]:
    """(cleaned value, None) when believable; (None, reason) when dropped.

    ``(None, None)`` means the value was simply not read (nothing to judge).
    """
    if value is None:
        return None, None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, REASON_NONFINITE
    if not math.isfinite(number):
        return None, REASON_NONFINITE
    if field == "co2":
        if not CO2_MIN <= number <= CO2_MAX:
            return None, REASON_RANGE
        return round_metric(field, number), None
    if field in ("temp", "co2_temp"):
        if not TEMP_RANGE[0] <= number <= TEMP_RANGE[1]:
            return None, REASON_RANGE
        return round_metric(field, number), None
    if field in ("humid", "co2_humid"):
        if not HUMID_RANGE[0] <= number <= HUMID_RANGE[1]:
            return None, REASON_RANGE
        return round_metric(field, number), None
    # particulates: mass, number concentrations, typical particle size
    if number < 0:
        return None, REASON_NEGATIVE
    return round_metric(field, number), None


def clean_row(raw: Dict[str, Any]) -> Tuple[Dict[str, Optional[float]], Dict[str, Tuple[Any, str]]]:
    """Apply ``clean`` to every metric: (row for the database, {field: (raw, reason)})."""
    row: Dict[str, Optional[float]] = {}
    dropped: Dict[str, Tuple[Any, str]] = {}
    for field in METRICS:
        if field not in raw:
            row[field] = None
            continue
        value, reason = clean(field, raw[field])
        row[field] = value
        if reason is not None:
            dropped[field] = (raw[field], reason)
    return row, dropped
