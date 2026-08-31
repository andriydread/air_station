"""Tests for airmonitor.storage.AirMonitorDatabase."""

import threading

import pytest

from airmonitor.storage import AirMonitorDatabase


@pytest.fixture
def database(tmp_path):
    db = AirMonitorDatabase(str(tmp_path / "test.db"))
    yield db
    db.close()


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


def test_crashed_running_commands_failed_on_reopen(tmp_path):
    path = str(tmp_path / "crash.db")
    db = AirMonitorDatabase(path)
    db.queue_command("sps30_force_clean")
    db.claim_pending_commands()  # left 'running', simulating a crash
    db.close()
    reopened = AirMonitorDatabase(path)
    try:
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
