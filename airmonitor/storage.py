"""SQLite storage shared by the collector and the dashboard.

Both processes open the same database file. WAL mode makes that safe:
the collector writes measurements/state, the dashboard reads them and
queues commands back.

Tables (schema is unchanged from earlier versions, so an existing
database keeps working):

- measurements  one row per sensor sample (raw history for charts);
                the nullable `flags` column holds JSON for readings the
                quality guards rejected (raw value + reason) — flagged
                values are NOT in the metric columns, so averages and
                charts stay clean while nothing is lost
- state         small JSON documents keyed by name (latest snapshot,
                collector status, latest weather, ...)
- commands      command queue from the dashboard to the collector
- events        diagnostic event log shown on the dashboard
"""

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from airmonitor.validation import DEFAULT_MIN_VALID_CO2_PPM, clean_value

METRIC_FIELDS = ("co2", "temp", "humid", "pm1", "pm25", "pm4", "pm10", "tps")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at INTEGER NOT NULL,
    co2 INTEGER,
    temp REAL,
    humid REAL,
    pm1 REAL,
    pm25 REAL,
    pm4 REAL,
    pm10 REAL,
    tps REAL,
    flags TEXT
);
CREATE INDEX IF NOT EXISTS idx_measurements_recorded_at
    ON measurements(recorded_at);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS commands (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command TEXT NOT NULL,
    payload TEXT,
    status TEXT NOT NULL,
    result TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_commands_status_created_at
    ON commands(status, created_at);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_created_at
    ON events(created_at DESC, id DESC);
"""


def _to_iso(timestamp: Optional[int]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _from_json(text: Optional[str], fallback: Any = None) -> Any:
    if text is None:
        return fallback
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return fallback


class AirMonitorDatabase:
    """One instance = one open connection, safe to share between threads."""

    def __init__(self, path: str, min_valid_co2_ppm: int = DEFAULT_MIN_VALID_CO2_PPM):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._min_valid_co2_ppm = min_valid_co2_ppm
        self._lock = threading.Lock()
        # Last serialized JSON written per state key; lets set_state skip
        # writes when nothing changed (SD-card wear, see set_state).
        self._state_cache: Dict[str, str] = {}
        self._connection = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=10000")
        self._connection.executescript(_SCHEMA)
        self._migrate_schema()
        # Commands left "running" by a crashed collector will never finish.
        self._write(
            "UPDATE commands SET status='failed', "
            "result='\"Collector restarted before completing command\"', updated_at=? "
            "WHERE status='running'",
            (self._now(),),
        )

    def _migrate_schema(self) -> None:
        """Additive migrations so databases from older versions keep working."""
        columns = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(measurements)")
        }
        if "flags" not in columns:
            self._connection.execute("ALTER TABLE measurements ADD COLUMN flags TEXT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _now() -> int:
        return int(time.time())

    # One connection is shared between threads (the dashboard serves requests
    # from a thread pool), so every execute AND its fetch must happen under
    # the same lock hold — a cursor must never escape the lock.

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._connection.execute(sql, params).fetchall()

    def _write(self, sql: str, params: tuple = ()) -> Tuple[int, Optional[int]]:
        """Run a statement and return (rowcount, lastrowid)."""
        with self._lock:
            cursor = self._connection.execute(sql, params)
            return cursor.rowcount or 0, cursor.lastrowid

    # --- Measurements ------------------------------------------------------

    def insert_measurement(
        self,
        values: Dict[str, Optional[float]],
        flags: Optional[Dict[str, Any]] = None,
    ) -> None:
        cleaned = {
            field: clean_value(field, values.get(field), self._min_valid_co2_ppm)
            for field in METRIC_FIELDS
        }
        columns = ", ".join(METRIC_FIELDS)
        placeholders = ", ".join("?" for _ in METRIC_FIELDS)
        self._write(
            f"INSERT INTO measurements (recorded_at, {columns}, flags) "
            f"VALUES (?, {placeholders}, ?)",
            (
                self._now(),
                *[cleaned[field] for field in METRIC_FIELDS],
                json.dumps(flags) if flags else None,
            ),
        )

    def get_latest_measurement(self) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM measurements ORDER BY recorded_at DESC, id DESC LIMIT 1"
        )
        return self._measurement_to_dict(rows[0]) if rows else None

    def query_history(self, hours: int, bucket_seconds: int) -> List[Dict[str, Any]]:
        """Average measurements into time buckets for charting."""
        now = self._now()
        return self.query_history_range(now - max(hours, 1) * 3600, now, bucket_seconds)

    def query_history_range(
        self, start_ts: int, end_ts: int, bucket_seconds: int
    ) -> List[Dict[str, Any]]:
        """Bucketed averages for an arbitrary [start, end] window."""
        averages = ", ".join(f"AVG({field}) AS {field}" for field in METRIC_FIELDS)
        rows = self._query(
            f"""
            SELECT (recorded_at / ?) * ? AS bucket_ts, {averages}
            FROM measurements
            WHERE recorded_at >= ? AND recorded_at <= ?
            GROUP BY bucket_ts
            ORDER BY bucket_ts ASC
            """,
            (bucket_seconds, bucket_seconds, start_ts, end_ts),
        )
        return [self._measurement_to_dict(row, ts_column="bucket_ts") for row in rows]

    def query_stats(self, start_ts: int, end_ts: int) -> Dict[str, Any]:
        """Per-metric min/avg/max over raw samples in the window."""
        selects = ["COUNT(*) AS sample_count"]
        for field in METRIC_FIELDS:
            selects.extend(
                (
                    f"MIN({field}) AS {field}_min",
                    f"AVG({field}) AS {field}_avg",
                    f"MAX({field}) AS {field}_max",
                )
            )
        rows = self._query(
            f"SELECT {', '.join(selects)} FROM measurements "
            "WHERE recorded_at >= ? AND recorded_at <= ?",
            (start_ts, end_ts),
        )
        row = rows[0]
        stats: Dict[str, Any] = {"sample_count": row["sample_count"]}
        for field in METRIC_FIELDS:
            avg = row[f"{field}_avg"]
            stats[field] = {
                "min": row[f"{field}_min"],
                "avg": round(avg, 2) if avg is not None else None,
                "max": row[f"{field}_max"],
            }
        return stats

    def export_rows(self, start_ts: int, end_ts: int) -> List[Dict[str, Any]]:
        """Raw (uncleaned) samples for CSV export, oldest first, flags included."""
        rows = self._query(
            "SELECT * FROM measurements WHERE recorded_at >= ? AND recorded_at <= ? "
            "ORDER BY recorded_at ASC, id ASC",
            (start_ts, end_ts),
        )
        exported = []
        for row in rows:
            item: Dict[str, Any] = {"timestamp": _to_iso(row["recorded_at"])}
            for field in METRIC_FIELDS:
                item[field] = row[field]
            item["flags"] = row["flags"] if "flags" in row.keys() else None
            exported.append(item)
        return exported

    def get_recent_flagged(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Samples the quality guards flagged, newest first (diagnostics)."""
        rows = self._query(
            "SELECT id, recorded_at, flags FROM measurements "
            "WHERE flags IS NOT NULL ORDER BY recorded_at DESC, id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": row["id"],
                "timestamp": _to_iso(row["recorded_at"]),
                "flags": _from_json(row["flags"], {}),
            }
            for row in rows
        ]

    def delete_history(self) -> int:
        deleted, _ = self._write("DELETE FROM measurements")
        # VACUUM needs the database to itself; the collector writes every few
        # seconds, so reclaiming space is best-effort — the delete stands.
        try:
            self._write("VACUUM")
        except sqlite3.Error as exc:
            self.insert_event(
                "warning", "storage", "vacuum_skipped",
                f"VACUUM after history delete failed: {exc}",
            )
        return int(deleted)

    def prune(self, keep_measurements_days: int, keep_events_days: int) -> Dict[str, int]:
        """Delete old rows so the database does not grow forever. 0 = keep all."""
        result = {"measurements": 0, "events": 0}
        if keep_measurements_days > 0:
            cutoff = self._now() - keep_measurements_days * 86400
            deleted, _ = self._write("DELETE FROM measurements WHERE recorded_at < ?", (cutoff,))
            result["measurements"] = deleted
        if keep_events_days > 0:
            cutoff = self._now() - keep_events_days * 86400
            deleted, _ = self._write("DELETE FROM events WHERE created_at < ?", (cutoff,))
            result["events"] = deleted
        return result

    def _measurement_to_dict(self, row: sqlite3.Row, ts_column: str = "recorded_at") -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "timestamp": _to_iso(row[ts_column]),
            "timestamp_ts": row[ts_column],
        }
        for field in METRIC_FIELDS:
            result[field] = clean_value(field, row[field], self._min_valid_co2_ppm)
        if "flags" in row.keys():
            result["flags"] = _from_json(row["flags"])
        return result

    # --- State (small JSON documents) --------------------------------------

    def set_state(self, key: str, value: Any) -> None:
        # The collector republishes mostly-identical state documents all day;
        # skipping unchanged writes saves thousands of SD-card transactions.
        serialized = json.dumps(value)
        if self._state_cache.get(key) == serialized:
            return
        self._write(
            """
            INSERT INTO state(key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, serialized, self._now()),
        )
        self._state_cache[key] = serialized

    def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        rows = self._query(
            "SELECT value, updated_at FROM state WHERE key = ?", (key,)
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "value": _from_json(row["value"]),
            "updated_at": _to_iso(row["updated_at"]),
            "updated_at_ts": row["updated_at"],
        }

    # --- Command queue (dashboard -> collector) -----------------------------

    def queue_command(self, command: str, payload: Optional[Dict[str, Any]] = None) -> int:
        now = self._now()
        _, lastrowid = self._write(
            "INSERT INTO commands(command, payload, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (command, json.dumps(payload or {}), now, now),
        )
        return int(lastrowid)

    def claim_pending_commands(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Atomically mark pending commands as running and return them."""
        # The queue is almost always empty; a cheap read avoids opening a
        # write transaction every poll (every 2s -> real SD-card wear).
        pending = self._query(
            "SELECT 1 FROM commands WHERE status='pending' LIMIT 1"
        )
        if not pending:
            return []
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT id, command, payload FROM commands "
                    "WHERE status='pending' ORDER BY created_at ASC, id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
                if rows:
                    ids = [row["id"] for row in rows]
                    placeholders = ",".join("?" for _ in ids)
                    self._connection.execute(
                        f"UPDATE commands SET status='running', updated_at=? "
                        f"WHERE id IN ({placeholders})",
                        (self._now(), *ids),
                    )
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        return [
            {
                "id": row["id"],
                "command": row["command"],
                "payload": _from_json(row["payload"], {}),
            }
            for row in rows
        ]

    def complete_command(self, command_id: int, success: bool, result: Any) -> None:
        self._write(
            "UPDATE commands SET status=?, result=?, updated_at=? WHERE id=?",
            ("succeeded" if success else "failed", json.dumps(result), self._now(), command_id),
        )

    def get_recent_commands(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {
                "id": row["id"],
                "command": row["command"],
                "payload": _from_json(row["payload"], {}),
                "status": row["status"],
                "result": _from_json(row["result"]),
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _to_iso(row["updated_at"]),
            }
            for row in rows
        ]

    # --- Event log ----------------------------------------------------------

    def insert_event(
        self,
        level: str,
        source: str,
        event_type: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._write(
            "INSERT INTO events(level, source, event_type, message, details, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                str(level).strip().lower(),
                str(source).strip(),
                str(event_type).strip(),
                str(message),
                json.dumps(details or {}),
                self._now(),
            ),
        )

    def get_recent_events(
        self,
        limit: int = 100,
        *,
        source: Optional[str] = None,
        level: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if source:
            clauses.append("source = ?")
            params.append(source)
        if level:
            clauses.append("level = ?")
            params.append(level.strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._query(
            f"SELECT * FROM events {where} ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        return [
            {
                "id": row["id"],
                "level": row["level"],
                "source": row["source"],
                "event_type": row["event_type"],
                "message": row["message"],
                "details": _from_json(row["details"], {}),
                "created_at": _to_iso(row["created_at"]),
                "created_at_ts": row["created_at"],
            }
            for row in rows
        ]

    # --- Dashboard summary --------------------------------------------------

    def get_dashboard_summary(self) -> Dict[str, Any]:
        return {
            "latest_measurement": self.get_latest_measurement(),
            "latest_measurements": self.get_state("latest_measurements"),
            "latest_weather": self.get_state("latest_weather"),
            "latest_display_snapshot": self.get_state("latest_display_snapshot"),
            "collector_status": self.get_state("collector_status"),
            "scd41_last_calibration": self.get_state("scd41_last_calibration"),
            "recent_commands": self.get_recent_commands(),
            "recent_events": self.get_recent_events(limit=50),
        }
