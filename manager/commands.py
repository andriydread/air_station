"""How the manager answers its four buttons.

Restart collector / restart dashboard / reboot run fixed command strings
through sudo, deferred two seconds in a detached shell so the command row
is completed before the action lands. Delete history clears the
measurement tables. Reboot and delete history require ``confirmed: true``
in the payload (the dashboard asks for a typed confirmation).
"""

import subprocess
import time
from typing import Any, Callable, Dict

SYSTEM_COMMANDS = {
    "restart_collector": "sudo systemctl restart airstation-collector",
    "restart_dashboard": "sudo systemctl restart airstation-dashboard",
    "reboot": "sudo reboot",
}
CONFIRM_REQUIRED = ("reboot", "delete_history")
DEFER_SECONDS = 2


def _require_confirmation(type_: str, payload: Dict[str, Any]) -> None:
    if type_ in CONFIRM_REQUIRED and payload.get("confirmed") is not True:
        raise ValueError(f"{type_} needs confirmed=true")


def _system(runner, type_: str, payload: Dict[str, Any], now: float) -> Dict[str, Any]:
    _require_confirmation(type_, payload)
    command = SYSTEM_COMMANDS[type_]
    argv = ["sh", "-c", f"sleep {DEFER_SECONDS}; exec {command}"]
    runner.spawner(argv, start_new_session=True)
    return {"scheduled": command, "in_s": DEFER_SECONDS}


def _delete_history(runner, type_: str, payload: Dict[str, Any], now: float) -> Dict[str, Any]:
    _require_confirmation(type_, payload)
    counts = runner.db.delete_history()
    runner.log.warning("storage", "history_deleted", **counts)
    return {"deleted": counts}


HANDLERS: Dict[str, Callable] = {
    "restart_collector": _system,
    "restart_dashboard": _system,
    "reboot": _system,
    "delete_history": _delete_history,
}


class CommandRunner:
    APP = "manager"

    def __init__(self, db, log, spawner: Callable = subprocess.Popen, monotonic=time.monotonic):
        self.db = db
        self.log = log
        self.spawner = spawner
        self.monotonic = monotonic
        self.handled = 0

    def process(self, now: float) -> int:
        commands = self.db.claim_pending(self.APP)
        for command in commands:
            self._run(command, now)
        return len(commands)

    def _run(self, command: Dict[str, Any], now: float) -> None:
        cid, type_, payload = command["id"], command["type"], command.get("payload") or {}
        started = self.monotonic()
        handler = HANDLERS.get(type_)
        try:
            if handler is None:
                raise ValueError(f"unsupported command: {type_}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            result = handler(self, type_, payload, now)
        except Exception as exc:
            took = round((self.monotonic() - started) * 1000)
            self.db.complete_command(cid, False, {"error": str(exc)})
            self.log.event("warning", "app", "command_failed", f"{type_} failed: {exc}",
                           id=cid, type=type_, error=str(exc), ms=took)
            self.handled += 1
            return
        took = round((self.monotonic() - started) * 1000)
        self.db.complete_command(cid, True, result)
        self.log.event("info", "app", "command_done", f"{type_} done", id=cid, type=type_, ms=took,
                       result=result)
        self.handled += 1
