"""The collector's status document (every 30 s) and its bench-period debug lines."""

from typing import Any, Dict, Optional

from shared.db import METRICS


def build_status(sampler, started_at: float, now: float, log_failures: int,
                 last_calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ``collector_status`` document, shape fixed in tasks.md → Interfaces."""
    scd41, sht41, sps30 = sampler.scd41, sampler.sht41, sampler.sps30
    return {
        "started_at": int(started_at),
        "uptime": int(now - started_at),
        "sample_count": sampler.sample_count,
        "log_failures": int(log_failures),
        "storage_failures": sampler.storage_failures,
        "bus_reinits": sampler.bus_reinits,
        "asc": bool(scd41.asc),
        "pressure_hpa": scd41.pressure_hpa,
        "sensors": {
            "i2c": {
                "available": True, "healthy": sampler.bus_reinits == 0 or any(
                    s.device is not None for s in sampler.sensors),
                "last_error": None, "last_ok_at": None, "warmup_left": 0,
                "reinit_count": sampler.bus_reinits, "id": None,
            },
            "scd41": scd41.status(now),
            "sht41": sht41.status(now),
            "sps30": sps30.status(now),
        },
        "calibration": {**scd41.calibration_readiness(now), "last": last_calibration},
    }


def debug_sample_lines(log, record: Dict[str, Any]) -> None:
    """One ``sample`` line per sensor with everything the bench wants to see."""
    raw = record.get("raw", {})
    row = record.get("row", {})
    extra = record.get("extra", {})
    dropped = record.get("dropped", {})
    per_sensor = {
        "scd41": ("co2", "co2_temp", "co2_humid"),
        "sht41": ("temp", "humid"),
        "sps30": ("pm1", "pm25", "pm10", "tps", "nc05", "nc1", "nc25"),
    }
    for sensor, metrics in per_sensor.items():
        if sensor not in record.get("present", []):
            continue
        kv: Dict[str, Any] = {"ts": record["ts"]}
        for metric in metrics:
            kv[metric] = raw.get(metric)
            kv[f"{metric}_ok"] = metric in row and row.get(metric) is not None
        if sensor == "sps30":
            kv.update({f"x_{name}": value for name, value in extra.items()})
            status = record.get("sps30_status")
            if status:
                kv.update({f"st_{key}": value for key, value in status.items()})
        kv["dropped"] = ",".join(f"{m}:{dropped[m][1]}" for m in metrics if m in dropped) or None
        kv["read_ms"] = record.get("read_ms", {}).get(sensor)
        kv["data_ready"] = record.get("data_ready", {}).get(sensor)
        kv["warmup_left"] = record.get("warmup_left", {}).get(sensor)
        kv["error"] = record.get("errors", {}).get(sensor)
        kv["errno"] = record.get("errno", {}).get(sensor)
        log.debug(sensor, "sample", **kv)
    missing = [m for m in METRICS if row.get(m) is None]
    log.debug("sample", "row", ts=record["ts"], empty=",".join(missing) or None,
              raised=",".join(record.get("raised", [])) or None)
