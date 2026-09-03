"""prune, checkpoint, backup, delete_history."""

import sqlite3
from pathlib import Path

from shared.db import Database

DAY = 86400
NOW = 100 * DAY


def _seed(db, retention):
    # one row just inside and one just outside each retention window
    db.insert_raw(NOW - retention.raw * DAY + 1, {"co2": 700})
    db.insert_raw(NOW - retention.raw * DAY - 1, {"co2": 600})
    db.insert_vitals({"recorded_at": NOW - retention.vitals * DAY + 1})
    db.insert_vitals({"recorded_at": NOW - retention.vitals * DAY - 1})
    db.insert_event("manager", "info", "app", "started", "x", ts=NOW - retention.events * DAY + 1)
    db.insert_event("manager", "info", "app", "started", "x", ts=NOW - retention.events * DAY - 1)
    db.rollup_hour(NOW - retention.raw * DAY - 1)  # an hourly row from old data


def test_prune_counts_per_table_and_never_touches_hourly(tmp_path, tmp_config):
    ticks = iter([NOW - tmp_config.retention_days.commands * DAY - 1,
                  NOW - tmp_config.retention_days.commands * DAY + 1, NOW, NOW, NOW])
    db = Database(tmp_path / "m.db", now=lambda: next(ticks))
    retention = tmp_config.retention_days
    db.queue_command("sps30_fan_clean", "dashboard", "collector")  # old
    db.queue_command("sps30_fan_clean", "dashboard", "collector")  # recent
    _seed(db, retention)
    assert db.prune(NOW, retention) == {"raw": 1, "vitals": 1, "events": 1, "commands": 1}
    assert len(db.raw_between(0, NOW)) == 1
    assert len(db.vitals_between(0, NOW)) == 1
    assert len(db.recent_events()) == 1
    assert len(db.recent_commands()) == 1
    assert len(db.hourly_between(0, NOW)) == 1
    db.close()


def test_checkpoint_truncates_the_wal(db):
    for i in range(500):
        db.insert_raw(i * 10, {"co2": 500 + i})
    wal = Path(str(db.path) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0
    result = db.checkpoint()
    assert result["busy"] == 0 and result["checkpointed"] == result["log_pages"]
    assert wal.stat().st_size == 0


def test_backup_is_a_complete_openable_copy(db, tmp_path):
    for i in range(50):
        db.insert_raw(i * 10, {"co2": 500 + i})
    db.set_state("k", {"v": 1})
    beats = []
    size = db.backup_to(tmp_path / "out" / "airstation.db.bak", progress=lambda: beats.append(1))
    target = tmp_path / "out" / "airstation.db.bak"
    assert target.exists() and size == target.stat().st_size and size > 0
    assert not (tmp_path / "out" / "airstation.db.bak.partial").exists()
    assert beats  # progress callback fired at least once
    copy = sqlite3.connect(str(target))
    assert copy.execute("SELECT COUNT(*) FROM raw_measurements").fetchone()[0] == 50
    assert copy.execute("SELECT value FROM state WHERE key='k'").fetchone()[0] == '{"v":1}'
    copy.close()


def test_backup_replaces_an_older_copy(db, tmp_path):
    target = tmp_path / "airstation.db.bak"
    db.insert_raw(10, {"co2": 1})
    db.backup_to(target)
    db.insert_raw(20, {"co2": 2})
    db.backup_to(target)
    copy = sqlite3.connect(str(target))
    assert copy.execute("SELECT COUNT(*) FROM raw_measurements").fetchone()[0] == 2
    copy.close()


def test_delete_history_leaves_the_story(db):
    db.insert_raw(3600, {"co2": 700})
    db.rollup_hour(3600)
    db.insert_vitals({"recorded_at": 3600})
    db.insert_event("manager", "info", "app", "started", "x")
    db.queue_command("reboot", "dashboard", "manager", {"confirmed": True})
    db.set_state("display_data", {"co2": 700})
    assert db.delete_history() == {"raw": 1, "hourly": 1, "vitals": 1}
    assert db.raw_between(0, 10**9) == [] and db.hourly_between(0, 10**9) == []
    assert db.latest_vitals() is None
    assert len(db.recent_events()) == 1 and len(db.recent_commands()) == 1
    assert db.get_state("display_data")["value"] == {"co2": 700}
