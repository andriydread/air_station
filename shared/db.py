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


    # --- hourly rollups --------------------------------------------------------

    CATCHUP_MAX_HOURS = 24 * 100  # one call never scans more than 100 days

    def rollup_hour(self, hour_ts: int) -> int:
        """Fold the raw rows of [hour_ts, hour_ts+3600) into one hourly row.

        Returns the number of raw rows (samples); 0 means nothing was written —
        an hour with no measurements gets no row.
        """
        hour_ts = int(hour_ts) // 3600 * 3600
        selects = ", ".join(
            f"MIN({m}) AS {m}_min, MAX({m}) AS {m}_max, AVG({m}) AS {m}_avg" for m in METRICS
        )
        row = self.query_one(
            f"SELECT COUNT(*) AS n, {selects} FROM raw_measurements "
            "WHERE recorded_at >= ? AND recorded_at < ?",
            (hour_ts, hour_ts + 3600),
        )
        samples = int(row["n"] or 0)
        if samples == 0:
            return 0
        columns = ["hour", "samples"]
        params: List[Any] = [hour_ts, samples]
        for m in METRICS:
            for stat in ("min", "max", "avg"):
                columns.append(f"{m}_{stat}")
                params.append(round_metric(m, row[f"{m}_{stat}"]))
        marks = ", ".join("?" for _ in columns)
        self.write(
            f"INSERT OR REPLACE INTO hourly_measurements ({', '.join(columns)}) VALUES ({marks})",
            params,
        )
        return samples

    def last_rolled_hour(self) -> Optional[int]:
        return self.query_one("SELECT MAX(hour) AS h FROM hourly_measurements")["h"]

    def rollup_catchup(self, now: int) -> Dict[str, Any]:
        """Roll up every finished hour not rolled yet, oldest first.

        Starts after the newest hourly row (or at the oldest raw row on a fresh
        database), stops before the current hour. Hours later than ``now``
        (clock skew) are never rolled; their count is reported as
        ``skipped_future`` so the caller can log it. ``remaining`` > 0 means
        the range was capped and another call is needed.
        """
        current_hour = int(now) // 3600 * 3600
        result: Dict[str, Any] = {"rolled": 0, "skipped_future": 0, "remaining": 0, "hours": []}
        oldest = self.raw_oldest_at()
        if oldest is None:
            return result
        last = self.last_rolled_hour()
        start = (oldest // 3600 * 3600) if last is None else last + 3600
        future = self.query_one(
            "SELECT COUNT(DISTINCT recorded_at / 3600) AS n FROM raw_measurements WHERE recorded_at >= ?",
            (current_hour + 3600,),
        )
        result["skipped_future"] = int(future["n"] or 0)
        if start >= current_hour:
            return result
        end = min(current_hour, start + self.CATCHUP_MAX_HOURS * 3600)
        hours = self.query(
            "SELECT DISTINCT (recorded_at / 3600) * 3600 AS h FROM raw_measurements "
            "WHERE recorded_at >= ? AND recorded_at < ? ORDER BY h",
            (start, end),
        )
        for row in hours:
            if self.rollup_hour(int(row["h"])) > 0:
                result["rolled"] += 1
                result["hours"].append(int(row["h"]))
        if end < current_hour:
            result["remaining"] = (current_hour - end) // 3600
        return result

    def hourly_between(self, start: int, end: int) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM hourly_measurements WHERE hour >= ? AND hour < ? ORDER BY hour",
            (start, end),
        )
        return [dict(row) for row in rows]

    def hourly_stats(self, start: int, end: int) -> Dict[str, Dict[str, Any]]:
        """Like raw_stats but from hourly rows; avg is sample-weighted."""
        selects = ", ".join(
            f"MIN({m}_min) AS {m}_min, MAX({m}_max) AS {m}_max, "
            f"SUM(CASE WHEN {m}_avg IS NOT NULL THEN {m}_avg * samples END) AS {m}_sum, "
            f"SUM(CASE WHEN {m}_avg IS NOT NULL THEN samples END) AS {m}_n"
            for m in METRICS
        )
        row = self.query_one(
            f"SELECT {selects} FROM hourly_measurements WHERE hour >= ? AND hour < ?",
            (start, end),
        )
        stats = {}
        for m in METRICS:
            n = int(row[f"{m}_n"] or 0)
            avg = (row[f"{m}_sum"] / n) if n else None
            stats[m] = {
                "min": round_metric(m, row[f"{m}_min"]),
                "max": round_metric(m, row[f"{m}_max"]),
                "avg": round_metric(m, avg),
                "n": n,
            }
        return stats


    # --- state documents -------------------------------------------------------

    ALWAYS_WRITE_STATE = ("display_data",)  # its updated_at is the Live tab's freshness

    def set_state(self, key: str, doc: Any) -> bool:
        """Store a small JSON document under ``key``; returns True when written.

        Unchanged documents are skipped (thousands of identical status writes
        a day would only wear the SD card) — except the keys in
        ``ALWAYS_WRITE_STATE``, whose timestamp must move every time.
        """
        serialized = to_json(doc)
        if key not in self.ALWAYS_WRITE_STATE and self._state_cache.get(key) == serialized:
            return False
        self.write(
            "INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, serialized, self.now()),
        )
        self._state_cache[key] = serialized
        return True

    def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT value, updated_at FROM state WHERE key = ?", (key,))
        if row is None:
            return None
        return {"value": from_json(row["value"]), "updated_at": int(row["updated_at"])}

    def state_updated_at(self, keys: Sequence[str]) -> Dict[str, Optional[int]]:
        keys = list(keys)
        if not keys:
            return {}
        marks = ", ".join("?" for _ in keys)
        rows = self.query(f"SELECT key, updated_at FROM state WHERE key IN ({marks})", keys)
        found = {row["key"]: int(row["updated_at"]) for row in rows}
        return {key: found.get(key) for key in keys}


    # --- commands ------------------------------------------------------------

    def queue_command(self, type_: str, from_whom: str, to_whom: str,
                      payload: Optional[Dict[str, Any]] = None) -> int:
        now = self.now()
        _, row_id = self.write(
            "INSERT INTO commands(created_at, updated_at, from_whom, to_whom, type, status, payload) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (now, now, from_whom, to_whom, type_, to_json(payload or {})),
        )
        return int(row_id)

    def claim_pending(self, to_whom: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Atomically move this app's oldest pending commands to ``running``."""
        # The queue is almost always empty: a cheap read first avoids a write
        # transaction every 2 s (real SD-card wear).
        if self.query_one(
            "SELECT 1 FROM commands WHERE to_whom = ? AND status = 'pending' LIMIT 1", (to_whom,)
        ) is None:
            return []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT id, type, payload, from_whom, created_at FROM commands "
                "WHERE to_whom = ? AND status = 'pending' ORDER BY created_at, id LIMIT ?",
                (to_whom, limit),
            ).fetchall()
            if rows:
                marks = ", ".join("?" for _ in rows)
                conn.execute(
                    f"UPDATE commands SET status = 'running', updated_at = ? WHERE id IN ({marks})",
                    (self.now(), *[row["id"] for row in rows]),
                )
        return [
            {"id": row["id"], "type": row["type"], "payload": from_json(row["payload"], {}),
             "from_whom": row["from_whom"], "created_at": row["created_at"]}
            for row in rows
        ]

    def complete_command(self, command_id: int, success: bool, result: Any) -> None:
        self.write(
            "UPDATE commands SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            ("success" if success else "fail", to_json(result), self.now(), command_id),
        )

    def fail_running(self, to_whom: str, reason: str) -> int:
        """At app start: rows a crash left ``running`` can never finish."""
        count, _ = self.write(
            "UPDATE commands SET status = 'fail', result = ?, updated_at = ? "
            "WHERE to_whom = ? AND status = 'running'",
            (to_json({"error": reason}), self.now(), to_whom),
        )
        return count

    def fail_unclaimed(self, older_than_s: int, now: Optional[int] = None) -> int:
        """Commands still ``pending`` after ``older_than_s`` were never picked up."""
        now = self.now() if now is None else int(now)
        count, _ = self.write(
            "UPDATE commands SET status = 'fail', result = ?, updated_at = ? "
            "WHERE status = 'pending' AND created_at <= ?",
            (to_json({"error": "not picked up"}), now, now - older_than_s),
        )
        return count

    def recent_commands(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM commands ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [
            {**dict(row), "payload": from_json(row["payload"], {}), "result": from_json(row["result"])}
            for row in rows
        ]

    def newest_command_id(self) -> int:
        return int(self.query_one("SELECT COALESCE(MAX(id), 0) AS i FROM commands")["i"])

    def prune_commands(self, before_ts: int) -> int:
        count, _ = self.write("DELETE FROM commands WHERE created_at < ?", (before_ts,))
        return count


    # --- events ------------------------------------------------------------------

    def insert_event(self, app: str, level: str, source: str, type_: str, message: str,
                     details: Optional[Dict[str, Any]] = None, ts: Optional[int] = None) -> int:
        _, row_id = self.write(
            "INSERT INTO events(ts, app, level, source, type, message, details) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.now() if ts is None else int(ts), app, level, source, type_, message,
             to_json(details or {})),
        )
        return int(row_id)

    def recent_events(self, limit: int = 100, app: Optional[str] = None,
                      level: Optional[str] = None, source: Optional[str] = None,
                      since_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Newest first; every filter is optional and they combine."""
        clauses, params = self._event_filters(app, level, source)
        if since_id is not None:
            clauses.append("id > ?")
            params.append(int(since_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.query(
            f"SELECT * FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit)
        )
        return [self._event_row(row) for row in rows]

    def events_between(self, start: int, end: int, app: Optional[str] = None,
                       level: Optional[str] = None, source: Optional[str] = None,
                       limit: int = 5000) -> List[Dict[str, Any]]:
        """Oldest first, start <= ts < end (for exports and the text of a range)."""
        clauses, params = self._event_filters(app, level, source)
        clauses = ["ts >= ?", "ts < ?", *clauses]
        rows = self.query(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY ts, id LIMIT ?",
            (start, end, *params, limit),
        )
        return [self._event_row(row) for row in rows]

    def newest_event_id(self) -> int:
        return int(self.query_one("SELECT COALESCE(MAX(id), 0) AS i FROM events")["i"])

    def count_events(self, type_: str, since_ts: int, app: Optional[str] = None) -> int:
        clauses, params = ["type = ?", "ts >= ?"], [type_, since_ts]
        if app is not None:
            clauses.append("app = ?")
            params.append(app)
        row = self.query_one(f"SELECT COUNT(*) AS n FROM events WHERE {' AND '.join(clauses)}", params)
        return int(row["n"])

    def latest_event(self, type_: str, app: Optional[str] = None) -> Optional[Dict[str, Any]]:
        clauses, params = ["type = ?"], [type_]
        if app is not None:
            clauses.append("app = ?")
            params.append(app)
        row = self.query_one(
            f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY ts DESC, id DESC LIMIT 1",
            params,
        )
        return self._event_row(row) if row is not None else None

    def prune_events(self, before_ts: int) -> int:
        count, _ = self.write("DELETE FROM events WHERE ts < ?", (before_ts,))
        return count

    @staticmethod
    def _event_filters(app, level, source) -> Tuple[List[str], List[Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        for column, value in (("app", app), ("level", level), ("source", source)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        return clauses, params

    @staticmethod
    def _event_row(row: sqlite3.Row) -> Dict[str, Any]:
        return {**dict(row), "details": from_json(row["details"], {})}


    # --- vitals --------------------------------------------------------------------

    # The documented vcgencmd get_throttled bits: 0-3 "now", 16-19 "since boot".
    THROTTLED_BITS = (1, 2, 4, 8, 1 << 16, 1 << 17, 1 << 18, 1 << 19)

    def insert_vitals(self, row: Dict[str, Any]) -> None:
        """One minute of machine health; ``recorded_at`` required, the rest optional."""
        columns = ("recorded_at", *VITALS_COLUMNS)
        marks = ", ".join("?" for _ in columns)
        params = [int(row["recorded_at"]), *(row.get(column) for column in VITALS_COLUMNS)]
        self.write(f"INSERT OR REPLACE INTO vitals ({', '.join(columns)}) VALUES ({marks})", params)

    def latest_vitals(self) -> Optional[Dict[str, Any]]:
        row = self.query_one("SELECT * FROM vitals ORDER BY recorded_at DESC LIMIT 1")
        return dict(row) if row is not None else None

    def vitals_between(self, start: int, end: int) -> List[Dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM vitals WHERE recorded_at >= ? AND recorded_at < ? ORDER BY recorded_at",
            (start, end),
        )
        return [dict(row) for row in rows]

    def vitals_bucketed(self, start: int, end: int, bucket_s: int) -> List[Dict[str, Any]]:
        """Per-bucket averages; ``throttled`` is the OR of the rows' bits (a set bit survives)."""
        bucket_s = max(1, int(bucket_s))
        averaged = [c for c in VITALS_COLUMNS if c != "throttled"]
        selects = ", ".join(f"AVG({c}) AS {c}" for c in averaged)
        or_bits = " + ".join(f"MAX(throttled & {bit})" for bit in self.THROTTLED_BITS)
        rows = self.query(
            f"SELECT (recorded_at / ?) * ? AS ts, {selects}, ({or_bits}) AS throttled FROM vitals "
            "WHERE recorded_at >= ? AND recorded_at < ? GROUP BY ts ORDER BY ts",
            (bucket_s, bucket_s, start, end),
        )
        out = []
        for row in rows:
            item: Dict[str, Any] = {"ts": int(row["ts"])}
            for c in averaged:
                item[c] = None if row[c] is None else round(float(row[c]), 2)
            item["throttled"] = None if row["throttled"] is None else int(row["throttled"])
            out.append(item)
        return out

    def prune_vitals(self, before_ts: int) -> int:
        count, _ = self.write("DELETE FROM vitals WHERE recorded_at < ?", (before_ts,))
        return count


    # --- maintenance ----------------------------------------------------------------

    def prune(self, now: int, retention) -> Dict[str, int]:
        """Delete rows older than their retention (days); hourly rows are never pruned.

        ``retention`` is the config's ``Retention`` (raw, vitals, events, commands).
        """
        day = 86400
        return {
            "raw": self.write("DELETE FROM raw_measurements WHERE recorded_at < ?",
                              (now - retention.raw * day,))[0],
            "vitals": self.prune_vitals(now - retention.vitals * day),
            "events": self.prune_events(now - retention.events * day),
            "commands": self.prune_commands(now - retention.commands * day),
        }

    def checkpoint(self) -> Dict[str, int]:
        """Fold the WAL side file into the main file and truncate it."""
        with self._lock:
            row = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return {"busy": int(row[0]), "log_pages": int(row[1]), "checkpointed": int(row[2])}

    def backup_to(self, target: str | Path, progress: Optional[Callable[[], None]] = None) -> int:
        """Consistent online copy of the whole database; returns bytes written.

        ``progress`` is called between page batches so a caller can keep its
        watchdog heartbeat flowing during a long copy.
        """
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".partial")

        def _step(_status, _remaining, _total):
            if progress is not None:
                progress()

        with self._lock:
            destination = sqlite3.connect(str(partial))
            try:
                self._connection.backup(destination, pages=2000, progress=_step)
            finally:
                destination.close()
        partial.replace(target)
        return target.stat().st_size

    def delete_history(self) -> Dict[str, int]:
        """The dashboard's Delete history: measurements and vitals go, the story stays."""
        counts = {
            "raw": self.query_one("SELECT COUNT(*) AS n FROM raw_measurements")["n"],
            "hourly": self.query_one("SELECT COUNT(*) AS n FROM hourly_measurements")["n"],
            "vitals": self.query_one("SELECT COUNT(*) AS n FROM vitals")["n"],
        }
        self.write_many([
            ("DELETE FROM raw_measurements", ()),
            ("DELETE FROM hourly_measurements", ()),
            ("DELETE FROM vitals", ()),
        ])
        return {key: int(value) for key, value in counts.items()}


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
