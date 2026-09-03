"""How the collector answers the two buttons meant for it.

Every 2 s: claim this app's pending commands, run each, write success or
fail with a result. A handler that raises fails its command and the loop
carries on; an unknown type fails with "unsupported".
"""

import time
from typing import Any, Callable, Dict

from collector.sensors import CalibrationRefused

TARGET_RANGE = (400, 2000)


def _calibrate(runner, payload: Dict[str, Any], now: float) -> Dict[str, Any]:
    target = payload.get("target_ppm", runner.config.sensors.calibration_target_ppm)
    try:
        target = int(target)
    except (TypeError, ValueError):
        raise ValueError(f"target_ppm must be a whole number, got {target!r}") from None
    if not TARGET_RANGE[0] <= target <= TARGET_RANGE[1]:
        raise ValueError(f"target_ppm must be between {TARGET_RANGE[0]} and {TARGET_RANGE[1]}")
    allow_large_offset = bool(payload.get("allow_large_offset", False))
    persist = bool(payload.get("persist", False))
    try:
        result = runner.sampler.scd41.force_calibration(now, target, allow_large_offset, persist)
    except CalibrationRefused as exc:
        runner.log.event("warning", "scd41", "calibration_refused", str(exc), target_ppm=target)
        raise
    record = {"at": int(now), "target_ppm": target, "correction_ppm": result["correction_ppm"],
              "persisted": persist}
    runner.db.set_state("last_calibration", record)
    runner.last_calibration = record
    runner.log.event("info", "scd41", "calibration_done",
                     f"forced calibration to {target} ppm, correction {result['correction_ppm']} ppm",
                     **record)
    return result


def _fan_clean(runner, _payload: Dict[str, Any], now: float) -> Dict[str, Any]:
    return runner.sampler.sps30.force_clean(now, manual=True)


HANDLERS: Dict[str, Callable] = {
    "scd41_calibrate": _calibrate,
    "sps30_fan_clean": _fan_clean,
}


class CommandRunner:
    APP = "collector"

    def __init__(self, db, log, sampler, config, monotonic=time.monotonic):
        self.db = db
        self.log = log
        self.sampler = sampler
        self.config = config
        self.monotonic = monotonic
        self.last_calibration = None
        self.handled = 0

    def process(self, now: float) -> int:
        """Claim and run pending commands for the collector; returns how many ran."""
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
            result = handler(self, payload, now)
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
