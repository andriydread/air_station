"""Start/stop context for the collector: reboot vs restart, clean vs killed.

The events table already said *that* the collector started; it never said
*why*. A CO2 spike that follows every start is a different problem from one
that follows a power loss, and unless the distinction is recorded next to
the data nobody can tell them apart a week later. Everything here is
best-effort: a missing /proc file or systemctl just leaves a field None.
"""

import logging
import subprocess
import time
from typing import Any, Dict, List, Optional

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
UPTIME_PATH = "/proc/uptime"

# A dashboard system command that completed this recently before the start
# is taken as what caused it.
COMMAND_TRIGGER_WINDOW_SECONDS = 15 * 60
TRIGGER_COMMANDS = ("system_reboot", "system_restart_collector")


def read_boot_id(path: str = BOOT_ID_PATH) -> Optional[str]:
    """The kernel's per-boot UUID: same value = same boot, no reboot happened."""
    try:
        with open(path, encoding="ascii") as handle:
            value = handle.read().strip()
        return value or None
    except OSError:
        return None


def read_system_uptime(path: str = UPTIME_PATH) -> Optional[float]:
    try:
        with open(path, encoding="ascii") as handle:
            return float(handle.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def systemd_unit_info(unit: str = "airmonitor", runner=subprocess.run) -> Dict[str, Any]:
    """NRestarts / Result of our own unit; {} when systemd is not around.

    `Result=watchdog` is the one value worth interpreting: it means systemd
    killed the previous instance for missing heartbeats.
    """
    try:
        completed = runner(
            ["systemctl", "show", unit, "--property=NRestarts,Result"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except Exception:  # noqa: BLE001 - absent systemctl, timeout, anything
        return {}
    info: Dict[str, Any] = {}
    for line in (completed.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "NRestarts":
            try:
                info["n_restarts"] = int(value)
            except ValueError:
                pass
        elif key == "Result":
            info["result"] = value
    return info


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown time"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def describe_start(
    *,
    boot_id: Optional[str],
    previous_boot_id: Optional[str],
    system_uptime: Optional[float],
    previous_status: Optional[Dict[str, Any]],
    recent_commands: List[Dict[str, Any]],
    unit_info: Dict[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Classify this start; returns {"level", "message", "details"}.

    `previous_status` is the stored `collector_status` document (with
    `value` and `updated_at_ts`) left by the previous instance. Its
    `running` flag is the clean/unclean tell: shutdown() always publishes
    running=False — even after a crash, via run()'s finally — so a document
    still saying running=True means the process was killed outright
    (watchdog SIGABRT, OOM, power loss).
    """
    now = time.time() if now is None else now
    value = (previous_status or {}).get("value") or {}
    previous_seen_ts = (previous_status or {}).get("updated_at_ts")

    rebooted: Optional[bool]
    if boot_id is None or previous_boot_id is None:
        rebooted = None
    else:
        rebooted = boot_id != previous_boot_id

    previous_clean: Optional[bool]
    if "running" in value:
        previous_clean = value["running"] is False
    else:
        previous_clean = None

    downtime = None if previous_seen_ts is None else max(0, int(now - previous_seen_ts))

    trigger = "unknown"
    trigger_command = None
    for command in recent_commands:
        if command.get("command") not in TRIGGER_COMMANDS:
            continue
        if command.get("status") != "succeeded":
            continue
        finished = command.get("updated_at_ts")
        if finished is None or now - finished > COMMAND_TRIGGER_WINDOW_SECONDS:
            continue
        trigger_command = command["command"]
        trigger = f"dashboard command {trigger_command}"
        break
    if trigger_command is None:
        if unit_info.get("result") == "watchdog":
            trigger = "systemd watchdog (heartbeats stopped)"
        elif previous_clean is False and rebooted:
            trigger = "power loss or hard reset (previous run never shut down)"
        elif previous_clean is False:
            trigger = "process killed (crash, watchdog or OOM)"
        elif previous_clean is True and rebooted:
            trigger = "orderly reboot"
        elif previous_clean is True:
            trigger = "service restart (deploy or manual)"

    if previous_status is None and previous_boot_id is None:
        headline = "Air monitor started (first start on record)"
    elif rebooted:
        headline = "Air monitor started after a Pi reboot"
    elif rebooted is False:
        headline = "Air monitor restarted without a reboot"
    else:
        headline = "Air monitor started"

    parts = []
    if system_uptime is not None:
        parts.append(f"system up {_format_duration(system_uptime)}")
    if downtime is not None:
        parts.append(f"station silent for {_format_duration(downtime)}")
    if previous_clean is True:
        reason = value.get("stop_reason")
        ended = f"stopped cleanly{f' on {reason}' if reason else ''}"
        parts.append(f"previous run {ended} after {_format_duration(value.get('uptime_seconds'))}")
    elif previous_clean is False:
        parts.append(
            f"previous run was killed after {_format_duration(value.get('uptime_seconds'))}"
        )
    parts.append(f"trigger: {trigger}")
    message = f"{headline} ({'; '.join(parts)})"

    details = {
        "boot_id": boot_id,
        "rebooted": rebooted,
        "system_uptime_seconds": None if system_uptime is None else int(system_uptime),
        "downtime_seconds": downtime,
        "previous_clean": previous_clean,
        "previous_uptime_seconds": value.get("uptime_seconds"),
        "previous_stop_reason": value.get("stop_reason"),
        "trigger": trigger,
        "trigger_command": trigger_command,
        "systemd": unit_info,
    }
    level = logging.WARNING if previous_clean is False else logging.INFO
    return {"level": level, "message": message, "details": details}
