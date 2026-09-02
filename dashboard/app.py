import csv
import io
import logging
import math
import os
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

# Text export: bucket ladder and the row budget it is chosen against. The
# output is meant to be pasted into a chat/prompt, so ~150 lines is the
# point, not completeness (the CSV export is for that).
TEXT_EXPORT_BUCKETS = (60, 300, 600, 900, 1800, 3600, 3 * 3600, 6 * 3600, 86400)
TEXT_EXPORT_MAX_ROWS = 150
TEXT_EXPORT_DEFAULT_METRICS = ("co2", "temp", "humid", "pm25")
METRIC_UNITS = {
    "co2": "ppm", "temp": "°C", "humid": "%", "pm1": "µg/m³", "pm25": "µg/m³",
    "pm4": "µg/m³", "pm10": "µg/m³", "tps": "µm",
}
METRIC_DIGITS = {"co2": 0, "temp": 1, "humid": 0, "pm1": 1, "pm25": 1, "pm4": 1, "pm10": 1, "tps": 2}


def choose_text_bucket_seconds(range_seconds: int) -> int:
    for bucket in TEXT_EXPORT_BUCKETS:
        if range_seconds / bucket <= TEXT_EXPORT_MAX_ROWS:
            return bucket
    return TEXT_EXPORT_BUCKETS[-1]


def parse_metrics(value: Any) -> Tuple[str, ...]:
    if not value:
        return TEXT_EXPORT_DEFAULT_METRICS
    metrics = tuple(part.strip() for part in str(value).split(",") if part.strip())
    unknown = [m for m in metrics if m not in METRIC_FIELDS]
    if unknown:
        raise ValueError(f"unknown metrics: {', '.join(unknown)}")
    return metrics or TEXT_EXPORT_DEFAULT_METRICS


def _format_value(metric: str, value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.{METRIC_DIGITS.get(metric, 1)}f}"


def render_text_export(
    *,
    start: int,
    end: int,
    bucket_seconds: int,
    metrics: Tuple[str, ...],
    rows: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    flagged: List[Dict[str, Any]],
    now: Optional[float] = None,
) -> str:
    """A compact, human/LLM-readable dump: bucket rows with station events
    and flagged samples interleaved by time, in the Pi's local time.

    Events shown: everything the collector said about itself (starts,
    shutdowns) plus every warning/error — the context a CO2 spike needs.
    """
    local = datetime.fromtimestamp(end if now is None else now).astimezone()
    offset = local.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "local time"
    multi_day = (end - start) > 86400
    stamp_format = "%m-%d %H:%M" if multi_day else "%H:%M"

    def stamp(ts: int) -> str:
        return datetime.fromtimestamp(ts).strftime(stamp_format)

    if bucket_seconds >= 3600:
        cadence = f"{bucket_seconds // 3600}-hour averages"
    else:
        cadence = f"{bucket_seconds // 60}-min averages" if bucket_seconds >= 60 else f"{bucket_seconds}s buckets"
    lines = [
        f"# air_station export · {datetime.fromtimestamp(start).strftime('%Y-%m-%d %H:%M')} → "
        f"{datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M')} ({offset})",
        f"# {cadence} of 10 s samples · " + ", ".join(f"{m} ({METRIC_UNITS.get(m, '')})" for m in metrics),
        "# '!' lines are station events (starts/stops, warnings, errors); "
        "'~' lines are samples the quality guards flagged (raw value kept, not averaged)",
    ]
    widths = {m: max(len(m), 6) for m in metrics}
    header = f"{'time':<{len(stamp(start))}}  " + "  ".join(f"{m:>{widths[m]}}" for m in metrics)
    lines.append(header)

    entries: List[Tuple[int, int, str]] = []  # (ts, order, text); order keeps events before the bucket row
    for event in events:
        if not (event["level"] in ("warning", "error") or event["source"] == "collector"):
            continue
        entries.append((
            event["created_at_ts"], 0,
            f"! {event['source']}: {event['message']}",
        ))
    for item in flagged:
        parts = []
        for metric, flag in item["flags"].items():
            if metrics and metric not in metrics:
                continue
            value = flag.get("value")
            parts.append(f"{metric} {_format_value(metric, value)} ({flag.get('reason', 'flagged')})")
        if parts:
            entries.append((item["recorded_at_ts"], 1, "~ " + "; ".join(parts)))
    for row in rows:
        ts = row.get("timestamp_ts")
        if ts is None:
            continue
        entries.append((
            ts, 2, "  ".join(f"{_format_value(m, row.get(m)):>{widths[m]}}" for m in metrics),
        ))
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    for ts, _order, text in entries:
        lines.append(f"{stamp(ts)}  {text}")
    if not rows:
        lines.append("(no samples in this range)")
    return "\n".join(lines) + "\n"


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

    def history_rows(start: int, end: int, bucket_seconds: int):
        """Bucketed rows + stats; picks raw vs rollup source per B19/R8 rules.

        Hour-or-coarser buckets read the rollup table (cheaper, and the only
        source once the range outlives raw retention); fine buckets stay raw
        — unless the window starts before the raw horizon, where only the
        rollups still hold data (a short range 6 months back must not come
        up empty just because it is short).
        """
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
        return rows, stats, bucket_seconds

    @app.get("/api/history")
    def api_history() -> Any:
        start, end = resolve_range(request.args)
        rows, stats, bucket_seconds = history_rows(
            start, end, choose_bucket_seconds((end - start) / 3600)
        )
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

    @app.get("/api/export.txt")
    def api_export_txt() -> Any:
        """Paste-sized plain text: bucketed values with events interleaved.

        Exists so the operator can hand a slice of history (with the
        reboots, warnings and flagged samples that explain it) to a person
        or a chat model without wrangling a CSV. `?metrics=co2` narrows the
        columns, `?bucket=300` overrides the automatic bucket.
        """
        start, end = resolve_range(request.args)
        metrics = parse_metrics(request.args.get("metrics"))
        if request.args.get("bucket") is not None:
            bucket_seconds = max(10, parse_int(request.args.get("bucket"), "bucket"))
        else:
            bucket_seconds = choose_text_bucket_seconds(end - start)
        rows, _stats, bucket_seconds = history_rows(start, end, bucket_seconds)
        text = render_text_export(
            start=start, end=end, bucket_seconds=bucket_seconds, metrics=metrics,
            rows=rows,
            events=database.query_events_range(start, end, limit=200),
            flagged=database.query_flagged_range(start, end, limit=200),
        )
        return Response(text, mimetype="text/plain; charset=utf-8")

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
