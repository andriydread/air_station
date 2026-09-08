"""The manager's status document (every 30 s), the frame line (one per minute) and the weather line."""

from typing import Any, Dict, Optional


def build_status(*, started_at: float, now: float, log_failures: int, panel, weather_state: Dict[str, Any],
                 wifi, machine, db, nightly, hourly, collector_state: Dict[str, Any]) -> Dict[str, Any]:
    """The ``manager_status`` document, shape fixed in tasks.md → Interfaces."""
    return {
        "started_at": int(started_at),
        "uptime": int(now - started_at),
        "log_failures": int(log_failures),
        "display": panel.status(),
        "weather": {
            "ok": weather_state.get("ok"),
            "fetched_at": weather_state.get("fetched_at"),
            "error": weather_state.get("error"),
            "pressure_hpa": weather_state.get("pressure_hpa"),
            "fetches": weather_state.get("fetches", 0),
            "failures": weather_state.get("failures", 0),
        },
        "wifi": wifi.status(),
        "power": machine.status(),
        "storage": {
            "db_mb": db.size_mb(),
            "last_backup_at": nightly.last_run_at,
            "last_backup_mb": nightly.last_backup_mb,
            "last_prune_at": nightly.last_run_at,
            "last_rollup_hour": hourly.last_hour,
            "vitals_write_failures": machine.storage_failures,
        },
        "collector": {
            "last_row_at": collector_state.get("last_row_at"),
            "silent": bool(collector_state.get("silent")),
            "warming_up": bool(collector_state.get("warming_up")),
            "unhealthy": list(collector_state.get("unhealthy") or []),
        },
    }


def frame_line(log, doc: Dict[str, Any], mode: Optional[str], render_ms: Optional[float],
               busy_ms: Optional[float], because: Optional[str] = None) -> None:
    """One info line per frame: what the panel shows and how long it took.

    The exceptions, not the rule: ``samples=6`` when every metric has the same
    count, else the odd ones out and ``rest=``; ``missing=`` names the metrics
    without a value; ``because=`` says why the panel is on the start-up screen
    or shows the sensor glyph. A normal minute is one short line.
    """
    values = doc.get("values") or {}
    samples = doc.get("samples") or {}
    missing = [m for m, v in values.items() if v is None]
    if values and len(missing) == len(values):
        missing = ["all"]  # an empty minute: one word, not fifteen names
    counts = sorted(set(samples.values()))
    if len(counts) <= 1:
        sample_text = str(counts[0]) if counts else None
    else:
        common = max(counts, key=lambda c: list(samples.values()).count(c))
        odd = ",".join(f"{m}:{n}" for m, n in samples.items() if n != common)
        sample_text = f"{odd},rest:{common}"
    log.info("display", "frame", mode=mode or "skipped", render_ms=render_ms, busy_ms=busy_ms,
              samples=sample_text, missing=",".join(missing) or None,
              aqi=doc.get("aqi"), aqi_short=doc.get("aqi_short"), co2=doc.get("co2_category"),
              warming=doc.get("warming_up"), silent=doc.get("collector_silent"), because=because,
              weather_stale=(doc.get("weather") or {}).get("stale"),
              glyphs=",".join(k for k, v in (doc.get("glyphs") or {}).items() if v) or None)


def weather_line(log, ok: bool, ms: float, doc: Optional[Dict[str, Any]] = None,
                 error: Optional[str] = None) -> None:
    """One info line per fetch, good or bad (the first failure of a run is also an event)."""
    if ok and doc is not None:
        hourly = doc.get("hourly") or {}
        log.info("weather", "fetch", ok=True, ms=round(ms), bytes=doc.get("bytes"),
                 hours=len(hourly.get("time", [])), pressure_hpa=doc.get("pressure_hpa"),
                 timezone=doc.get("timezone"))
    else:
        log.info("weather", "fetch", ok=False, ms=round(ms), error=error)
