"""Data-quality guards: spike flagging and sensor cross-checking.

The operator's core complaint was "I don't trust that data". These guards
make trust inspectable: a physically implausible jump is stored as a
*flagged* raw value (not silently averaged into the charts), and two
sensors that measure the same thing are compared so a silently drifting
sensor gets reported — the one failure a sensor can never self-report.
"""

import logging
import time
from typing import Any, Dict, Optional, Tuple

LOGGER = logging.getLogger("airmonitor")

# Maximum plausible change per second of elapsed time between samples.
# Tuned generously: a window opened onto a smoky street must NOT flag more
# than the first sample of the step change.
MAX_DELTA_PER_SECOND = {
    "co2": 40.0,    # 400 ppm per 10s sample
    "temp": 0.2,    # 2.0 C per 10s
    "humid": 1.0,   # 10 % per 10s
    "pm1": 10.0,
    "pm25": 10.0,
    "pm4": 10.0,
    "pm10": 10.0,
    "tps": 0.5,
}

# Log the first flagged sample of a streak, then every 6th (like the SCD41
# invalid-reading logging), so a noisy sensor can't flood the events table.
_FLAG_LOG_EVERY = 6


class RateGuard:
    """Splits a sample into accepted values and flagged outliers.

    A flagged value still becomes the new baseline: a genuine step change
    flags exactly one sample, then readings are accepted again. Only a
    value that keeps jumping wildly keeps getting flagged.
    """

    def __init__(self, events, limits: Optional[Dict[str, float]] = None):
        self.events = events
        self.limits = dict(MAX_DELTA_PER_SECOND if limits is None else limits)
        self._last: Dict[str, Tuple[float, float]] = {}  # metric -> (monotonic, value)
        self._flag_streak: Dict[str, int] = {}

    def filter(self, sample: Dict[str, float]) -> Tuple[Dict[str, float], Dict[str, Any]]:
        accepted: Dict[str, float] = {}
        flags: Dict[str, Any] = {}
        now = time.monotonic()
        for metric, value in sample.items():
            limit = self.limits.get(metric)
            previous = self._last.get(metric)
            self._last[metric] = (now, value)
            if limit is None or previous is None:
                accepted[metric] = value
                self._clear_streak(metric)
                continue
            elapsed = max(now - previous[0], 1.0)
            allowed = limit * elapsed
            delta = value - previous[1]
            if abs(delta) > allowed:
                flags[metric] = {
                    "value": value,
                    "reason": (
                        f"jumped {delta:+.2f} in {elapsed:.0f}s "
                        f"(limit ±{allowed:.2f})"
                    ),
                }
                self._log_flag(metric, flags[metric])
            else:
                accepted[metric] = value
                self._clear_streak(metric)
        return accepted, flags

    def _log_flag(self, metric: str, flag: Dict[str, Any]) -> None:
        streak = self._flag_streak.get(metric, 0) + 1
        self._flag_streak[metric] = streak
        if streak == 1 or streak % _FLAG_LOG_EVERY == 0:
            self.events.log(
                logging.WARNING, "quality", "sample_flagged",
                f"{metric} reading flagged: {flag['reason']} ({streak} in a row)",
                {"metric": metric, "streak": streak, **flag},
            )

    def _clear_streak(self, metric: str) -> None:
        if self._flag_streak.get(metric):
            self.events.log(
                logging.INFO, "quality", "sample_flag_cleared",
                f"{metric} readings back within plausible range",
                {"metric": metric},
            )
        self._flag_streak[metric] = 0


class CrossCheck:
    """Compares SHT41 ambient readings against the SCD41's own sensors.

    Sustained disagreement means one of them is lying — which one is for
    the operator to determine, but the *fact* becomes an event instead of
    silently poisoning months of history.
    """

    def __init__(
        self,
        events,
        temp_delta: float = 4.0,
        humid_delta: float = 15.0,
        after_samples: int = 30,
    ):
        self.events = events
        self.temp_delta = temp_delta
        self.humid_delta = humid_delta
        self.after_samples = after_samples
        self.streak = 0
        self.reported = False

    def compare(
        self,
        sht_temp: Optional[float],
        sht_humid: Optional[float],
        scd_temp: Optional[float],
        scd_humid: Optional[float],
    ) -> None:
        if None in (sht_temp, sht_humid, scd_temp, scd_humid):
            return
        temp_diff = abs(sht_temp - scd_temp)
        humid_diff = abs(sht_humid - scd_humid)
        if temp_diff > self.temp_delta or humid_diff > self.humid_delta:
            self.streak += 1
            if self.streak >= self.after_samples and not self.reported:
                self.reported = True
                self.events.log(
                    logging.WARNING, "quality", "sensor_disagreement",
                    f"SHT41 and SCD41 disagree for {self.streak} samples: "
                    f"temp diff {temp_diff:.1f}C, humidity diff {humid_diff:.1f}%",
                    {
                        "sht41_temp": sht_temp, "scd41_temp": scd_temp,
                        "sht41_humid": sht_humid, "scd41_humid": scd_humid,
                        "samples": self.streak,
                    },
                )
        else:
            if self.reported:
                self.events.log(
                    logging.INFO, "quality", "sensor_agreement_restored",
                    "SHT41 and SCD41 agree again",
                )
            self.streak = 0
            self.reported = False
