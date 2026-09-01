"""utils.weather tests: happy path, broken payloads, network failure."""

import requests

from utils.weather import get_weather_forecast


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.requested_url = None

    def get(self, url, headers=None, timeout=None):
        self.requested_url = url
        return self._response


def _hourly(values=None):
    """24 hourly entries; index == hour of day."""
    temps = [10.0 + hour * 0.5 for hour in range(24)]
    precip = [hour for hour in range(24)]
    codes = [0] * 24
    if values:
        temps, precip, codes = values
    return {
        "temperature_2m": temps,
        "precipitation_probability": precip,
        "weathercode": codes,
    }


def test_happy_path_aggregates_three_blocks():
    session = FakeSession(FakeResponse({"hourly": _hourly()}))
    forecast = get_weather_forecast(50.0, 24.0, session)
    assert set(forecast) == {1, 2, 3}
    label, t_max, t_min, precip, code = forecast[1]
    assert label == "09:00-12:00"
    # hours 9..12: temps 14.5..16.0
    assert t_max == 16.0
    assert t_min == 14.5
    assert precip == 12  # max precipitation probability in the window
    assert code == 0


def test_missing_hourly_returns_empty():
    session = FakeSession(FakeResponse({"unexpected": True}))
    assert get_weather_forecast(50.0, 24.0, session) == {}


def test_http_error_returns_empty():
    session = FakeSession(FakeResponse(error=requests.HTTPError("500")))
    assert get_weather_forecast(50.0, 24.0, session) == {}


def test_null_holes_in_arrays_are_tolerated():
    temps = [None] * 24
    precip = [None] * 24
    codes = [None] * 24
    session = FakeSession(FakeResponse({"hourly": (lambda: {
        "temperature_2m": temps,
        "precipitation_probability": precip,
        "weathercode": codes,
    })()}))
    forecast = get_weather_forecast(50.0, 24.0, session)
    assert forecast[1] == ["09:00-12:00", None, None, None, None]


def test_short_arrays_are_tolerated():
    session = FakeSession(FakeResponse({"hourly": {
        "temperature_2m": [1.0, 2.0],  # far fewer than 24 entries
        "precipitation_probability": [],
        "weathercode": "not-a-list",
    }}))
    forecast = get_weather_forecast(50.0, 24.0, session)
    assert forecast[2] == ["13:00-17:00", None, None, None, None]


def test_block_icon_prefers_worst_weather_not_highest_code():
    """WMO codes aren't ordered by severity — plain numeric max picked snow
    (71) over heavy rain (65) and fog (48) over drizzle-free storm hours."""
    from utils.weather import _wmo_severity

    assert _wmo_severity(95) > _wmo_severity(86)   # thunderstorm beats snow
    assert _wmo_severity(82) > _wmo_severity(45)   # violent rain beats fog
    assert _wmo_severity(61) > _wmo_severity(3)    # any rain beats clouds
    assert max([3, 45, 61], key=_wmo_severity) == 61
    assert max([65, 71], key=_wmo_severity) == 71  # snow still tops rain
    assert max([95, 45, 2], key=_wmo_severity) == 95
