"""/api/vitals: buckets, latest row, decoded power bits."""

import pytest

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


def _row(ts, **kw):
    base = {"recorded_at": ts, "cpu_temp": 48.0, "load": 0.3, "mem_free": 210, "disk_free": 11800,
            "db_size": 41.7, "wifi_rssi": -61, "wifi_link": 43.3, "lan_ms": 3.0, "wan_ms": 18.0,
            "throttled": 0, "uptime": 86000, "collector_lag": 4}
    base.update(kw)
    return base


def test_empty(client):
    body = client.get("/api/vitals").get_json()
    assert body["rows"] == [] and body["latest"] is None and body["bucket_seconds"] == 300


def test_buckets_latest_and_decoded_bits(client, db):
    for i in range(30):
        db.insert_vitals(_row(NOW - 1800 + 60 * i, cpu_temp=40.0 + i, throttled=0x50005 if i == 7 else 0))
    body = client.get(f"/api/vitals?from={NOW - 1800}&to={NOW}").get_json()
    assert body["bucket_seconds"] == 60 and len(body["rows"]) == 30   # a 30 min span keeps the minute rows
    hot = [r for r in body["rows"] if r["throttled"]]
    assert len(hot) == 1 and hot[0]["throttled_now"] == ["undervoltage", "throttled"]
    assert hot[0]["throttled_since_boot"] == ["undervoltage", "throttled"]
    assert body["latest"]["cpu_temp"] == 69.0 and body["latest"]["throttled_now"] == []
    day = client.get(f"/api/vitals?from={NOW - 86400}&to={NOW}").get_json()
    assert day["bucket_seconds"] == 300 and len(day["rows"]) == 6
    assert any(r["throttled"] == 0x50005 for r in day["rows"])  # the set bit survives bucketing


def test_validation(client):
    assert client.get(f"/api/vitals?from={NOW}&to={NOW - 1}").status_code == 400
