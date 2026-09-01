"""Tests for airmonitor.storage.AirMonitorDatabase."""

import threading

import pytest

from airmonitor.storage import AirMonitorDatabase


@pytest.fixture
def database(tmp_path):
    db = AirMonitorDatabase(str(tmp_path / "test.db"))
    yield db
    db.close()


def _insert_raw(database, ts, co2, flagged=False):
    with database._lock:
        database._connection.execute(
            "INSERT INTO measurements (recorded_at, co2, temp, humid, pm1, pm25, pm4, pm10, tps, flags)"
            " VALUES (?, ?, 21.0, 45.0, 1.0, 3.0, 4.0, 5.0, 0.8, ?)",
            (ts, co2, '{"co2": {"value": 9999, "reason": "test"}}' if flagged else None),
        )


def test_hourly_rollup_is_incremental_and_skips_open_hour(database):
    current_hour = (database._now() // 3600) * 3600
    _insert_raw(database, current_hour - 7200, 600)
    _insert_raw(database, current_hour - 7200 + 60, None, flagged=True)
    _insert_raw(database, current_hour - 3600, 800)
    _insert_raw(database, current_hour + 10, 900)  # still-open hour: never rolled

    assert database.rollup_hourly() == 2
    assert database.rollup_hourly() == 0  # nothing new -> idempotent

    rows = database._query("SELECT * FROM measurements_hourly ORDER BY hour_ts")
    assert len(rows) == 2
    assert rows[0]["sample_count"] == 2
    assert rows[0]["flagged_count"] == 1
    assert rows[0]["co2_min"] == 600 and rows[0]["co2_avg"] == 600 and rows[0]["co2_max"] == 600
    assert rows[1]["co2_avg"] == 800


def test_history_and_stats_survive_raw_prune_via_rollups(database):
    current_hour = (database._now() // 3600) * 3600
    old = current_hour - 100 * 86400  # beyond the 90-day raw retention
    for i in range(3):
        _insert_raw(database, old + i * 600, 500 + i * 100)
    _insert_raw(database, current_hour + 10, 900)  # un-rolled raw tail

    assert database.rollup_hourly() == 1
    pruned = database.prune(90, 14)
    assert pruned["measurements"] == 3  # old raw gone, rollup remains

    rows = database.query_history_hourly(old - 3600, database._now() + 60, 3600)
    assert rows[0]["co2"] == 600  # avg of 500/600/700 from the rollup
    assert rows[-1]["co2"] == 900  # raw tail still visible

    stats = database.query_stats_hourly(old - 3600, database._now() + 60)
    assert stats["sample_count"] == 4
    assert stats["co2"]["min"] == 500
    assert stats["co2"]["max"] == 900


def test_rollup_self_heals_after_forward_clock_excursion(database, monkeypatch):
    current_hour = (database._now() // 3600) * 3600
    _insert_raw(database, current_hour - 3600, 700)

    # Clock jumps a week ahead (bad fake-hwclock restore), rolls a bogus hour.
    monkeypatch.setattr(
        AirMonitorDatabase, "_now", staticmethod(lambda: current_hour + 7 * 86400)
    )
    _insert_raw(database, current_hour + 7 * 86400 - 3600, 999)
    assert database.rollup_hourly() >= 1
    monkeypatch.undo()

    # Back on real time: the next rollup drops the future-dated rows.
    database.rollup_hourly()
    rows = database._query(
        "SELECT hour_ts FROM measurements_hourly WHERE hour_ts >= ?", (current_hour,)
    )
    assert rows == []  # nothing future-dated survives
    assert any(
        e["event_type"] == "rollup_clock_skew" for e in database.get_recent_events()
    )


def test_rollup_backfill_is_chunked_and_drains(database, monkeypatch):
    monkeypatch.setattr(AirMonitorDatabase, "ROLLUP_MAX_HOURS_PER_CALL", 2)
    current_hour = (database._now() // 3600) * 3600
    for i in range(1, 6):  # five complete hours of data
        _insert_raw(database, current_hour - i * 3600, 600 + i)

    total = 0
    calls = 0
    while True:
        step = database.rollup_hourly()
        if step == 0:
            break
        total += step
        calls += 1
    assert total == 5
    assert calls >= 3  # bounded chunks, not one giant statement


def test_rollup_skips_over_empty_stretch(database, monkeypatch):
    monkeypatch.setattr(AirMonitorDatabase, "ROLLUP_MAX_HOURS_PER_CALL", 2)
    current_hour = (database._now() // 3600) * 3600
    _insert_raw(database, current_hour - 10 * 3600, 700)  # old lone hour
    _insert_raw(database, current_hour - 3600, 800)       # recent hour

    while database.rollup_hourly():
        pass
    rolled = database._query(
        "SELECT hour_ts, sample_count FROM measurements_hourly "
        "WHERE sample_count > 0 ORDER BY hour_ts"
    )
    assert len(rolled) == 2  # both real hours present despite the gap


def test_delete_history_also_wipes_rollups(database):
    current_hour = (database._now() // 3600) * 3600
    _insert_raw(database, current_hour - 3600, 700)
    assert database.rollup_hourly() == 1
    database.delete_history()
    assert database._query("SELECT COUNT(*) AS n FROM measurements_hourly")[0]["n"] == 0


def test_export_rows_is_a_paged_generator(database):
    current = database._now()
    for i in range(25):
        _insert_raw(database, current - 1000 + i, 600 + i)
    rows = list(database.export_rows(current - 2000, current, chunk_size=10))
    assert len(rows) == 25
    assert rows[0]["co2"] == 600 and rows[-1]["co2"] == 624  # ordered, complete
    import types
    assert isinstance(database.export_rows(0, 1), types.GeneratorType)


def test_database_stats_counts_rows_and_disk_size(database):
    assert database.database_stats() == {
        "measurements": 0,
        "size_bytes": database.database_stats()["size_bytes"],
    }
    for _ in range(3):
        database.insert_measurement({"co2": 600, "temp": 21.0, "humid": 45.0})
    stats = database.database_stats()
    assert stats["measurements"] == 3
    assert stats["size_bytes"] > 0


def test_insert_and_latest_roundtrip(database):
    database.insert_measurement({"co2": 612.4, "temp": 21.37, "humid": 44.2, "pm25": 3.456})
    latest = database.get_latest_measurement()
    assert latest["co2"] == 612
    assert latest["temp"] == 21.37
    assert latest["humid"] == 44.2
    assert latest["pm25"] == 3.46
    assert latest["pm1"] is None


def test_implausible_values_stored_as_null(database):
    database.insert_measurement({"co2": 120, "temp": 130.0, "humid": -5.0, "pm25": -1.0})
    latest = database.get_latest_measurement()
    assert latest["co2"] is None
    assert latest["temp"] is None
    assert latest["humid"] is None
    assert latest["pm25"] is None


def test_command_lifecycle(database):
    command_id = database.queue_command("sps30_force_clean", {"a": 1})
    claimed = database.claim_pending_commands()
    assert [c["id"] for c in claimed] == [command_id]
    assert claimed[0]["payload"] == {"a": 1}
    # claimed commands are now running: a second claim gets nothing
    assert database.claim_pending_commands() == []
    database.complete_command(command_id, True, {"message": "done"})
    recent = database.get_recent_commands()
    assert recent[0]["status"] == "succeeded"
    assert recent[0]["result"] == {"message": "done"}


def test_stale_running_commands_reaped_only_by_the_collector(tmp_path):
    path = str(tmp_path / "crash.db")
    db = AirMonitorDatabase(path)
    db.queue_command("sps30_force_clean")
    db.claim_pending_commands()  # left 'running', simulating a crash

    # The dashboard opens the same file: merely opening must NOT reap a
    # command the collector may be executing right now.
    dashboard_side = AirMonitorDatabase(path)
    assert dashboard_side.get_recent_commands()[0]["status"] == "running"
    dashboard_side.close()
    db.close()

    # The collector's startup step is what fails the stale row.
    reopened = AirMonitorDatabase(path)
    try:
        reopened.fail_stale_running_commands()
        recent = reopened.get_recent_commands()
        assert recent[0]["status"] == "failed"
        assert "restarted" in recent[0]["result"]
    finally:
        reopened.close()


def test_events_filtering(database):
    database.insert_event("INFO", "scd41", "state_change", "ok")
    database.insert_event("warning", "network", "connectivity_check", "down")
    assert len(database.get_recent_events()) == 2
    warnings = database.get_recent_events(level="WARNING")
    assert [e["source"] for e in warnings] == ["network"]
    scd = database.get_recent_events(source="scd41")
    assert [e["level"] for e in scd] == ["info"]


def test_prune_and_delete_history(database):
    database.insert_measurement({"co2": 500})
    database.insert_event("info", "x", "y", "z")
    assert database.prune(0, 0) == {"measurements": 0, "events": 0}
    assert database.delete_history() == 1
    assert database.get_latest_measurement() is None


def test_concurrent_access_is_safe(database):
    """B1 regression: mixed reads/writes from many threads on one instance.

    Before the fix, cursors escaped the lock and concurrent requests raised
    sqlite3.ProgrammingError ("Recursive use of cursors") or returned
    interleaved garbage.
    """
    errors = []
    barrier = threading.Barrier(8)

    def worker(worker_id):
        try:
            barrier.wait()
            for i in range(50):
                database.insert_measurement({"co2": 400 + worker_id, "temp": 20.0 + i * 0.01})
                database.get_latest_measurement()
                database.query_history(hours=1, bucket_seconds=60)
                database.set_state(f"key-{worker_id}", {"i": i})
                database.get_state(f"key-{worker_id}")
                database.get_recent_events(limit=10)
        except Exception as exc:  # noqa: BLE001 - the test asserts on any error
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(database.query_history(hours=1, bucket_seconds=3600)) >= 1
    # No silent lost writes under contention: 8 workers x 50 inserts each.
    assert database.database_stats()["measurements"] == 8 * 50


def test_delete_history_never_vacuums(monkeypatch, database):
    """VACUUM was removed on purpose: on a slow SD card it starves the
    collector's writes. Inserts must still work fine after a delete."""
    database.insert_measurement({"co2": 500})
    statements = []
    original = database._write

    def spying_write(sql, params=()):
        statements.append(sql.strip().upper())
        return original(sql, params)

    monkeypatch.setattr(database, "_write", spying_write)
    assert database.delete_history() == 1
    assert not any(sql.startswith("VACUUM") for sql in statements)
    database.insert_measurement({"co2": 600})  # freed pages get reused
    assert database.get_latest_measurement()["co2"] == 600


def test_pending_commands_claimed_exactly_once_across_two_connections(tmp_path):
    import threading

    path = str(tmp_path / "claims.db")
    first = AirMonitorDatabase(path)
    second = AirMonitorDatabase(path)
    ids = [first.queue_command("display_full_refresh") for _ in range(20)]

    claimed = []
    claimed_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(db):
        barrier.wait()
        for _ in range(40):
            for row in db.claim_pending_commands():
                with claimed_lock:
                    claimed.append(row["id"])

    threads = [threading.Thread(target=worker, args=(db,)) for db in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(claimed) == sorted(ids)  # every command claimed exactly once
    first.close()
    second.close()


def test_corrupt_state_json_degrades_to_fallback(database):
    with database._lock:
        database._connection.execute(
            "INSERT INTO state(key, value, updated_at) VALUES('latest_weather', '{bad', 0)"
        )
    state = database.get_state("latest_weather")
    assert state["value"] is None  # not an exception


def test_hourly_stats_not_biased_by_null_gaps(database):
    """A metric that was flagged/offline half the window must keep its true
    average — weighting by whole-row counts used to halve it."""
    current_hour = (database._now() // 3600) * 3600
    old = current_hour - 95 * 86400
    hour_a = (old // 3600) * 3600
    hour_b = hour_a + 3600
    # Hour A: two real co2 samples of 600. Hour B: two rows with co2 NULL.
    _insert_raw(database, hour_a + 10, 600)
    _insert_raw(database, hour_a + 20, 600)
    _insert_raw(database, hour_b + 10, None, flagged=True)
    _insert_raw(database, hour_b + 20, None, flagged=True)
    while database.rollup_hourly():
        pass

    stats = database.query_stats_hourly(hour_a, hour_b + 3600)
    assert stats["sample_count"] == 4
    assert stats["co2"]["avg"] == 600.0  # not 300
    assert stats["co2"]["count"] == 2

    # Rows rolled before the count columns existed (count NULL) fall back to
    # sample_count — the old approximation, never a crash.
    with database._lock:
        database._connection.execute(
            "UPDATE measurements_hourly SET co2_count = NULL WHERE hour_ts = ?", (hour_a,)
        )
    stats = database.query_stats_hourly(hour_a, hour_b + 3600)
    assert stats["co2"]["avg"] == 600.0


def test_integrity_check_reports_ok_on_a_healthy_database(database):
    database.insert_measurement({"co2": 600})
    assert database.integrity_check() == []


def test_backup_is_a_faithful_readable_copy(database, tmp_path):
    for i in range(50):
        database.insert_measurement({"co2": 600 + i})
    heartbeats = []
    target = str(tmp_path / "copy.db")
    written = database.backup_to(target, progress=lambda: heartbeats.append(1))
    assert written > 0

    copy = AirMonitorDatabase(target)
    try:
        # The copy stands alone: full row count, latest value intact.
        assert copy.database_stats()["measurements"] == 50
        assert copy.get_latest_measurement()["co2"] == 649
        assert copy.integrity_check() == []
    finally:
        copy.close()
