"""Database opening: pragmas, the six tables, write transactions."""

import sqlite3

import pytest

from shared.db import METRICS, TABLES, Database


def test_fresh_file_gets_exactly_the_six_tables(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.tables() == sorted(TABLES)
    columns = [row["name"] for row in db.query("PRAGMA table_info(raw_measurements)")]
    assert columns == ["recorded_at", *METRICS]
    hourly = [row["name"] for row in db.query("PRAGMA table_info(hourly_measurements)")]
    assert len(hourly) == 2 + 3 * len(METRICS)
    assert "co2_avg" in hourly and "nc25_max" in hourly
    db.close()


def test_wal_mode_and_busy_timeout(tmp_path):
    db = Database(tmp_path / "a.db")
    assert db.journal_mode() == "wal"
    assert db.query_one("PRAGMA busy_timeout")[0] == 5000
    assert db.query_one("PRAGMA synchronous")[0] == 1  # NORMAL
    db.close()


def test_two_connections_on_one_file_both_work(tmp_path):
    first = Database(tmp_path / "shared.db")
    second = Database(tmp_path / "shared.db")  # re-runs CREATE IF NOT EXISTS harmlessly
    first.write("INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)", ("k", "1", 1))
    assert second.query_one("SELECT value FROM state WHERE key='k'")["value"] == "1"
    first.close()
    second.close()


def test_write_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "a.db")
    db.write("INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)", ("k", "1", 1))
    with pytest.raises(sqlite3.IntegrityError):
        db.write_many([
            ("INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)", ("j", "2", 2)),
            ("INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)", ("k", "3", 3)),  # duplicate
        ])
    assert db.query_one("SELECT COUNT(*) AS n FROM state")["n"] == 1
    # the connection is usable afterwards
    db.write("INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)", ("j", "2", 2))
    assert db.query_one("SELECT COUNT(*) AS n FROM state")["n"] == 2
    db.close()


def test_transaction_context_commits_and_rolls_back(tmp_path):
    db = Database(tmp_path / "a.db")
    with db.transaction() as conn:
        conn.execute("INSERT INTO state(key, value, updated_at) VALUES ('a', '1', 1)")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("INSERT INTO state(key, value, updated_at) VALUES ('b', '1', 1)")
            raise RuntimeError("boom")
    keys = [row["key"] for row in db.query("SELECT key FROM state")]
    assert keys == ["a"]
    db.close()


def test_now_uses_the_injected_clock_and_size_is_reported(tmp_path):
    db = Database(tmp_path / "a.db", now=lambda: 1234.9)
    assert db.now() == 1234
    assert db.size_mb() >= 0.0
    db.close()
