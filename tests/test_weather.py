"""Open-Meteo parsing, rolling blocks, stale rule, pressure, failures."""

import io
import json
import time as _time
from datetime import datetime, timedelta

import pytest

from manager import weather
from manager.weather import WeatherError, blocks, build_url, fetch, is_stale, parse, summarize, wmo_severity


@pytest.fixture(autouse=True)
def kyiv(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    yield
    monkeypatch.delenv("TZ")
    _time.tzset()


def _payload(start_local="2026-09-03T00:00", hours=48):
    start = datetime.fromisoformat(start_local)
    times = [(start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:00") for h in range(hours)]
    return {
        "timezone": "Europe/Kyiv", "utc_offset_seconds": 10800,
        "hourly": {
            "time": times,
            "temperature_2m": [10 + (h % 24) * 0.5 for h in range(hours)],
            "precipitation_probability": [h % 100 for h in range(hours)],
            "weathercode": [0 if h < 12 else (61 if h < 15 else (95 if h == 20 else 3)) for h in range(hours)],
            "surface_pressure": [980.0 + h * 0.1 for h in range(hours)],
            "is_day": [1 if 6 <= (h % 24) < 20 else 0 for h in range(hours)],
        },
    }


def _local_ts(text):
    return datetime.fromisoformat(text).astimezone().timestamp()


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_url_has_the_location_fields_and_two_days(tmp_config):
    url = build_url(tmp_config)
    assert "latitude=49.842957" in url and "forecast_days=2" in url and "timezone=auto" in url
    assert "surface_pressure" in url and "is_day" in url


def test_fetch_parses_and_keeps_48_hours(tmp_config):
    seen = {}

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return _Response(json.dumps(_payload()).encode())

    now = _local_ts("2026-09-03T12:34:00")
    doc = fetch(tmp_config, opener=opener, now=now)
    assert seen["timeout"] == 10 and "open-meteo" in seen["url"]
    assert doc["fetched_at"] == int(now) and len(doc["hourly"]["time"]) == 48
    assert doc["pressure_hpa"] == pytest.approx(981.2)  # the 12:00 entry
    assert doc["timezone"] == "Europe/Kyiv" and doc["bytes"] > 100


def test_fetch_failures_become_weather_error(tmp_config):
    def broken(_request, timeout):
        raise OSError("no route")

    with pytest.raises(WeatherError, match="OSError"):
        fetch(tmp_config, opener=broken)

    class Bad(_Response):
        status = 503

    with pytest.raises(WeatherError, match="HTTP 503"):
        fetch(tmp_config, opener=lambda r, timeout: Bad(b"{}"))
    with pytest.raises(WeatherError, match="bad JSON"):
        fetch(tmp_config, opener=lambda r, timeout: _Response(b"<html>"))
    with pytest.raises(WeatherError, match="missing hourly"):
        parse({"hourly": {"time": "x"}})
    ragged = _payload()
    ragged["hourly"]["weathercode"] = ragged["hourly"]["weathercode"][:-1]
    with pytest.raises(WeatherError, match="ragged"):
        parse(ragged)


def test_blocks_start_with_the_current_block_and_shift(tmp_config):
    doc = parse(_payload(), now=_local_ts("2026-09-03T12:34:00"))
    noon = blocks(doc, _local_ts("2026-09-03T13:10:00"))
    assert [b["label"] for b in noon] == ["12–15", "15–18", "18–21"]
    assert noon[0]["wmo"] == 61 and noon[1]["wmo"] == 3        # rain block, then clouds
    assert noon[0]["t_max"] == 17.0 and noon[0]["t_min"] == 16.0
    assert noon[0]["rain"] == 14 and noon[0]["is_night"] is False
    later = blocks(doc, _local_ts("2026-09-03T15:00:00"))
    assert [b["label"] for b in later] == ["15–18", "18–21", "21–00"]
    assert later[1]["wmo"] == 95  # the thunderstorm at 20:00 wins the 18–21 block


def test_night_blocks_and_midnight_wrap(tmp_config):
    doc = parse(_payload(), now=_local_ts("2026-09-03T12:00:00"))
    night = blocks(doc, _local_ts("2026-09-03T23:30:00"))
    assert [b["label"] for b in night] == ["21–00", "00–03", "03–06"]
    assert [b["is_night"] for b in night] == [True, True, True]
    assert night[1]["t_max"] is not None  # tomorrow's data is there (two forecast days)


def test_blocks_beyond_the_data_are_empty_not_errors():
    doc = parse(_payload(hours=6), now=_local_ts("2026-09-03T00:10:00"))
    out = blocks(doc, _local_ts("2026-09-03T05:00:00"))
    assert out[0]["t_max"] is not None and out[1]["t_max"] is None and out[2]["wmo"] is None
    assert [b["label"] for b in out] == ["03–06", "06–09", "09–12"]
    assert blocks(None, _local_ts("2026-09-03T05:00:00"))[0]["label"] == "03–06"


def test_block_hours_config(tmp_config):
    doc = parse(_payload(), now=_local_ts("2026-09-03T12:00:00"))
    six = blocks(doc, _local_ts("2026-09-03T13:00:00"), block_hours=6)
    assert [b["label"] for b in six] == ["12–18", "18–00", "00–06"]


def test_stale_rule_and_summary():
    now = _local_ts("2026-09-03T12:00:00")
    doc = parse(_payload(), now=now)
    assert is_stale(doc, now + 6 * 3600) is False
    assert is_stale(doc, now + 6 * 3600 + 1) is True
    assert is_stale(None, now) is True and is_stale({}, now) is True
    fresh = summarize(doc, now + 60)
    assert fresh["stale"] is False and len(fresh["blocks"]) == 3 and fresh["fetched_at"] == int(now)
    stale = summarize(doc, now + 7 * 3600)
    assert stale["stale"] is True and stale["blocks"] == []


def test_wmo_severity_order():
    assert max([82, 85], key=wmo_severity) == 85   # snow outranks violent showers
    assert max([3, 999], key=wmo_severity) == 999  # unknown codes tie with clouds, higher code wins
    assert max([0, 45, 2], key=wmo_severity) == 45


def test_pressure_falls_back_to_the_first_value_when_the_hour_is_missing():
    doc = parse(_payload(hours=3), now=_local_ts("2026-09-04T12:00:00"))
    assert doc["pressure_hpa"] == 980.0
