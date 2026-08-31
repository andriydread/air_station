"""Data-quality guard tests: rate guard, flag storage, cross-check."""

from airmonitor.quality import CrossCheck, RateGuard
from airmonitor.storage import AirMonitorDatabase


class StubEvents:
    def __init__(self):
        self.entries = []

    def log(self, level, source, event_type, message, details=None):
        self.entries.append((event_type, details or {}))

    def types(self):
        return [event_type for (event_type, _d) in self.entries]


# --- RateGuard --------------------------------------------------------------


def test_first_sample_always_accepted():
    guard = RateGuard(StubEvents())
    accepted, flags = guard.filter({"co2": 5000.0, "temp": 30.0})
    assert accepted == {"co2": 5000.0, "temp": 30.0}
    assert flags == {}


def test_plausible_drift_accepted():
    guard = RateGuard(StubEvents())
    guard.filter({"co2": 600.0})
    accepted, flags = guard.filter({"co2": 620.0})
    assert accepted == {"co2": 620.0}
    assert flags == {}


def test_implausible_jump_flagged_with_reason():
    events = StubEvents()
    guard = RateGuard(events)
    guard.filter({"co2": 600.0})
    accepted, flags = guard.filter({"co2": 4000.0})  # +3400 in ~0s
    assert "co2" not in accepted
    assert flags["co2"]["value"] == 4000.0
    assert "jumped" in flags["co2"]["reason"]
    assert "sample_flagged" in events.types()


def test_step_change_flags_exactly_one_sample():
    """A real event (window opened) must not be censored — the flagged value
    becomes the new baseline, so the very next reading is accepted."""
    events = StubEvents()
    guard = RateGuard(events)
    guard.filter({"co2": 2000.0})
    _accepted, flags = guard.filter({"co2": 450.0})  # window opened
    assert "co2" in flags
    accepted, flags = guard.filter({"co2": 452.0})
    assert accepted == {"co2": 452.0}
    assert flags == {}
    assert "sample_flag_cleared" in events.types()


def test_unknown_metric_passes_through():
    guard = RateGuard(StubEvents())
    guard.filter({"mystery": 1.0})
    accepted, flags = guard.filter({"mystery": 99999.0})
    assert accepted == {"mystery": 99999.0}
    assert flags == {}


def test_flag_event_spam_suppressed():
    events = StubEvents()
    guard = RateGuard(events, limits={"co2": 0.001})
    guard.filter({"co2": 0.0})
    for i in range(12):
        guard.filter({"co2": 1000.0 * (i + 1) * (-1) ** i})
    # first flag + every 6th, not one event per sample
    assert events.types().count("sample_flagged") == 3


# --- Flag storage -----------------------------------------------------------


def test_flags_persist_and_are_queryable(tmp_path):
    db = AirMonitorDatabase(str(tmp_path / "q.db"))
    try:
        db.insert_measurement(
            {"temp": 21.0},
            flags={"co2": {"value": 4000.0, "reason": "jumped +3400.00 in 10s"}},
        )
        db.insert_measurement({"temp": 21.1, "co2": 600})

        latest = db.get_latest_measurement()
        assert latest["flags"] is None

        flagged = db.get_recent_flagged()
        assert len(flagged) == 1
        assert flagged[0]["flags"]["co2"]["value"] == 4000.0

        # flagged metric is NULL in the row, so averages ignore it
        rows = db.query_history(hours=1, bucket_seconds=3600)
        assert rows[0]["co2"] == 600
    finally:
        db.close()


def test_flags_column_added_to_preexisting_database(tmp_path):
    """A database created before the flags column must be migrated in place."""
    import sqlite3

    path = str(tmp_path / "old.db")
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE measurements (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "recorded_at INTEGER NOT NULL, co2 INTEGER, temp REAL, humid REAL, "
        "pm1 REAL, pm25 REAL, pm4 REAL, pm10 REAL, tps REAL)"
    )
    connection.execute(
        "INSERT INTO measurements (recorded_at, co2) VALUES (strftime('%s','now'), 700)"
    )
    connection.commit()
    connection.close()

    db = AirMonitorDatabase(path)
    try:
        assert db.get_latest_measurement()["co2"] == 700
        db.insert_measurement({"co2": 710}, flags={"temp": {"value": 99.0, "reason": "x"}})
        assert len(db.get_recent_flagged()) == 1
    finally:
        db.close()


# --- CrossCheck -------------------------------------------------------------


def test_cross_check_reports_sustained_disagreement_once():
    events = StubEvents()
    check = CrossCheck(events, temp_delta=4.0, after_samples=3)
    for _ in range(5):
        check.compare(21.0, 50.0, 30.0, 52.0)  # 9C apart
    assert events.types().count("sensor_disagreement") == 1


def test_cross_check_recovers_and_resets():
    events = StubEvents()
    check = CrossCheck(events, temp_delta=4.0, after_samples=2)
    check.compare(21.0, 50.0, 30.0, 52.0)
    check.compare(21.0, 50.0, 30.0, 52.0)
    assert "sensor_disagreement" in events.types()
    check.compare(21.0, 50.0, 22.0, 52.0)  # agreement again
    assert "sensor_agreement_restored" in events.types()
    assert check.streak == 0


def test_cross_check_ignores_missing_values():
    events = StubEvents()
    check = CrossCheck(events, after_samples=1)
    check.compare(None, 50.0, 30.0, 52.0)
    check.compare(21.0, 50.0, None, None)
    assert events.entries == []


def test_transient_disagreement_not_reported():
    events = StubEvents()
    check = CrossCheck(events, temp_delta=4.0, after_samples=5)
    for _ in range(4):
        check.compare(21.0, 50.0, 30.0, 52.0)
    check.compare(21.0, 50.0, 21.5, 52.0)
    assert events.entries == []
