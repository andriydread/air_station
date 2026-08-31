import logging
import os
import traceback
from pathlib import Path
from typing import Any, Callable, Dict

from flask import Flask, jsonify, render_template, request, send_from_directory

from airmonitor.config import Config
from airmonitor.logging_utils import configure_logging
from airmonitor.storage import AirMonitorDatabase


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


def choose_bucket_seconds(hours: int) -> int:
    if hours <= 6:
        return 60
    if hours <= 24:
        return 300
    if hours <= 72:
        return 900
    return 1800


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
        "sps30_force_clean": validate_empty,
        "sps30_set_auto_cleaning_interval": validate_sps30_interval,
        "scd41_force_calibration": validate_scd41_calibration,
        "scd41_set_asc": validate_scd41_asc,
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
        return jsonify(database.get_dashboard_summary())

    @app.get("/api/history")
    def api_history() -> Any:
        hours = max(1, min(parse_int(request.args.get("hours", 24), "hours"), 24 * 30))
        bucket_seconds = choose_bucket_seconds(hours)
        return jsonify(
            {
                "hours": hours,
                "bucket_seconds": bucket_seconds,
                "rows": database.query_history(hours, bucket_seconds),
            }
        )

    @app.get("/api/events")
    def api_events() -> Any:
        limit = max(1, min(parse_int(request.args.get("limit", 50), "limit"), 200))
        source = request.args.get("source") or None
        level = request.args.get("level") or None
        return jsonify({"events": database.get_recent_events(limit=limit, source=source, level=level)})

    @app.delete("/api/history")
    def api_delete_history() -> Any:
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
    host = env_str("AIRMONITOR_WEB_HOST", "0.0.0.0")
    port = env_int("AIRMONITOR_WEB_PORT", 8080)
    app.run(host=host, port=port, debug=False)
