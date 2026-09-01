def calculate_aqi(pm25: float, pm10: float) -> int:
    """
    Calculates the US EPA AQI for both PM2.5 and PM10.
    The final AQI is always the highest (worst) index of all pollutants measured.
    """

    # EPA truncates concentrations before breakpoint lookup (PM2.5 to one
    # decimal, PM10 to integer); without it, gap values like 35.45 select the
    # bracket above and interpolate to 101 instead of the correct 100.
    pm25 = int(max(0.0, pm25) * 10) / 10.0
    pm10 = int(max(0.0, pm10))

    def _linear(aqi_high, aqi_low, conc_high, conc_low, conc):
        """Standard EPA linear interpolation formula."""
        return round(
            ((aqi_high - aqi_low) / (conc_high - conc_low)) * (conc - conc_low)
            + aqi_low
        )

    def aqi_pm25(c):
        # US EPA breakpoints, May 2024 revision ("Good" tightened to 9.0,
        # upper brackets re-cut, the 400-tier folded into 301-500).
        if c <= 9.0:
            return _linear(50, 0, 9.0, 0, c)
        if c <= 35.4:
            return _linear(100, 51, 35.4, 9.1, c)
        if c <= 55.4:
            return _linear(150, 101, 55.4, 35.5, c)
        if c <= 125.4:
            return _linear(200, 151, 125.4, 55.5, c)
        if c <= 225.4:
            return _linear(300, 201, 225.4, 125.5, c)
        if c <= 325.4:
            return _linear(500, 301, 325.4, 225.5, c)
        return 500

    def aqi_pm10(c):
        if c <= 54:
            return _linear(50, 0, 54, 0, c)
        if c <= 154:
            return _linear(100, 51, 154, 55, c)
        if c <= 254:
            return _linear(150, 101, 254, 155, c)
        if c <= 354:
            return _linear(200, 151, 354, 255, c)
        if c <= 424:
            return _linear(300, 201, 424, 355, c)
        if c <= 504:
            return _linear(400, 301, 504, 425, c)
        if c <= 604:
            return _linear(500, 401, 604, 505, c)
        return 500

    # Ensure negative readings (which can occur during sensor warmup) are floored to 0
    aqi25 = aqi_pm25(max(0, pm25))
    aqi10 = aqi_pm10(max(0, pm10))

    return max(aqi25, aqi10)


def get_aqi_category(aqi: int) -> str:
    """Translates the numerical AQI into health risk categories."""
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 175:
        return "Unhealthy"
    if aqi <= 300:
        return "Very Unhealthy"
    return "Hazardous"


def get_co2_category(co2_val):
    """Translates the numerical CO2 into health risk categories."""
    if not isinstance(co2_val, (int, float)):
        return "N/A"
    if co2_val < 1000:
        return "Good"
    if co2_val < 1500:
        return "Moderate"
    return "Unhealthy"
