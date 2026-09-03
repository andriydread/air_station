"""The Pi's own health, once a minute: power bits, temperature, load, memory,
disk, database size, Wi-Fi signal, uptime, collector lag → one ``vitals`` row.

Power: ``vcgencmd get_throttled`` reports what is happening now (bits 0-3)
and what has happened since boot (bits 16-19). The row stores the raw
value; the glyph and the pill use only the "now" half. Threshold events
(``cpu_hot``, ``disk_low``, ``memory_low``) fire once per episode.
"""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

CPU_HOT_C = 75.0
DISK_LOW_MB = 500
MEM_LOW_MB = 50

# Bit positions from the Raspberry Pi firmware documentation.
FLAG_BITS = {
    "undervoltage_now": 0,
    "freq_capped_now": 1,
    "throttled_now": 2,
    "soft_temp_limit_now": 3,
    "undervoltage_since_boot": 16,
    "freq_capped_since_boot": 17,
    "throttled_since_boot": 18,
    "soft_temp_limit_since_boot": 19,
}


@dataclass
class Sources:
    """Where the numbers come from; tests point these at temp files."""
    thermal: str = "/sys/class/thermal/thermal_zone0/temp"
    loadavg: str = "/proc/loadavg"
    meminfo: str = "/proc/meminfo"
    uptime: str = "/proc/uptime"
    wireless: str = "/proc/net/wireless"
    data_dir: str = "."
    interface: str = "wlan0"


def parse_throttled(text: str) -> int:
    """``throttled=0x50005`` → 0x50005."""
    return int(text.strip().split("=", 1)[-1], 16)


def flag_names(raw: Optional[int], since_boot: bool = False) -> List[str]:
    if raw is None:
        return []
    suffix = "_since_boot" if since_boot else "_now"
    return [name.removesuffix(suffix) for name, bit in FLAG_BITS.items()
            if name.endswith(suffix) and raw & (1 << bit)]


def _read_text(path: str) -> Optional[str]:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def read_cpu_temp(path: str) -> Optional[float]:
    text = _read_text(path)
    try:
        return round(int(text.strip()) / 1000, 1) if text else None
    except ValueError:
        return None


def read_load(path: str) -> Optional[float]:
    text = _read_text(path)
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def read_mem_free_mb(path: str) -> Optional[int]:
    text = _read_text(path)
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("MemAvailable:"):
            try:
                return int(line.split()[1]) // 1024
            except (ValueError, IndexError):
                return None
    return None


