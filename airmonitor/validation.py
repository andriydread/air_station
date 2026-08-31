"""Plausibility limits for sensor readings — the single source of truth.

Both the sensor wrappers (reject at read time) and storage (guard at write
time) import from here, so the definition of "believable data" can never
silently diverge between the two.
"""

from typing import Any, Optional

DEFAULT_MIN_VALID_CO2_PPM = 350
VALID_TEMPERATURE = (-40.0, 85.0)
VALID_HUMIDITY = (0.0, 100.0)


def clean_value(
    field: str,
    value: Any,
    min_valid_co2_ppm: int = DEFAULT_MIN_VALID_CO2_PPM,
) -> Optional[float]:
    """Round a raw reading and drop it (return None) when it is implausible."""
    if value is None:
        return None
    number = float(value)
    if field == "co2":
        number = int(round(number))
        return number if number >= min_valid_co2_ppm else None
    number = round(number, 2)
    if field == "temp":
        return number if VALID_TEMPERATURE[0] <= number <= VALID_TEMPERATURE[1] else None
    if field == "humid":
        return number if VALID_HUMIDITY[0] <= number <= VALID_HUMIDITY[1] else None
    # particulate matter fields: must not be negative
    return number if number >= 0 else None
