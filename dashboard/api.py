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


MAX_RANGE_SECONDS = 5 * 365 * 86400
DEFAULT_RANGE_SECONDS = 24 * 3600

# (span up to, bucket seconds): fine enough to look continuous, coarse enough to be light
_BUCKETS = ((2 * 3600, 10), (6 * 3600, 60), (24 * 3600, 300), (3 * 86400, 900),
            (7 * 86400, 1800), (31 * 86400, 3600), (93 * 86400, 3 * 3600))


def choose_bucket_seconds(span: int) -> int:
    for limit, bucket in _BUCKETS:
        if span <= limit:
            return bucket
    return 86400


def parse_range(args) -> tuple:
    """?from=<unix>&to=<unix>; default the last 24 h. ValueError → 400."""
    now = int(clock.now())
    try:
        end = int(args.get("to", now))
        start = int(args.get("from", end - DEFAULT_RANGE_SECONDS))
    except (TypeError, ValueError):
        raise ValueError("from and to must be Unix seconds") from None
    if end <= start:
        raise ValueError("to must be after from")
    if end - start > MAX_RANGE_SECONDS:
        raise ValueError("range longer than five years")
    return start, end


def history_rows(db, config, start: int, end: int) -> Dict[str, Any]:
    """Raw buckets inside the raw retention window, hourly rows beyond it."""
    from shared.aqi import aqi_from_pm25
    from shared.db import METRICS

    now = int(clock.now())
    raw_horizon = now - config.retention_days.raw * 86400
    bucket = choose_bucket_seconds(end - start)
    if start >= raw_horizon and bucket < 3600:
        rows = db.raw_bucketed(start, end, bucket)
        stats = db.raw_stats(start, end)
        resolution = "raw"
    else:
        bucket = max(bucket, 3600)
        rows = []
        for row in db.hourly_between(start, end):
            item = {"ts": row["hour"], "samples": row["samples"]}
            for m in METRICS:
                item[m] = row[f"{m}_avg"]
                item[f"{m}_min"] = row[f"{m}_min"]
                item[f"{m}_max"] = row[f"{m}_max"]
            rows.append(item)
        stats = db.hourly_stats(start, end)
        resolution = "hourly"
    for row in rows:
        row["aqi"] = aqi_from_pm25(row.get("pm25"))
    return {"from": start, "to": end, "bucket_seconds": bucket, "resolution": resolution,
            "raw_horizon": raw_horizon, "rows": rows, "stats": stats}


@api.get("/history")
def history() -> Any:
    from flask import request

    rt = _rt()
    start, end = parse_range(request.args)
    return jsonify(history_rows(rt["db"], rt["config"], start, end))


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
