"""vitals table: insert, latest, buckets with bit-preserving throttled, prune."""

from shared.db import VITALS_COLUMNS


def _row(ts, **kw):
    base = {"recorded_at": ts, "cpu_temp": 48.0, "load": 0.3, "mem_free": 210, "disk_free": 11800,
            "db_size": 41.7, "wifi_rssi": -61, "wifi_link": 43.3, "lan_ms": 3.0, "wan_ms": 18.0,
            "throttled": 0, "uptime": 86000, "collector_lag": 4}
    base.update(kw)
    return base


def test_insert_latest_and_optional_columns(db):
    db.insert_vitals({"recorded_at": 100, "cpu_temp": 50.5})
    db.insert_vitals(_row(160))
    latest = db.latest_vitals()
    assert latest["recorded_at"] == 160 and latest["wifi_rssi"] == -61
    first = db.vitals_between(0, 150)[0]
    assert first["cpu_temp"] == 50.5 and first["load"] is None
    assert set(first) == {"recorded_at", *VITALS_COLUMNS}


def test_bucketed_averages_keep_a_set_throttled_bit(db):
    db.insert_vitals(_row(600, cpu_temp=40.0, throttled=0))
    db.insert_vitals(_row(660, cpu_temp=50.0, throttled=0x50005))  # under-voltage now + since boot, throttled since boot
    db.insert_vitals(_row(720, cpu_temp=60.0, throttled=0))
    db.insert_vitals(_row(1800, cpu_temp=70.0, throttled=1 << 3))
    rows = db.vitals_bucketed(600, 3600, 900)
    assert [r["ts"] for r in rows] == [0 + 600 // 900 * 900, 1800]
    assert rows[0]["cpu_temp"] == 50.0
    assert rows[0]["throttled"] == 0x50005          # OR over the bucket, not the average
    assert rows[1]["throttled"] == 8


def test_prune(db):
    for ts in (100, 200, 300):
        db.insert_vitals(_row(ts))
    assert db.prune_vitals(before_ts=250) == 2
    assert [r["recorded_at"] for r in db.vitals_between(0, 1000)] == [300]
    assert db.latest_vitals()["recorded_at"] == 300


def test_empty_table(db):
    assert db.latest_vitals() is None
    assert db.vitals_bucketed(0, 10, 5) == []
