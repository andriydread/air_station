"""The manager's housekeeping: the hourly rollup, the nightly prune → checkpoint →
backup, the watch over the collector, and failing commands nobody picked up.
"""

import subprocess
import time
from typing import Any, Callable, Dict, Optional

from shared import clock

NIGHTLY_HOUR = 0
NIGHTLY_MINUTE = 5
COLLECTOR_SILENT = 60.0          # no raw row for this long → event
COLLECTOR_RESTART_AFTER = 300.0  # … and after this long → restart its unit
RESTART_COOLDOWN = 600.0
UNCLAIMED_FAIL_AFTER = 600
RESTART_COLLECTOR = "sudo systemctl restart airstation-collector"
DEFER_SECONDS = 2


class Hourly:
    """Roll up the hour just finished at :00 (and every hour missed while the Pi was off)."""

    def __init__(self, db, log):
        self.db = db
        self.log = log
        self.last_hour: Optional[int] = None

    def tick(self, now: float) -> Optional[Dict[str, Any]]:
        current_hour = int(now) // 3600 * 3600
        if self.last_hour == current_hour:
            return None
        first_run = self.last_hour is None
        result = self.db.rollup_catchup(int(now))
        self.last_hour = current_hour
        self.log.debug("storage", "rollup", hour=current_hour - 3600, rolled=result["rolled"],
                       skipped_future=result["skipped_future"], remaining=result["remaining"])
        if first_run or result["rolled"] > 1 or result["skipped_future"] or result["remaining"]:
            self.log.event("info", "storage", "rollup_catchup",
                           f"rolled up {result['rolled']} hour(s)" + (
                               f", skipped {result['skipped_future']} future" if result["skipped_future"] else ""),
                           rolled=result["rolled"], skipped_future=result["skipped_future"],
                           remaining=result["remaining"], first_run=first_run)
        return result


def nightly(db, log, config, now: float, heartbeat: Optional[Callable[[], None]] = None,
            monotonic: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """prune → checkpoint → backup, one ``nightly`` event with the numbers."""
    started = monotonic()
    pruned = db.prune(int(now), config.retention_days)
    checkpoint = db.checkpoint()
    backup_path = str(config.paths.database) + ".bak"
    backup_bytes = db.backup_to(backup_path, progress=heartbeat)
    took = round((monotonic() - started) * 1000)
    summary = {"pruned": pruned, "checkpointed_pages": checkpoint["checkpointed"],
               "backup_mb": round(backup_bytes / 1_048_576, 1), "db_mb": db.size_mb(), "ms": took}
    log.event("info", "storage", "nightly",
              f"nightly done: pruned {sum(pruned.values())} rows, backup {summary['backup_mb']} MB",
              **summary)
    return summary


class Nightly:
    def __init__(self, db, log, config, heartbeat: Optional[Callable[[], None]] = None):
        self.db = db
        self.log = log
        self.config = config
        self.heartbeat = heartbeat
        self.schedule = clock.LocalSchedule(NIGHTLY_HOUR, NIGHTLY_MINUTE)
        self.last_run_at: Optional[int] = None
        self.last_backup_mb: Optional[float] = None

    def tick(self, now: float) -> Optional[Dict[str, Any]]:
        if not self.schedule.due(now):
            return None
        summary = nightly(self.db, self.log, self.config, now, self.heartbeat)
        self.last_run_at = int(now)
        self.last_backup_mb = summary["backup_mb"]
        return summary


class CollectorWatch:
    """No raw row for 60 s → event; 5 min → restart the collector's unit (10 min cooldown)."""

    def __init__(self, log, spawner: Callable = subprocess.Popen):
        self.log = log
        self.spawner = spawner
        self.silent_since: Optional[float] = None
        self.event_logged = False
        self.last_restart_at: Optional[int] = None
        self.restarts = 0

    def tick(self, now: float, latest_raw_at: Optional[float]) -> Dict[str, Any]:
        quiet_for = now - latest_raw_at if latest_raw_at is not None else None
        if quiet_for is None or quiet_for > COLLECTOR_SILENT:
            if self.silent_since is None:
                self.silent_since = latest_raw_at if latest_raw_at is not None else now
            silent_for = now - self.silent_since
            if not self.event_logged and silent_for >= COLLECTOR_SILENT:
                self.log.event("warning", "watch", "collector_silent",
                               f"no raw row for {int(silent_for)} s", silent_s=int(silent_for),
                               last_row_at=latest_raw_at)
                self.event_logged = True
            if silent_for >= COLLECTOR_RESTART_AFTER and (
                    self.last_restart_at is None or now - self.last_restart_at >= RESTART_COOLDOWN):
                self.restart(now, silent_for)
        else:
            if self.event_logged:
                self.log.info("watch", "collector_back", silent_s=int(now - self.silent_since))
            self.silent_since = None
            self.event_logged = False
        return {"silent": self.silent_since is not None, "silent_since": self.silent_since}

    def restart(self, now: float, silent_for: float) -> None:
        self.restarts += 1
        self.last_restart_at = int(now)
        argv = ["sh", "-c", f"sleep {DEFER_SECONDS}; exec {RESTART_COLLECTOR}"]
        try:
            self.spawner(argv, start_new_session=True)
            ok = True
        except Exception as exc:
            ok = False
            self.log.warning("watch", "restart_spawn_failed", error=str(exc))
        self.log.event("warning" if ok else "error", "watch", "collector_restarted",
                       f"collector silent for {int(silent_for)} s: restarting its service",
                       silent_s=int(silent_for), count=self.restarts, spawned=ok)


def fail_unclaimed(db, log, now: float) -> int:
    count = db.fail_unclaimed(UNCLAIMED_FAIL_AFTER, int(now))
    if count:
        log.event("warning", "app", "command_failed",
                  f"{count} command(s) not picked up for {UNCLAIMED_FAIL_AFTER // 60} minutes",
                  count=count, reason="not picked up")
    return count
