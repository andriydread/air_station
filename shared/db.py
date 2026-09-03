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


    # --- raw measurements ----------------------------------------------------

    def insert_raw(self, recorded_at: int, values: Dict[str, Any]) -> None:
        """One beat. Missing or None metrics are stored as NULL; same second replaces."""
        columns = ", ".join(("recorded_at", *METRICS))
        marks = ", ".join("?" for _ in range(len(METRICS) + 1))
        params = [int(recorded_at), *(values.get(metric) for metric in METRICS)]
        self.write(f"INSERT OR REPLACE INTO raw_measurements ({columns}) VALUES ({marks})", params)

    def latest_raw_at(self) -> Optional[int]:
        return self.query_one("SELECT MAX(recorded_at) AS t FROM raw_measurements")["t"]

    def raw_oldest_at(self) -> Optional[int]:
        return self.query_one("SELECT MIN(recorded_at) AS t FROM raw_measurements")["t"]

    def raw_between(self, start: int, end: int) -> List[Dict[str, Any]]:
        """Rows with start <= recorded_at < end, oldest first."""
        rows = self.query(
            "SELECT * FROM raw_measurements WHERE recorded_at >= ? AND recorded_at < ? "
            "ORDER BY recorded_at",
            (start, end),
        )
        return [dict(row) for row in rows]

    def minute_average(self, now: int, window: int = 60) -> Dict[str, Dict[str, Any]]:
        """Averages over recorded_at in (now-window, now]; NULLs do not count."""
        selects = ", ".join(f"AVG({m}) AS {m}_avg, COUNT({m}) AS {m}_n" for m in METRICS)
        row = self.query_one(
            f"SELECT {selects} FROM raw_measurements WHERE recorded_at > ? AND recorded_at <= ?",
            (now - window, now),
        )
        values = {m: round_metric(m, row[f"{m}_avg"]) for m in METRICS}
        samples = {m: int(row[f"{m}_n"] or 0) for m in METRICS}
        return {"values": values, "samples": samples}

    def raw_bucketed(self, start: int, end: int, bucket_s: int) -> List[Dict[str, Any]]:
        """Per-bucket averages for charts: [{"ts": bucket_start, metric: avg|None, ...}]."""
        bucket_s = max(1, int(bucket_s))
        selects = ", ".join(f"AVG({m}) AS {m}" for m in METRICS)
        rows = self.query(
            f"SELECT (recorded_at / ?) * ? AS ts, {selects} FROM raw_measurements "
            "WHERE recorded_at >= ? AND recorded_at < ? GROUP BY ts ORDER BY ts",
            (bucket_s, bucket_s, start, end),
        )
        return [
            {"ts": int(row["ts"]), **{m: round_metric(m, row[m]) for m in METRICS}}
            for row in rows
        ]

    def raw_stats(self, start: int, end: int) -> Dict[str, Dict[str, Any]]:
        """{metric: {min, max, avg, n}} over start <= recorded_at < end."""
        selects = ", ".join(
            f"MIN({m}) AS {m}_min, MAX({m}) AS {m}_max, AVG({m}) AS {m}_avg, COUNT({m}) AS {m}_n"
            for m in METRICS
        )
        row = self.query_one(
            f"SELECT {selects} FROM raw_measurements WHERE recorded_at >= ? AND recorded_at < ?",
            (start, end),
        )
        return {
            m: {
                "min": round_metric(m, row[f"{m}_min"]),
                "max": round_metric(m, row[f"{m}_max"]),
                "avg": round_metric(m, row[f"{m}_avg"]),
                "n": int(row[f"{m}_n"] or 0),
            }
            for m in METRICS
        }


def round_metric(metric: str, value: Any) -> Any:
    """CO2 is a whole ppm, particle size keeps 3 decimals, the rest 2."""
    if value is None:
        return None
    if metric == "co2":
        return int(round(value))
    if metric == "tps":
        return round(float(value), 3)
    return round(float(value), 2)


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
