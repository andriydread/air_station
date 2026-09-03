"""`make status` — one screen about the station, from systemd and the database.

    airstation-collector   active (running) since Tue 09:12 · last raw row 4 s ago
    airstation-manager     active (running) since Tue 09:12 · display_data 22 s ago · vitals 22 s ago
    airstation-dashboard   active (running) since Tue 09:12 · http://airstation.local:8080
    database 41.7 MB · backup Wed 00:05 · disk free 11.8 GB · log level debug · commit ace2686
    last events:
      Wed 09:41  warning  manager  wifi        internet_down   wan probe failed (3 in a row)

Read-only: it never creates the database (a missing file is reported, not
made). Times are the Pi's local zone, ages are relative to now.
"""

import argparse
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from shared.config import REPO_ROOT, Config
from shared.db import Database
from shared.events import git_commit

UNITS = ("airstation-collector", "airstation-manager", "airstation-dashboard")
EVENTS = 8


def age(ts: Optional[float], now: float) -> str:
    if ts is None:
        return "never"
    seconds = max(0, int(now - ts))
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def local_stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


def unit_state(unit: str, runner: Callable[..., Any]) -> str:
    """'active (running) since Tue 09:12', or what systemd says instead."""
    try:
        result = runner(["systemctl", "show", "-p", "ActiveState,SubState,ActiveEnterTimestamp", unit],
                        capture_output=True, text=True, timeout=5)
    except Exception as exc:  # no systemd here (the dev server), or it hangs
        return f"unknown ({exc.__class__.__name__})"
    if result.returncode != 0:
        return f"unknown (systemctl exit {result.returncode})"
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    state = fields.get("ActiveState", "unknown")
    sub = fields.get("SubState", "")
    since = fields.get("ActiveEnterTimestamp", "")
    text = f"{state} ({sub})" if sub else state
    parts = since.split()  # "Tue 2026-09-01 09:12:03 EEST"
    if len(parts) >= 3:
        text += f" since {parts[0]} {parts[2][:5]}"
    return text


def database_facts(config: Config, now: float) -> Dict[str, Any]:
    path = Path(config.paths.database)
    facts: Dict[str, Any] = {"exists": path.exists(), "path": path, "raw_at": None, "display_at": None,
                             "vitals_at": None, "size_mb": None, "events": []}
    if facts["exists"]:
        db = Database(path)
        try:
            facts["raw_at"] = db.latest_raw_at()
            facts["display_at"] = db.state_updated_at(["display_data"]).get("display_data")
            vitals = db.latest_vitals()
            facts["vitals_at"] = vitals["recorded_at"] if vitals else None
            facts["size_mb"] = db.size_mb()
            facts["events"] = db.recent_events(limit=EVENTS)
        finally:
            db.close()
    backup = Path(str(path) + ".bak")
    facts["backup_at"] = backup.stat().st_mtime if backup.exists() else None
    try:
        facts["disk_free_gb"] = round(shutil.disk_usage(path.parent if path.parent.exists() else "/").free / 1e9, 1)
    except OSError:
        facts["disk_free_gb"] = None
    return facts


def render(config: Config, runner: Callable[..., Any] = subprocess.run, now: Optional[float] = None,
           hostname: Optional[str] = None) -> str:
    now = time.time() if now is None else now
    host = hostname or socket.gethostname().split(".")[0]
    facts = database_facts(config, now)
    tails = {
        "airstation-collector": f"last raw row {age(facts['raw_at'], now)}",
        "airstation-manager": f"display_data {age(facts['display_at'], now)} · vitals {age(facts['vitals_at'], now)}",
        "airstation-dashboard": f"http://{host}.local:{config.dashboard.port}",
    }
    lines: List[str] = [f"{unit:<22} {unit_state(unit, runner)} · {tails[unit]}" for unit in UNITS]
    if facts["exists"]:
        database = f"database {facts['size_mb']} MB"
    else:
        database = f"database not created yet ({facts['path']})"
    backup = f"backup {local_stamp(facts['backup_at'])}" if facts["backup_at"] else "backup none"
    disk = f"disk free {facts['disk_free_gb']} GB" if facts["disk_free_gb"] is not None else "disk free ?"
    lines.append(f"{database} · {backup} · {disk} · log level {config.logging.level} · commit {git_commit(REPO_ROOT)}")
    lines.append("last events:")
    if not facts["events"]:
        lines.append("  (none)")
    for event in facts["events"]:
        lines.append(f"  {local_stamp(event['ts'])}  {event['level']:<7}  {event['app']:<9}  "
                     f"{event['source']:<10}  {event['type']:<15}  {event['message']}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="one screen about the station")
    parser.add_argument("--config", default=None, help="path to config.toml (default: repo root)")
    args = parser.parse_args(argv)
    config = Config.load(args.config)
    print(render(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
