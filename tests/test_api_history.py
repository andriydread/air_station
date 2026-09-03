"""/api/history: raw vs hourly source, buckets, per-row AQI, stats, validation."""

import pytest

from dashboard.api import choose_bucket_seconds
from dashboard.app import create_app
from shared import clock
from shared.events import Log

NOW = 1_788_436_800


@pytest.fixture
def client(tmp_config, db, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: float(NOW))
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    yield app.test_client()
    log.close()


def _fill(db, start, end, step=10):
    for i, t in enumerate(range(start, end, step)):
        db.insert_raw(t, {"co2": 700 + (i % 50), "temp": 22.0, "pm25": 4.0 + (i % 3), "pm10": 6.0,
                          "co2_temp": 23.0, "nc25": 8.0, "tps": 0.5})


@pytest.mark.parametrize("span, bucket", [
    (3600, 10), (2 * 3600, 10), (2 * 3600 + 1, 60), (6 * 3600, 60), (12 * 3600, 300),
    (2 * 86400, 900), (7 * 86400, 1800), (30 * 86400, 3600), (60 * 86400, 10800), (400 * 86400, 86400),
])
def test_bucket_choice(span, bucket):
    assert choose_bucket_seconds(span) == bucket


def test_default_range_is_the_last_day_with_raw_buckets(client, db):
    _fill(db, NOW - 3600, NOW)
    body = client.get("/api/history").get_json()
    assert body["from"] == NOW - 86400 and body["to"] == NOW
    assert body["resolution"] == "raw" and body["bucket_seconds"] == 300
    assert len(body["rows"]) == 12 and body["rows"][0]["ts"] % 300 == 0
    row = body["rows"][0]
    assert row["aqi"] is not None and 20 <= row["aqi"] <= 35 and row["co2_temp"] == 23.0
    assert body["stats"]["co2"]["n"] == 360 and body["stats"]["nc25"]["avg"] == 8.0


def test_short_range_uses_ten_second_buckets(client, db):
    _fill(db, NOW - 600, NOW)
    body = client.get(f"/api/history?from={NOW - 600}&to={NOW}").get_json()
    assert body["bucket_seconds"] == 10 and len(body["rows"]) == 60


def test_range_before_the_raw_horizon_switches_to_hourly(client, db, tmp_config):
    horizon = NOW - tmp_config.retention_days.raw * 86400
    hour = (horizon - 5 * 3600) // 3600 * 3600
    _fill(db, hour, hour + 3600)
    db.rollup_hour(hour)
    body = client.get(f"/api/history?from={hour - 3600}&to={hour + 2 * 3600}").get_json()
    assert body["resolution"] == "hourly" and body["bucket_seconds"] == 3600
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["ts"] == hour and row["samples"] == 360 and row["co2_min"] == 700 and row["co2_max"] == 749
    assert row["aqi"] is not None and body["stats"]["co2"]["n"] == 360


def test_long_range_inside_the_window_is_hourly_too(client, db):
    _fill(db, NOW - 2 * 3600, NOW, step=60)
    db.rollup_catchup(NOW)
    body = client.get(f"/api/history?from={NOW - 40 * 86400 + 1}&to={NOW}").get_json()
    assert body["resolution"] == "hourly"


def test_validation(client):
    assert client.get("/api/history?from=abc").status_code == 400
    assert client.get(f"/api/history?from={NOW}&to={NOW - 1}").status_code == 400
    assert client.get(f"/api/history?from={NOW - 6 * 365 * 86400}&to={NOW}").status_code == 400
    assert client.get(f"/api/history?from={NOW - 60}&to={NOW}").get_json()["rows"] == []
