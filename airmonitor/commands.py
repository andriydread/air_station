"""Executes commands queued by the dashboard.

The dashboard writes a command row into SQLite; the collector polls for
pending rows and runs them here. Each handler returns a small dict that
is stored as the command result and shown on the dashboard.
"""

import logging
import os
import shutil
import subprocess
import time
import traceback
from typing import Any, Callable, Dict, List

from airmonitor.sensors import utc_now_iso

LOGGER = logging.getLogger("airmonitor")


def _find_executable(name: str, fallbacks: List[str]) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError(f"{name} not found on this system")


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    raise ValueError("boolean payload value is invalid")


def as_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


class CommandProcessor:
    def __init__(self, app):
        self.app = app  # the AirMonitor instance from main.py
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
            "display_full_refresh": self._display_full_refresh,
            "display_partial_refresh": self._display_partial_refresh,
            "sps30_force_clean": self._sps30_force_clean,
            "sps30_set_auto_cleaning_interval": self._sps30_set_interval,
            "scd41_force_calibration": self._scd41_force_calibration,
            "scd41_set_asc": self._scd41_set_asc,
            "system_restart_collector": self._system_restart_collector,
            "system_restart_web": self._system_restart_web,
            "system_reboot": self._system_reboot,
        }
        # Injection point for tests; production spawns detached shells.
        self.spawn = subprocess.Popen

    def process_pending(self) -> None:
        for command in self.app.database.claim_pending_commands():
            name, payload = command["command"], command["payload"]
            self.app.events.log(
                logging.INFO, "command", "started", f"Processing command {name}",
                {"id": command["id"], "payload": payload},
            )
            try:
                handler = self.handlers.get(name)
                if handler is None:
                    raise ValueError(f"Unsupported command: {name}")
                if not isinstance(payload, dict):
                    raise ValueError("command payload must be a JSON object")
                result = handler(payload)
                self.app.database.complete_command(command["id"], True, result)
                self.app.events.log(
                    logging.INFO, "command", "succeeded", f"Command {name} succeeded",
                    {"id": command["id"], "result": result},
                )
            except Exception as exc:
                LOGGER.exception("Command %s failed", name)
                failure = {
                    "error": str(exc),
                    "type": exc.__class__.__name__,
                    "traceback": traceback.format_exc(),
                }
                self.app.database.complete_command(command["id"], False, failure)
                self.app.events.log(
                    logging.ERROR, "command", "failed", f"Command {name} failed: {exc}",
                    {"id": command["id"], "payload": payload, **failure},
                )
            finally:
                self.app.publish_status()

    # --- Handlers -----------------------------------------------------------

    def _display_full_refresh(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self.app.redraw_display(full_refresh=True)
        return {"message": "Triggered full display refresh"}

    def _display_partial_refresh(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self.app.redraw_display(full_refresh=False)
        return {"message": "Triggered partial display refresh"}

    def _require_sensor(self, sensor, name: str):
        if sensor is None:
            raise RuntimeError(f"{name} is not initialized")
        return sensor

    def _sps30_force_clean(self, _payload: Dict[str, Any]) -> Dict[str, Any]:
        self._require_sensor(self.app.sps30, "SPS30").force_clean()
        return {"message": "Triggered SPS30 fan cleaning"}

    def _sps30_set_interval(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        seconds = as_int(payload.get("seconds", 604800), "seconds")
        if not 0 <= seconds <= 31536000:
            raise ValueError("seconds must be between 0 and 31536000")
        self._require_sensor(self.app.sps30, "SPS30").set_auto_cleaning_interval(seconds)
        return {"message": "Updated SPS30 auto cleaning interval", "seconds": seconds}

    def _scd41_force_calibration(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        scd41 = self._require_sensor(self.app.scd41, "SCD41")
        target_co2 = as_int(payload.get("target_co2", 420), "target_co2")
        persist = as_bool(payload.get("persist"), True)
        if not as_bool(payload.get("confirmed"), False):
            raise ValueError(
                "Forced calibration requires explicit confirmation that the sensor is in known stable air"
            )
        if not 350 <= target_co2 <= 2000:
            raise ValueError("target_co2 must be between 350 and 2000 ppm")

        validation = scd41.check_calibration_preconditions(target_co2)
        correction = scd41.force_calibration(target_co2, persist)

        result = {
            "message": "Triggered SCD41 forced calibration",
            "target_co2": target_co2,
            "persisted": persist,
            "correction": correction,
            "validation": validation,
            "calibrated_at": utc_now_iso(),
        }
        scd41.health.state["last_calibration_at"] = result["calibrated_at"]
        self.app.database.set_state("scd41_last_calibration", result)
        return result

    def _scd41_set_asc(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        enabled = as_bool(payload.get("enabled"), False)
        persist = as_bool(payload.get("persist"), False)
        applied = self._require_sensor(self.app.scd41, "SCD41").set_asc(enabled, persist)
        return {"message": "Updated SCD41 ASC setting", "enabled": applied, "persisted": persist}

    # --- System actions (sudo grants in systemd/airmonitor-sudoers) --------

    def _deferred_system_action(self, command: str, message: str) -> Dict[str, Any]:
        """Run a privileged action a couple of seconds from now, detached.

        The delay lets this command row complete and its result reach the
        dashboard BEFORE the action kills this very process (collector
        restart, reboot). The commands are fixed strings — nothing from the
        payload is ever interpolated.
        """
        self.spawn(["sh", "-c", f"sleep 2 && {command}"])
        return {"message": message}

    def _system_restart_collector(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not as_bool(payload.get("confirmed"), False):
            raise ValueError("system commands require explicit confirmation")
        systemctl = _find_executable("systemctl", ["/usr/bin/systemctl", "/bin/systemctl"])
        return self._deferred_system_action(
            f"sudo -n {systemctl} restart airmonitor",
            "Collector restart scheduled (2s)",
        )

    def _system_restart_web(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not as_bool(payload.get("confirmed"), False):
            raise ValueError("system commands require explicit confirmation")
        systemctl = _find_executable("systemctl", ["/usr/bin/systemctl", "/bin/systemctl"])
        return self._deferred_system_action(
            f"sudo -n {systemctl} restart airmonitor-web",
            "Dashboard restart scheduled (2s)",
        )

    def _system_reboot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not as_bool(payload.get("confirmed"), False):
            raise ValueError("system commands require explicit confirmation")
        reboot = _find_executable("reboot", ["/usr/sbin/reboot", "/sbin/reboot"])
        return self._deferred_system_action(
            f"sudo -n {reboot}",
            "Reboot scheduled (2s)",
        )
