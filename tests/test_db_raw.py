"""raw_measurements helpers: insert, minute average, buckets, stats."""

from shared.db import METRICS, round_metric


def _row(co2=800, temp=22.0, **extra):
    values = {"co2": co2, "co2_temp": 23.0, "co2_humid": 41.0, "temp": temp, "humid": 45.0,
              "pm1": 1.0, "pm25": 2.0, "pm10": 4.0, "tps": 0.5, "nc05": 7.0, "nc1": 8.0, "nc25": 8.5}
    values.update(extra)
    return values


def test_insert_stores_nulls_for_missing_and_none_metrics(db):
    db.insert_raw(1000, {"co2": None, "temp": 21.5})
    row = db.raw_between(1000, 1001)[0]
    assert row["recorded_at"] == 1000
    assert row["co2"] is None and row["temp"] == 21.5 and row["pm25"] is None
    assert set(row) == {"recorded_at", *METRICS}


def test_same_second_replaces_and_latest_oldest(db):
    db.insert_raw(1000, _row(co2=700))
    db.insert_raw(1000, _row(co2=750))
    db.insert_raw(1010, _row())
    assert len(db.raw_between(0, 2000)) == 2
    assert db.raw_between(1000, 1001)[0]["co2"] == 750
    assert db.latest_raw_at() == 1010 and db.raw_oldest_at() == 1000


def test_minute_average_window_and_nulls(db):
    for i, co2 in enumerate([800, None, 820, 830, 840, 850]):
        db.insert_raw(1000 + 10 * i, _row(co2=co2, temp=20.0 + i))
    db.insert_raw(940, _row(co2=100))   # exactly now-60: excluded (window is exclusive at the start)
    db.insert_raw(1060, _row(co2=9000))  # after now: excluded
    result = db.minute_average(now=1050)
    assert result["values"]["co2"] == 828          # (800+820+830+840+850)/5 = 828
    assert result["samples"]["co2"] == 5
    assert result["values"]["temp"] == 22.5 and result["samples"]["temp"] == 6


def test_minute_average_on_empty_table_is_none_not_error(db):
    result = db.minute_average(now=5000)
    assert result["values"]["co2"] is None and result["samples"]["co2"] == 0


def test_bucketed_averages(db):
    for i in range(12):  # two minutes of 10 s rows
        db.insert_raw(3600 + 10 * i, _row(co2=600 + i))
    rows = db.raw_bucketed(3600, 3720, 60)
    assert [r["ts"] for r in rows] == [3600, 3660]
    assert rows[0]["co2"] == round((600 + 605) / 2)  # 602.5 -> 602 (banker's) — accept int
    assert isinstance(rows[0]["co2"], int)
    assert rows[1]["tps"] == 0.5


def test_stats(db):
    for i, co2 in enumerate([600, 700, None, 800]):
        db.insert_raw(100 + 10 * i, _row(co2=co2))
    stats = db.raw_stats(100, 200)
    assert stats["co2"] == {"min": 600, "max": 800, "avg": 700, "n": 3}
    assert stats["temp"]["n"] == 4
    assert db.raw_stats(5000, 6000)["co2"] == {"min": None, "max": None, "avg": None, "n": 0}


def test_round_metric_rules():
    assert round_metric("co2", 812.6) == 813
    assert round_metric("tps", 0.54321) == 0.543
    assert round_metric("temp", 23.456) == 23.46
    assert round_metric("pm25", None) is None
