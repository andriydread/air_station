"""hourly_measurements: rollup of one hour, catch-up, stats."""

from shared.db import METRICS

H = 3600
BASE = 1_756_900_800  # a whole hour


def _fill_hour(db, hour_ts, co2_values, temp=20.0):
    for i, co2 in enumerate(co2_values):
        db.insert_raw(hour_ts + 10 * i, {"co2": co2, "temp": temp, "pm25": 2.0, "tps": 0.5})


def test_rollup_hour_min_max_avg_and_null_handling(db):
    _fill_hour(db, BASE, [800, None, 900, 1000])
    assert db.rollup_hour(BASE) == 4
    row = db.hourly_between(BASE, BASE + H)[0]
    assert row["hour"] == BASE and row["samples"] == 4
    assert (row["co2_min"], row["co2_max"], row["co2_avg"]) == (800, 1000, 900)
    assert row["temp_avg"] == 20.0 and row["humid_avg"] is None
    assert set(row) == {"hour", "samples", *(f"{m}_{s}" for m in METRICS for s in ("min", "max", "avg"))}


def test_empty_hour_writes_no_row(db):
    assert db.rollup_hour(BASE) == 0
    assert db.hourly_between(BASE, BASE + H) == []
    assert db.last_rolled_hour() is None


def test_rollup_is_idempotent_and_rounds_to_the_hour(db):
    _fill_hour(db, BASE, [600, 700])
    db.rollup_hour(BASE + 1234)  # any second inside the hour
    db.rollup_hour(BASE)
    assert len(db.hourly_between(0, BASE + 10 * H)) == 1


def test_catchup_rolls_only_hours_with_data_and_stops_before_now(db):
    _fill_hour(db, BASE, [600])            # hour 0
    _fill_hour(db, BASE + 3 * H, [700])    # hour 3 (hours 1-2 empty)
    _fill_hour(db, BASE + 4 * H, [800])    # hour 4 = the current, unfinished hour
    result = db.rollup_catchup(now=BASE + 4 * H + 600)
    assert result["rolled"] == 2 and result["hours"] == [BASE, BASE + 3 * H]
    assert result["skipped_future"] == 0 and result["remaining"] == 0
    assert [r["hour"] for r in db.hourly_between(0, BASE + 10 * H)] == [BASE, BASE + 3 * H]
    # a second call has nothing to do
    assert db.rollup_catchup(now=BASE + 4 * H + 600)["rolled"] == 0


def test_catchup_continues_after_the_last_rolled_hour(db):
    _fill_hour(db, BASE, [600])
    db.rollup_catchup(now=BASE + 2 * H)
    _fill_hour(db, BASE + H, [650])
    result = db.rollup_catchup(now=BASE + 2 * H)
    assert result["rolled"] == 1 and result["hours"] == [BASE + H]


def test_future_hours_are_skipped_and_counted(db):
    _fill_hour(db, BASE, [600])
    _fill_hour(db, BASE + 5 * H, [999])  # clock ran ahead: rows five hours in the future
    result = db.rollup_catchup(now=BASE + H + 60)
    assert result["rolled"] == 1 and result["skipped_future"] == 1
    assert db.last_rolled_hour() == BASE


def test_catchup_is_capped_and_reports_remaining(db, monkeypatch):
    monkeypatch.setattr(type(db), "CATCHUP_MAX_HOURS", 2)
    for k in range(5):
        _fill_hour(db, BASE + k * H, [600 + k])
    first = db.rollup_catchup(now=BASE + 5 * H + 30)
    assert first["rolled"] == 2 and first["remaining"] == 3
    second = db.rollup_catchup(now=BASE + 5 * H + 30)
    assert second["rolled"] == 2 and second["remaining"] == 1
    third = db.rollup_catchup(now=BASE + 5 * H + 30)
    assert third["rolled"] == 1 and third["remaining"] == 0


def test_hourly_stats_are_sample_weighted(db):
    _fill_hour(db, BASE, [600] * 2)          # 2 samples, avg 600
    _fill_hour(db, BASE + H, [900] * 6)      # 6 samples, avg 900
    db.rollup_catchup(now=BASE + 2 * H + 1)
    stats = db.hourly_stats(BASE, BASE + 2 * H)
    assert stats["co2"] == {"min": 600, "max": 900, "avg": 825, "n": 8}  # (600*2+900*6)/8
    assert stats["humid"] == {"min": None, "max": None, "avg": None, "n": 0}
