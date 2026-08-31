import csv
import io
import logging
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from airmonitor.config import Config
from airmonitor.logging_utils import configure_logging
from airmonitor.storage import METRIC_FIELDS, AirMonitorDatabase
from utils.aqi import calculate_aqi, get_aqi_category, get_co2_category
from utils.display import create_display_image


LOGGER = logging.getLogger("airmonitor.dashboard")

CommandValidator = Callable[[Dict[str, Any]], Dict[str, Any]]


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


def env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value


def choose_bucket_seconds(hours: float) -> int:
    if hours <= 6:
        return 60
    if hours <= 24:
        return 300
    if hours <= 72:
        return 900
    return 1800


MAX_RANGE_SECONDS = 90 * 86400


def parse_timestamp(value: str, field_name: str) -> int:
    """Accept unix seconds or an ISO date/datetime (local time)."""
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be unix seconds or an ISO date/datetime"
        ) from exc


def resolve_range(args) -> Tuple[int, int]:
    """Turn ?hours= / ?from=&to= query params into a [start, end] window."""
    now = int(time.time())
    if args.get("from") is not None:
        start = parse_timestamp(args["from"], "from")
        end = parse_timestamp(args["to"], "to") if args.get("to") is not None else now
    else:
        hours = max(1, min(parse_int(args.get("hours", 24), "hours"), 24 * 30))
        start, end = now - hours * 3600, now
    if end <= start:
        raise ValueError("'to' must be after 'from'")
    if end - start > MAX_RANGE_SECONDS:
        raise ValueError("range must not exceed 90 days")
    return start, end


def parse_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} must be a boolean")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def compute_aqi_fields(metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Derive AQI + category labels from a metrics dict (single AQI source, B6)."""
    pm25, pm10, co2 = metrics.get("pm25"), metrics.get("pm10"), metrics.get("co2")
    if _is_number(pm25) and _is_number(pm10):
        value = calculate_aqi(pm25, pm10)
        category = get_aqi_category(value)
    else:
        value = None
        category = None
    return {
        "value": value,
        "category": category,
        "co2_category": get_co2_category(co2) if _is_number(co2) else None,
    }


def row_aqi(row: Dict[str, Any]) -> Any:
    if _is_number(row.get("pm25")) and _is_number(row.get("pm10")):
        return calculate_aqi(row["pm25"], row["pm10"])
    return None


