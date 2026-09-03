"""The dashboard's routes — the browser reads the tables through these.

``/api/changes`` is the cheap "what changed?" the browser asks every 10 s;
everything else is fetched only when its stamp moved. The dashboard never
computes or cleans data: it shows what the collector and the manager wrote.
"""

from typing import Any, Dict, Optional

from flask import Blueprint, current_app, jsonify

from shared import clock

api = Blueprint("api", __name__, url_prefix="/api")

STATE_KEYS = ("display_data", "collector_status", "manager_status", "last_weather", "last_calibration")


def _rt() -> Dict[str, Any]:
    return current_app.extensions["airstation"]


def _state(db, key: str) -> Optional[Dict[str, Any]]:
    doc = db.get_state(key)
    return {"value": doc["value"], "updated_at": doc["updated_at"]} if doc else None


@api.get("/changes")
def changes() -> Any:
    db = _rt()["db"]
    stamps = db.state_updated_at(STATE_KEYS)
    latest_vitals = db.latest_vitals()
    return jsonify({
        **{key: stamps.get(key) for key in STATE_KEYS},
        "event_id": db.newest_event_id(),
        "command_id": db.newest_command_id(),
        "vitals_at": latest_vitals["recorded_at"] if latest_vitals else None,
        "raw_at": db.latest_raw_at(),
        "now": int(clock.now()),
    })


@api.get("/live")
def live() -> Any:
    rt = _rt()
    db = rt["db"]
    display = _state(db, "display_data")
    collector = _state(db, "collector_status")
    manager = _state(db, "manager_status")
    calibration = _state(db, "last_calibration")
    now = int(clock.now())
    uptimes = {
        "collector": (collector or {}).get("value", {}).get("uptime") if collector else None,
        "manager": (manager or {}).get("value", {}).get("uptime") if manager else None,
        "dashboard": now - rt["started_at"],
    }
    return jsonify({
        "display_data": display,
        "collector_status": collector,
        "manager_status": manager,
        "last_calibration": calibration,
        "version": {"commit": rt["commit"], "uptimes": uptimes},
        "now": now,
    })
