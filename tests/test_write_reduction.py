"""B2 regression tests: the collector must not rewrite unchanged data.

The Pi runs off an SD card; every avoided write transaction extends its
life. These tests count actual write statements through a spy on the
database's _write helper.
"""

import pytest

from airmonitor.config import Config
from airmonitor.storage import AirMonitorDatabase


@pytest.fixture
def database(tmp_path):
    db = AirMonitorDatabase(str(tmp_path / "test.db"))
    yield db
    db.close()


def _spy_writes(monkeypatch, database):
    calls = []
    original = database._write

    def spying_write(sql, params=()):
        calls.append(sql.strip().split()[0].upper())
        return original(sql, params)

    monkeypatch.setattr(database, "_write", spying_write)
    return calls


def test_set_state_skips_unchanged_value(monkeypatch, database):
    calls = _spy_writes(monkeypatch, database)
    database.set_state("collector_status", {"running": True, "uptime_seconds": 60})
    database.set_state("collector_status", {"running": True, "uptime_seconds": 60})
    database.set_state("collector_status", {"running": True, "uptime_seconds": 60})
    assert calls.count("INSERT") == 1
    database.set_state("collector_status", {"running": True, "uptime_seconds": 120})
    assert calls.count("INSERT") == 2


def test_empty_command_queue_claims_without_write(monkeypatch, database):
    calls = _spy_writes(monkeypatch, database)
    assert database.claim_pending_commands() == []
    assert database.claim_pending_commands() == []
    assert calls == []  # no write statements at all on an empty queue


def test_claim_still_works_with_pending_commands(database):
    command_id = database.queue_command("display_full_refresh")
    claimed = database.claim_pending_commands()
    assert [c["id"] for c in claimed] == [command_id]


def test_render_skips_snapshot_write_when_only_timestamp_moves(monkeypatch, tmp_path):
    from main import AirMonitor

    config = Config(
        database_path=str(tmp_path / "m.db"),
        log_file=str(tmp_path / "m.log"),
    )
    monitor = AirMonitor(config)
    try:
        calls = []
        original = monitor.database.set_state

        def spying_set_state(key, value):
            calls.append(key)
            return original(key, value)

        monkeypatch.setattr(monitor.database, "set_state", spying_set_state)

        snapshot_a = {"co2": 600, "temp": 21.0, "timestamp": "2026-08-31T10:00:00+00:00"}
        snapshot_b = {"co2": 600, "temp": 21.0, "timestamp": "2026-08-31T10:01:00+00:00"}
        snapshot_c = {"co2": 650, "temp": 21.0, "timestamp": "2026-08-31T10:02:00+00:00"}

        monitor._render(dict(snapshot_a), full_refresh=False)
        monitor._render(dict(snapshot_b), full_refresh=False)  # timestamp-only change
        assert calls.count("latest_display_snapshot") == 1
        monitor._render(dict(snapshot_c), full_refresh=False)  # real value change
        assert calls.count("latest_display_snapshot") == 2
        monitor._render(dict(snapshot_c), full_refresh=True)  # mode change counts too
        assert calls.count("latest_display_snapshot") == 3
    finally:
        monitor.database.close()