def validate_empty(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {}


def validate_sps30_interval(payload: Dict[str, Any]) -> Dict[str, Any]:
    seconds = parse_int(payload.get("seconds"), "seconds")
    if seconds < 0 or seconds > 31536000:
        raise ValueError("seconds must be between 0 and 31536000")
    return {"seconds": seconds}


def validate_scd41_calibration(payload: Dict[str, Any]) -> Dict[str, Any]:
    target_co2 = parse_int(payload.get("target_co2"), "target_co2")
    if target_co2 < 350 or target_co2 > 2000:
        raise ValueError("target_co2 must be between 350 and 2000 ppm")
    confirmed = parse_bool(payload.get("confirmed"), "confirmed")
    persist = True if payload.get("persist") is None else parse_bool(payload.get("persist"), "persist")
    return {
        "target_co2": target_co2,
        "confirmed": confirmed,
        "persist": persist,
    }


def validate_scd41_asc(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "enabled": parse_bool(payload.get("enabled"), "enabled"),
        "persist": False if payload.get("persist") is None else parse_bool(payload.get("persist"), "persist"),
    }


def validate_system(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not parse_bool(payload.get("confirmed"), "confirmed"):
        raise ValueError("system commands require confirmed=true")
    return {"confirmed": True}


def create_app() -> Flask:
    app = Flask(__name__)
    # Same Config as the collector, so validation thresholds can't diverge.
    config = Config.from_env()
    database = AirMonitorDatabase(
        config.database_path, min_valid_co2_ppm=config.min_valid_co2_ppm
    )
    project_root = Path(__file__).resolve().parents[1]
    icons_dir = project_root / "assets" / "icons"
    command_validators: Dict[str, CommandValidator] = {
        "display_full_refresh": validate_empty,
        "display_partial_refresh": validate_empty,
        "sps30_force_clean": validate_empty,
        "sps30_set_auto_cleaning_interval": validate_sps30_interval,
        "scd41_force_calibration": validate_scd41_calibration,
        "scd41_set_asc": validate_scd41_asc,
        "system_restart_collector": validate_system,
        "system_restart_web": validate_system,
        "system_reboot": validate_system,
    }

    @app.after_request
    def add_response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(ValueError)
    def handle_value_error(exc: ValueError):
        if request.path.startswith("/api/"):
            LOGGER.warning("Validation error on %s: %s", request.path, exc)
            return jsonify({"error": str(exc)}), 400
        raise exc

    @app.errorhandler(404)
    def handle_not_found(_exc):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return "Not Found", 404

    @app.errorhandler(500)
    def handle_server_error(exc):
        LOGGER.exception("Unhandled server error on %s", request.path)
        database.insert_event(
            "error",
            "dashboard",
            "server_error",
            f"Unhandled server error on {request.path}",
            {"traceback": traceback.format_exc()},
        )
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error"}), 500
        return "Internal Server Error", 500

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/assets/icons/<path:filename>")
    def asset_icons(filename: str) -> Any:
        return send_from_directory(icons_dir, filename)

    @app.get("/api/health")
    def api_health() -> Any:
        summary = database.get_dashboard_summary()
        collector = summary.get("collector_status") or {}
        payload = collector.get("value") or {}
        is_running = bool(payload.get("running"))
        return jsonify({"ok": is_running, "collector": payload})

    @app.get("/api/summary")
    def api_summary() -> Any:
        summary = database.get_dashboard_summary()
        live = summary.get("latest_measurements") or {}
        metrics = live.get("value") or summary.get("latest_measurement") or {}
        summary["aqi"] = compute_aqi_fields(metrics)
        return jsonify(summary)

    @app.get("/api/history")
    def api_history() -> Any:
        start, end = resolve_range(request.args)
        bucket_seconds = choose_bucket_seconds((end - start) / 3600)
        rows = database.query_history_range(start, end, bucket_seconds)
        for row in rows:
            row["aqi"] = row_aqi(row)
        return jsonify(
            {
                "from_ts": start,
                "to_ts": end,
                "bucket_seconds": bucket_seconds,
                "rows": rows,
                "stats": database.query_stats(start, end),
            }
        )

    @app.get("/api/export.csv")
    def api_export_csv() -> Any:
        start, end = resolve_range(request.args)
        rows = database.export_rows(start, end)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["timestamp", *METRIC_FIELDS, "flags"])
        for row in rows:
            writer.writerow(
                [row["timestamp"], *[row[field] for field in METRIC_FIELDS], row["flags"] or ""]
            )
        filename = f"airmonitor_{start}_{end}.csv"
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/api/flags")
    def api_flags() -> Any:
        limit = max(1, min(parse_int(request.args.get("limit", 50), "limit"), 200))
        return jsonify({"flagged": database.get_recent_flagged(limit=limit)})

    @app.get("/api/display-preview.png")
    def api_display_preview() -> Any:
        """Render exactly what the e-paper shows, from the stored snapshot."""
        state = database.get_state("latest_display_snapshot")
        if state is None or not isinstance(state.get("value"), dict):
            return jsonify({"error": "no display snapshot yet"}), 404
        snapshot = state["value"].get("snapshot") or {}
        if config.display_rotation in (90, 270):
            width, height = 416, 240
        else:
            width, height = 240, 416
        image = create_display_image(width, height, snapshot, config.font_path)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return Response(
            buffer.getvalue(),
            mimetype="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/events")
    def api_events() -> Any:
        limit = max(1, min(parse_int(request.args.get("limit", 50), "limit"), 200))
        source = request.args.get("source") or None
        level = request.args.get("level") or None
        return jsonify({"events": database.get_recent_events(limit=limit, source=source, level=level)})

    @app.delete("/api/history")
    def api_delete_history() -> Any:
        # A browser confirm() is not a guard: any device on the LAN can send
        # this request blind. Require the intent to be spelled out server-side.
        body = request.get_json(silent=True) or {}
        if body.get("confirm") != "delete":
            return jsonify({"error": 'history deletion requires {"confirm": "delete"}'}), 400
        deleted_rows = database.delete_history()
        LOGGER.warning("Deleted %s history rows via dashboard", deleted_rows)
        database.insert_event(
            "warning",
            "dashboard",
            "history_deleted",
            f"Deleted {deleted_rows} history rows via dashboard",
            {"deleted_rows": deleted_rows, "remote_addr": request.remote_addr},
        )
        return jsonify({"status": f"Deleted {deleted_rows} history rows."})

    @app.post("/api/commands")
    def api_commands() -> Any:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "request body must be a JSON object"}), 400
        command = str(body.get("command", "")).strip()
        raw_payload = body.get("payload") or {}
        if not command:
            return jsonify({"error": "command is required"}), 400
        if command not in command_validators:
            return jsonify({"error": f"unsupported command: {command}"}), 400
        if not isinstance(raw_payload, dict):
            return jsonify({"error": "payload must be a JSON object"}), 400

        payload = command_validators[command](raw_payload)
        command_id = database.queue_command(command, payload)
        LOGGER.info("Queued command %s #%s", command, command_id)
        database.insert_event(
            "info",
            "dashboard",
            "command_queued",
            f"Queued command {command}",
            {
                "id": command_id,
                "command": command,
                "payload": payload,
                "remote_addr": request.remote_addr,
            },
        )
        return jsonify({"id": command_id, "status": "pending"}), 202

    return app


LOG_FILE = env_str("AIRMONITOR_DASHBOARD_LOG_FILE", "data/logs/dashboard.log")
LOGGER = configure_logging("airmonitor.dashboard", log_file=LOG_FILE)
app = create_app()


if __name__ == "__main__":
    from waitress import serve

    host = env_str("AIRMONITOR_WEB_HOST", "0.0.0.0")
    port = env_int("AIRMONITOR_WEB_PORT", 8080)
    LOGGER.info("Serving dashboard on %s:%s", host, port)
    serve(app, host=host, port=port, threads=4)
