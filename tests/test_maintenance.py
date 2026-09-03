"""Hourly rollup, nightly job, the collector watch, orphaned commands."""

import time as _time
from datetime import datetime

import pytest

from manager.maintenance import (
    COLLECTOR_RESTART_AFTER, COLLECTOR_SILENT, RESTART_COOLDOWN, CollectorWatch, Hourly, Nightly,
    fail_unclaimed, nightly,
)
from shared.db import Database

H = 3600
BASE = 1_788_436_800  # 12:00 UTC


@pytest.fixture
def tdb(tmp_config):
    clock = {"t": BASE}
    db = Database(tmp_config.paths.database, now=lambda: clock["t"])
    db.clock = clock
    yield db
    db.close()


def test_hourly_first_run_catches_up_then_one_hour_per_tick(tdb, log, db):
    for k in range(3):
        tdb.insert_raw(BASE - 3 * H + k * H + 60, {"co2": 600 + k})
    hourly = Hourly(tdb, log)
    first = hourly.tick(BASE + 5)
    assert first["rolled"] == 3
    assert hourly.tick(BASE + 30 * 60) is None                  # same hour: nothing
    tdb.insert_raw(BASE + 600, {"co2": 700})
    second = hourly.tick(BASE + H + 2)
    assert second["rolled"] == 1
    types = [e["type"] for e in db.recent_events()]
    assert types.count("rollup_catchup") == 1                   # only the first run is an event


def test_nightly_runs_prune_checkpoint_backup_in_order(tdb, tmp_config, log, db):
    retention = tmp_config.retention_days
    tdb.insert_raw(BASE - (retention.raw + 1) * 86400, {"co2": 1})
    tdb.insert_raw(BASE - 60, {"co2": 2})
    beats = []
    summary = nightly(tdb, log, tmp_config, BASE, heartbeat=lambda: beats.append(1))
    assert summary["pruned"]["raw"] == 1 and summary["backup_mb"] >= 0 and beats
    backup = tmp_config.paths.database.parent / "airstation.db.bak"
    assert backup.exists()
    event = db.recent_events()[0]
    assert event["type"] == "nightly" and event["details"]["pruned"]["raw"] == 1
    assert len(tdb.raw_between(0, 10**10)) == 1


def test_nightly_schedule_fires_at_0005_local(tdb, tmp_config, log, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    try:
        job = Nightly(tdb, log, tmp_config)
        at_0004 = datetime(2026, 9, 4, 0, 4).astimezone().timestamp()
        assert job.tick(at_0004) is None
        assert job.tick(at_0004 + 60) is not None
        assert job.tick(at_0004 + 90) is None
        assert job.last_run_at == int(at_0004 + 60)
    finally:
        monkeypatch.delenv("TZ")
        _time.tzset()


class _Spawner:
    def __init__(self):
        self.calls = []
        self.fail = False

    def __call__(self, argv, **kwargs):
        if self.fail:
            raise OSError("no sh")
        self.calls.append((argv, kwargs))


def test_collector_watch_event_at_60s_restart_at_300s_with_cooldown(log, db):
    spawner = _Spawner()
    watch = CollectorWatch(log, spawner=spawner)
    last_row = 1000.0
    watch.tick(1000 + 30, last_row)
    assert not any(e["type"] == "collector_silent" for e in db.recent_events())
    watch.tick(1000 + COLLECTOR_SILENT + 1, last_row)
    watch.tick(1000 + COLLECTOR_SILENT + 31, last_row)
    assert [e["type"] for e in db.recent_events()].count("collector_silent") == 1
    assert spawner.calls == []
    watch.tick(1000 + COLLECTOR_RESTART_AFTER, last_row)
    assert len(spawner.calls) == 1
    argv, kwargs = spawner.calls[0]
    assert argv == ["sh", "-c", "sleep 2; exec sudo systemctl restart airstation-collector"]
    assert kwargs == {"start_new_session": True}
    assert db.recent_events()[0]["type"] == "collector_restarted"
    watch.tick(1000 + COLLECTOR_RESTART_AFTER + 60, last_row)  # still silent: cooldown holds
    assert len(spawner.calls) == 1
    watch.tick(1000 + COLLECTOR_RESTART_AFTER + RESTART_COOLDOWN, last_row)
    assert len(spawner.calls) == 2


def test_collector_watch_resets_when_rows_return(log, db):
    watch = CollectorWatch(log, spawner=_Spawner())
    watch.tick(1000 + 70, 1000.0)
    assert watch.event_logged
    watch.tick(1000 + 80, 1000 + 75)
    assert watch.silent_since is None and not watch.event_logged
    watch.tick(1000 + 80 + 70, 1000 + 75)
    assert [e["type"] for e in db.recent_events()].count("collector_silent") == 2


def test_collector_watch_with_no_rows_ever_counts_from_now(log, db):
    watch = CollectorWatch(log, spawner=_Spawner())
    watch.tick(5000, None)
    watch.tick(5000 + COLLECTOR_SILENT, None)
    assert db.recent_events()[0]["type"] == "collector_silent"


def test_restart_spawn_failure_is_an_error_event(log, db):
    spawner = _Spawner()
    spawner.fail = True
    watch = CollectorWatch(log, spawner=spawner)
    watch.tick(1000 + COLLECTOR_RESTART_AFTER, 1000.0)
    event = db.recent_events()[0]
    assert event["type"] == "collector_restarted" and event["level"] == "error" and event["details"]["spawned"] is False


def test_fail_unclaimed_logs_once_per_batch(tdb, log, db):
    tdb.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    tdb.clock["t"] = BASE + 700
    tdb.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    assert fail_unclaimed(tdb, log, BASE + 700) == 1
    assert db.recent_events()[0]["type"] == "command_failed"
    assert fail_unclaimed(tdb, log, BASE + 701) == 0
