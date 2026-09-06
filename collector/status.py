"""The collector's status document (every 30 s)."""

from typing import Any, Dict, Optional


def build_status(sampler, started_at: float, now: float, log_failures: int,
                 last_calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The ``collector_status`` document (Interfaces contract; ``ready_at`` since 2026-09-06)."""
    scd41, sht41, sps30 = sampler.scd41, sampler.sht41, sampler.sps30
    ready = [s.ready_at for s in sampler.sensors if s.device is not None and s.ready_at]
    return {
        "started_at": int(started_at),
        "uptime": int(now - started_at),
        "ready_at": max(ready) if ready else None,  # when every present sensor is past its quiet time
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
                "last_error": None, "last_ok_at": None, "ready_at": None,
                "reinit_count": sampler.bus_reinits, "id": None,
            },
            "scd41": scd41.status(),
            "sht41": sht41.status(),
            "sps30": sps30.status(),
        },
        "calibration": {**scd41.calibration_readiness(now), "last": last_calibration},
    }