def read_disk_free_mb(directory: str) -> Optional[int]:
    try:
        stat = os.statvfs(directory)
    except OSError:
        return None
    return int(stat.f_bavail * stat.f_frsize // 1_048_576)


def read_uptime(path: str) -> Optional[int]:
    text = _read_text(path)
    try:
        return int(float(text.split()[0])) if text else None
    except (ValueError, IndexError):
        return None


def read_rssi(path: str, interface: str) -> Optional[int]:
    """Signal level in dBm from /proc/net/wireless (no tool needed)."""
    text = _read_text(path)
    if not text:
        return None
    for line in text.splitlines()[2:]:
        parts = line.split()
        if parts and parts[0].rstrip(":") == interface and len(parts) >= 4:
            try:
                return int(float(parts[3].rstrip(".")))
            except ValueError:
                return None
    return None


def read_link_mbps(runner: Callable, interface: str) -> Optional[float]:
    """tx bitrate from ``iw dev wlan0 link``; None when iw is missing or not associated."""
    try:
        result = runner(["iw", "dev", interface, "link"], capture_output=True, text=True,
                        timeout=5, check=False)
    except Exception:
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("tx bitrate:"):
            try:
                return float(line.split(":", 1)[1].split()[0])
            except (ValueError, IndexError):
                return None
    return None


def read_throttled(runner: Callable) -> Optional[int]:
    try:
        result = runner(["vcgencmd", "get_throttled"], capture_output=True, text=True,
                        timeout=5, check=False)
        if getattr(result, "returncode", 1) != 0:
            return None
        return parse_throttled(result.stdout)
    except Exception:
        return None


class Machine:
    def __init__(self, db, log, network=None, runner: Callable = subprocess.run,
                 sources: Optional[Sources] = None):
        self.db = db
        self.log = log
        self.network = network
        self.runner = runner
        self.sources = sources or Sources()
        self.raw_throttled: Optional[int] = None
        self._last_now: Optional[List[str]] = None
        self._episodes: Dict[str, bool] = {"cpu_hot": False, "disk_low": False, "memory_low": False}
        self.last_row: Optional[Dict[str, Any]] = None
        self.storage_failures = 0

    def read_row(self, now: float) -> Dict[str, Any]:
        s = self.sources
        latest_raw = self.db.latest_raw_at()
        self.raw_throttled = read_throttled(self.runner)
        return {
            "recorded_at": int(now),
            "cpu_temp": read_cpu_temp(s.thermal),
            "load": read_load(s.loadavg),
            "mem_free": read_mem_free_mb(s.meminfo),
            "disk_free": read_disk_free_mb(s.data_dir),
            "db_size": self.db.size_mb(),
            "wifi_rssi": read_rssi(s.wireless, s.interface),
            "wifi_link": read_link_mbps(self.runner, s.interface),
            "lan_ms": getattr(self.network, "last_lan_ms", None),
            "wan_ms": getattr(self.network, "last_wan_ms", None),
            "throttled": self.raw_throttled,
            "uptime": read_uptime(s.uptime),
            "collector_lag": int(now - latest_raw) if latest_raw is not None else None,
        }

    def tick(self, now: float) -> Dict[str, Any]:
        row = self.read_row(now)
        try:
            self.db.insert_vitals(row)
        except Exception:
            self.storage_failures += 1
            self.log.exception("storage", "vitals_insert_failed")
        self._power_events(row["throttled"])
        self._threshold_events(row)
        self.last_row = row
        self.log.debug("machine", "vitals", **{k: v for k, v in row.items() if k != "recorded_at"})
        return row

    # --- events ---------------------------------------------------------------------

    def _power_events(self, raw: Optional[int]) -> None:
        names = flag_names(raw)
        if self._last_now is None:
            self._last_now = names
            if names:
                self.log.event("warning", "power", "power_issue", "power problem: " + ", ".join(names),
                               now=names, since_boot=flag_names(raw, True), raw=raw)
            return
        if names != self._last_now:
            if names:
                self.log.event("warning", "power", "power_issue", "power problem: " + ", ".join(names),
                               now=names, since_boot=flag_names(raw, True), raw=raw)
            else:
                self.log.event("info", "power", "power_ok", "power back to normal",
                               since_boot=flag_names(raw, True), raw=raw)
            self._last_now = names

    def _threshold_events(self, row: Dict[str, Any]) -> None:
        checks = (
            ("cpu_hot", row["cpu_temp"] is not None and row["cpu_temp"] > CPU_HOT_C,
             f"CPU at {row['cpu_temp']} °C", {"cpu_temp": row["cpu_temp"]}),
            ("disk_low", row["disk_free"] is not None and row["disk_free"] < DISK_LOW_MB,
             f"only {row['disk_free']} MB free on the card", {"disk_free": row["disk_free"]}),
            ("memory_low", row["mem_free"] is not None and row["mem_free"] < MEM_LOW_MB,
             f"only {row['mem_free']} MB of memory free", {"mem_free": row["mem_free"]}),
        )
        for name, active, message, details in checks:
            was = self._episodes[name]
            if active and not was:
                source = "storage" if name == "disk_low" else "machine"
                self.log.event("warning", source, name, message, **details)
            self._episodes[name] = bool(active)

    # --- for the frame and the status document ----------------------------------------------

    def glyph(self) -> bool:
        return bool(flag_names(self.raw_throttled))

    def status(self) -> Dict[str, Any]:
        return {
            "now": flag_names(self.raw_throttled),
            "since_boot": flag_names(self.raw_throttled, since_boot=True),
            "raw": self.raw_throttled,
            "available": self.raw_throttled is not None,
        }
