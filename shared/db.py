"""The one SQLite file all three apps share — schema and table helpers.

One ``Database`` per process; WAL mode so readers never block the writer;
every write inside ``BEGIN IMMEDIATE`` under a process-local lock; every app
creates the tables if they are missing (no owner, first one wins). No
migrations: the station is installed fresh on an empty file.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

METRICS: Tuple[str, ...] = (
    "co2", "co2_temp", "co2_humid", "temp", "humid",
    "pm1", "pm25", "pm10", "tps", "nc05", "nc1", "nc25",
)

VITALS_COLUMNS: Tuple[str, ...] = (
    "cpu_temp", "load", "mem_free", "disk_free", "db_size", "wifi_rssi", "wifi_link",
    "lan_ms", "wan_ms", "throttled", "uptime", "collector_lag",
)

TABLES: Tuple[str, ...] = (
    "raw_measurements", "hourly_measurements", "vitals", "events", "commands", "state",
)

_HOURLY_STAT_COLUMNS = ", ".join(
    f"{metric}_{stat} REAL" for metric in METRICS for stat in ("min", "max", "avg")
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS raw_measurements (
    recorded_at INTEGER PRIMARY KEY,
    co2 INTEGER, co2_temp REAL, co2_humid REAL,
    temp REAL, humid REAL,
    pm1 REAL, pm25 REAL, pm10 REAL, tps REAL,
    nc05 REAL, nc1 REAL, nc25 REAL
);

CREATE TABLE IF NOT EXISTS hourly_measurements (
    hour INTEGER PRIMARY KEY,
    samples INTEGER NOT NULL,
    {_HOURLY_STAT_COLUMNS}
);

CREATE TABLE IF NOT EXISTS vitals (
    recorded_at INTEGER PRIMARY KEY,
    cpu_temp REAL, load REAL, mem_free INTEGER, disk_free INTEGER, db_size REAL,
    wifi_rssi INTEGER, wifi_link REAL, lan_ms REAL, wan_ms REAL,
    throttled INTEGER, uptime INTEGER, collector_lag INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    app TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC, id DESC);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    from_whom TEXT NOT NULL,
    to_whom TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_commands_status ON commands(status, created_at);
CREATE INDEX IF NOT EXISTS idx_commands_target ON commands(to_whom, status);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class Database:
    """One connection per process, WAL, 5 s busy timeout, writes serialised."""

    def __init__(self, path: str | Path, now: Callable[[], float] = time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=5.0, check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._connection.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._connection.executescript(SCHEMA)
        self._state_cache: Dict[str, str] = {}

    # --- plumbing ------------------------------------------------------------

    def now(self) -> int:
        return int(self._now())

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> Optional[sqlite3.Row]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def write(self, sql: str, params: Sequence[Any] = ()) -> Tuple[int, Optional[int]]:
        """One statement in its own IMMEDIATE transaction → (rowcount, lastrowid)."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._connection.execute(sql, tuple(params))
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            return cursor.rowcount, cursor.lastrowid

    def write_many(self, statements: Iterable[Tuple[str, Sequence[Any]]]) -> None:
        """Several statements in ONE transaction (all or nothing)."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                for sql, params in statements:
                    self._connection.execute(sql, tuple(params))
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise

    def transaction(self):
        """``with db.transaction() as conn:`` for read-modify-write sequences."""
        return _Transaction(self)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def size_mb(self) -> float:
        """Main file + WAL, in MB with one decimal (what the operator sees)."""
        total = 0
        for suffix in ("", "-wal"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return round(total / 1_048_576, 1)

    def tables(self) -> List[str]:
        rows = self.query("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [row["name"] for row in rows if not row["name"].startswith("sqlite_")]

    def journal_mode(self) -> str:
        return self.query_one("PRAGMA journal_mode")[0]


class _Transaction:
    def __init__(self, db: Database):
        self._db = db

    def __enter__(self) -> sqlite3.Connection:
        self._db._lock.acquire()
        self._db._connection.execute("BEGIN IMMEDIATE")
        return self._db._connection

    def __exit__(self, exc_type, _exc, _tb) -> bool:
        try:
            if exc_type is None:
                self._db._connection.execute("COMMIT")
            else:
                self._db._connection.execute("ROLLBACK")
        finally:
            self._db._lock.release()
        return False


def to_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def from_json(text: Optional[str], fallback: Any = None) -> Any:
    if text is None:
        return fallback
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return fallback
