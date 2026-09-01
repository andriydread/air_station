import csv
import io
import logging
import math
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
    if hours <= 168:
        return 1800
    if hours <= 720:
        return 3600
    if hours <= 2160:
        return 3 * 3600
    return 86400


# Ranges past raw retention are served from the hourly rollups (R8), which
# never get pruned — 5 years is a UI sanity bound, not a data limit.
MAX_RANGE_SECONDS = 5 * 365 * 86400


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
        max_hours = MAX_RANGE_SECONDS // 3600
        hours = max(1, min(parse_int(args.get("hours", 24), "hours"), max_hours))
        start, end = now - hours * 3600, now
    if end <= start:
        raise ValueError("'to' must be after 'from'")
    if end - start > MAX_RANGE_SECONDS:
        raise ValueError("range must not exceed 5 years")
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
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


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
    allow_large_offset = (
        False if payload.get("allow_large_offset") is None
        else parse_bool(payload.get("allow_large_offset"), "allow_large_offset")
    )
    return {
        "target_co2": target_co2,
        "confirmed": confirmed,
        "persist": persist,
        "allow_large_offset": allow_large_offset,
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

    # DNS-rebinding hardening: a malicious page can re-resolve its own
    # hostname to the Pi's LAN IP and become same-origin with :8080. With
    # AIRMONITOR_ALLOWED_HOSTS set, requests carrying a foreign Host header
    # are refused before any route runs. Empty (default) = check disabled.
    allowed_hosts = {
        host.strip().lower()
        for host in config.allowed_hosts.split(",")
        if host.strip()
    }

    @app.before_request
    def check_host_header():
        if not allowed_hosts:
            return None
        host = (request.host or "").rsplit(":", 1)[0].lower()
        if host not in allowed_hosts:
            return jsonify({"error": "unrecognized host"}), 421
        return None

    @app.after_request
    def add_response_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if request.path.startswith("/api/") and "ETag" not in response.headers:
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
        try:
            database.insert_event(
                "error",
                "dashboard",
                "server_error",
                f"Unhandled server error on {request.path}",
                {"traceback": traceback.format_exc()},
            )
        except Exception:
            # The most likely cause of a 500 is the database itself; the
            # handler must still return JSON, not crash into Flask's HTML.
            LOGGER.exception("Could not record server_error event")
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

    # COUNT(*) walks the whole measurements table (~750k rows at full
    # retention) — too heavy for the 10s summary poll, so it's cached.
    db_stats_cache: Dict[str, Any] = {"at": 0.0, "value": None}

    def cached_database_stats() -> Dict[str, Any]:
        now = time.monotonic()
        if db_stats_cache["value"] is None or now - db_stats_cache["at"] > 60:
            db_stats_cache["value"] = database.database_stats()
            db_stats_cache["at"] = now
        return db_stats_cache["value"]

    @app.get("/api/summary")
    def api_summary() -> Any:
        summary = database.get_dashboard_summary()
        live = summary.get("latest_measurements") or {}
        metrics = live.get("value") or summary.get("latest_measurement") or {}
        summary["aqi"] = compute_aqi_fields(metrics)
        summary["database"] = cached_database_stats()
        return jsonify(summary)

    @app.get("/api/history")
    def api_history() -> Any:
        start, end = resolve_range(request.args)
        bucket_seconds = choose_bucket_seconds((end - start) / 3600)
        # Hour-or-coarser buckets read the rollup table (cheaper, and the only
        # source once the range outlives raw retention); fine buckets stay raw
        # — unless the window starts before the raw horizon, where only the
        # rollups still hold data (a short range 6 months back must not come
        # up empty just because it is short).
        raw_horizon = (
            int(time.time()) - config.keep_measurements_days * 86400
            if config.keep_measurements_days > 0 else None
        )
        if raw_horizon is not None and start < raw_horizon:
            bucket_seconds = max(bucket_seconds, 3600)
        if bucket_seconds >= 3600:
            rows = database.query_history_hourly(start, end, bucket_seconds)
            stats = database.query_stats_hourly(start, end)
        else:
            rows = database.query_history_range(start, end, bucket_seconds)
            stats = database.query_stats(start, end)
        for row in rows:
            row["aqi"] = row_aqi(row)
        return jsonify(
            {
                "from_ts": start,
                "to_ts": end,
                "bucket_seconds": bucket_seconds,
                "rows": rows,
                "stats": stats,
            }
        )

    @app.get("/api/export.csv")
    def api_export_csv() -> Any:
        start, end = resolve_range(request.args)

        def generate():
            # Streamed: a 90-day export is ~780k rows and must not be
            # materialized in RAM on a Pi Zero (export_rows pages in chunks).
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["timestamp", *METRIC_FIELDS, "flags"])
            yield buffer.getvalue()
            for row in database.export_rows(start, end):
                buffer.seek(0)
                buffer.truncate()
                writer.writerow(
                    [row["timestamp"], *[row[field] for field in METRIC_FIELDS], row["flags"] or ""]
                )
                yield buffer.getvalue()

        filename = f"airmonitor_{start}_{end}.csv"
        return Response(
            generate(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.get("/api/flags")
    def api_flags() -> Any:
        limit = max(1, min(parse_int(request.args.get("limit", 50), "limit"), 200))
        return jsonify({"flagged": database.get_recent_flagged(limit=limit)})

    @app.get("/api/display-preview.png")
    def api_display_preview() -> Any:
        """Render exactly what the e-paper shows, from the stored snapshot.

        ETagged on the snapshot's timestamp: the Live tab polls this every
        60s and re-rendering an unchanged PIL frame + PNG encode on a Pi
        Zero is pure waste — a 304 costs nothing. (Trade-off: the clock in
        the preview header freezes at snapshot time between changes.)
        """
        state = database.get_state("latest_display_snapshot")
        if state is None or not isinstance(state.get("value"), dict):
            return jsonify({"error": "no display snapshot yet"}), 404
        etag = f'"{state.get("updated_at_ts")}"'
        if request.headers.get("If-None-Match") == etag:
            return Response(status=304, headers={"ETag": etag})
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
            # no-cache (not no-store): the browser may keep it but must
            # revalidate, which the ETag answers with a 304.
            headers={"Cache-Control": "no-cache", "ETag": etag},
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
        db_stats_cache["at"] = 0.0  # size/count must not show stale numbers now
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


# No module-level app: importing this module must stay side-effect free
# (no logging setup, no database file creation). The service entry point is
# `python -m dashboard.app`; tests call create_app() themselves.
if __name__ == "__main__":
    from waitress import serve

    configure_logging(
        "airmonitor.dashboard",
        log_file=env_str("AIRMONITOR_DASHBOARD_LOG_FILE", "data/logs/dashboard.log"),
    )
    host = env_str("AIRMONITOR_WEB_HOST", "0.0.0.0")
    port = env_int("AIRMONITOR_WEB_PORT", 8080)
    LOGGER.info("Serving dashboard on %s:%s", host, port)
    serve(create_app(), host=host, port=port, threads=4)
