"""The logger every app uses: key=value log lines, and events rows for the
things the dashboard should show.

One line per fact, one format everywhere:

    2026-09-03T12:00:10Z DEBUG collector scd41 sample co2=812 temp=23.41 ok=1

= UTC time, level, app, source (subsystem), a one-word message, then
``key=value`` pairs (None → ``-``, booleans → 1/0, anything with a space or
``=`` double-quoted). Files live in ``paths.logs/<app>.log``, one per UTC day,
``retention_days.logs`` of them kept. A log or database failure never
propagates to the caller: it is counted in ``failures`` and the app carries on.
"""

import json
import logging
import sys
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

LEVELS = ("debug", "info", "warning", "error")
_LEVEL_NO = {"debug": 10, "info": 20, "warning": 30, "error": 40}

_SENSOR_TYPES = (
    "sensor_init", "sensor_reinit", "sensor_error", "value_dropped", "warming_up",
    "fan_clean", "calibration_done", "calibration_refused",
)

# source -> the event types it may emit. Decided in redesign.md §12; adding a
# type is a deliberate act here, never an ad-hoc string in a module.
EVENT_TYPES: Dict[str, tuple] = {
    "scd41": _SENSOR_TYPES,
    "sht41": _SENSOR_TYPES,
    "sps30": _SENSOR_TYPES,
    "i2c": _SENSOR_TYPES,
    "display": ("display_error", "display_reinit"),
    "weather": ("weather_error", "weather_stale"),
    "wifi": ("wifi_down", "wifi_up", "wifi_bounce", "internet_down", "internet_up"),
    "power": ("power_issue", "power_ok"),
    "watch": ("collector_silent", "collector_restarted"),
    "storage": ("nightly", "rollup_catchup", "storage_error", "disk_low"),
    "machine": ("cpu_hot", "memory_low"),
    "app": ("started", "shutdown", "clock_unsynced", "clock_jump",
            "command_done", "command_failed", "error"),
    "web": ("command_created", "server_error"),
}

APPS = ("collector", "manager", "dashboard")


def is_known_event(source: str, type_: str) -> bool:
    return type_ in EVENT_TYPES.get(source, ())


def format_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)
        return json.dumps(text)
    text = str(value)
    if text == "" or any(ch in text for ch in ' ="\n\t'):
        return json.dumps(text)
    return text


def format_kv(kv: Dict[str, Any]) -> str:
    return " ".join(f"{key}={format_value(value)}" for key, value in kv.items())


def format_line(ts: float, level: str, app: str, source: str, message: str,
                kv: Optional[Dict[str, Any]] = None) -> str:
    stamp = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    head = f"{stamp} {level.upper()} {app} {source} {message.strip().replace(' ', '_')}"
    tail = format_kv(kv) if kv else ""
    return f"{head} {tail}" if tail else head


def git_commit(repo_root: Path) -> str:
    """Short hash of HEAD without running git; '-' when it cannot be read."""
    try:
        head = (repo_root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref:"):
            ref = head.split(" ", 1)[1].strip()
            ref_path = repo_root / ".git" / ref
            if ref_path.exists():
                return ref_path.read_text().strip()[:7]
            packed = repo_root / ".git" / "packed-refs"
            for line in packed.read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:7]
            return "-"
        return head[:7]
    except OSError:
        return "-"


class _CountingHandler(TimedRotatingFileHandler):
    """A file handler that counts write failures instead of printing them."""

    failures = 0

    def handleError(self, record):  # noqa: N802 (logging API)
        self.failures += 1


class Log:
    def __init__(self, app: str, config, db=None, strict: bool = False,
                 stream: Optional[TextIO] = None, clock=time.time):
        if app not in APPS:
            raise ValueError(f"unknown app {app!r}")
        self.app = app
        self.db = db
        self.strict = strict
        self.clock = clock
        self.level = config.logging.level
        self._threshold = _LEVEL_NO[self.level]
        self.db_failures = 0
        self.unknown_events = 0

        logs_dir = Path(config.paths.logs)
        logs_dir.mkdir(parents=True, exist_ok=True)
        self.path = logs_dir / f"{app}.log"
        self._file = _CountingHandler(
            str(self.path), when="midnight", utc=True,
            backupCount=int(config.retention_days.logs), encoding="utf-8",
        )
        self._file.setFormatter(logging.Formatter("%(message)s"))
        self._stream = logging.StreamHandler(stream or sys.stderr)
        self._stream.setFormatter(logging.Formatter("%(message)s"))
        self._stream.setLevel(logging.WARNING)
        self._logger = logging.getLogger(f"airstation.{app}.{id(self)}")
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers = [self._file, self._stream]

    # --- plain lines --------------------------------------------------------------

    @property
    def failures(self) -> int:
        """Log-line write failures + events-row write failures."""
        return self._file.failures + self.db_failures

    def _emit(self, level: str, source: str, message: str, kv: Dict[str, Any]) -> None:
        if _LEVEL_NO[level] < self._threshold:
            return
        line = format_line(self.clock(), level, self.app, source, message, kv)
        self._logger.log(_LEVEL_NO[level], line)

    def debug(self, source: str, message: str, **kv: Any) -> None:
        self._emit("debug", source, message, kv)

    def info(self, source: str, message: str, **kv: Any) -> None:
        self._emit("info", source, message, kv)

    def warning(self, source: str, message: str, **kv: Any) -> None:
        self._emit("warning", source, message, kv)

    def error(self, source: str, message: str, **kv: Any) -> None:
        self._emit("error", source, message, kv)

    # --- events (line + row) -------------------------------------------------------

    def event(self, level: str, source: str, type_: str, message: str, **details: Any) -> None:
        if level not in ("info", "warning", "error"):
            raise ValueError(f"event level must be info/warning/error, got {level!r}")
        if not is_known_event(source, type_):
            if self.strict:
                raise ValueError(f"unknown event type {source}.{type_}")
            self.unknown_events += 1
            self._emit("warning", "app", "unknown_event_type", {"source": source, "type": type_})
        self._emit(level, source, type_, {"msg": message, **details})
        if self.db is None:
            return
        try:
            self.db.insert_event(self.app, level, source, type_, message, details, ts=int(self.clock()))
        except Exception:
            self.db_failures += 1

    def exception(self, source: str, message: str, **kv: Any) -> None:
        """Call from an ``except`` block: full traceback in the line, first line in the event."""
        text = traceback.format_exc()
        first = text.strip().splitlines()[-1] if text.strip() and text.strip() != "NoneType: None" else "-"
        self._emit("error", source, message, {**kv, "exc": first, "traceback": text})
        self.event("error", "app", "error", message, origin=source, exc=first, **kv)

    def start_line(self, config, commit: Optional[str] = None) -> None:
        self.info(
            "app", "start",
            commit=commit if commit is not None else git_commit(Path(config.repo_root)),
            python=sys.version.split()[0],
            level=self.level,
            config=config.as_dict(),
        )

    def close(self) -> None:
        for handler in list(self._logger.handlers):
            handler.flush()
            handler.close()
            self._logger.removeHandler(handler)
