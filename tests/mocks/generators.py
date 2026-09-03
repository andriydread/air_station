"""Plausible, drifting sensor values for the demo and for seeding charts.

``World.sample(t)`` returns what the three sensors would say at Unix time
``t``: a CO2 day curve with ventilation drops, a slow temperature/humidity
swing, a dust baseline with cooking-like bumps, and — every so often — a
garbage value so the drop path is exercised. Deterministic for a given
seed, so screenshots are reproducible.
"""

import math
import random
from typing import Any, Dict, Optional

from tests.mocks.fake_devices import FakeScd41Device, FakeSht41Device, FakeSps30Device

DAY = 86400.0


class World:
    def __init__(self, seed: int = 7):
        self.seed = seed

    def _noise(self, t: float, salt: int, scale: float) -> float:
        return random.Random(int(t) * 31 + salt + self.seed).uniform(-scale, scale)

    def sample(self, t: float) -> Dict[str, Any]:
        day = (t % DAY) / DAY                       # 0 at midnight (UTC), 0.5 at noon
        occupancy = max(0.0, math.sin((day - 0.25) * 2 * math.pi))  # people around from morning to evening
        co2 = 450 + 500 * occupancy + 60 * math.sin(t / 900) + self._noise(t, 1, 12)
        temp = 21.5 + 2.0 * math.sin((day - 0.35) * 2 * math.pi) + self._noise(t, 2, 0.08)
        humid = 42 + 6 * math.cos((day - 0.1) * 2 * math.pi) + self._noise(t, 3, 0.6)
        bump = 12 * math.exp(-((t % 7200) / 60 - 20) ** 2 / 30)  # a dust bump every two hours
        pm25 = max(0.3, 3.0 + bump + self._noise(t, 4, 0.4))
        garbage = int(t) % 1990 == 0                # roughly every 5.5 hours at 10 s beats
        return {
            "co2": 0.0 if garbage else round(co2, 1),
            "co2_temp": round(temp + 1.4, 2),
            "co2_humid": round(humid - 2.5, 2),
            "temp": round(temp, 2),
            "humid": round(humid, 2),
            "pm1": round(pm25 * 0.75, 3),
            "pm25": round(pm25, 3),
            "pm4": round(pm25 * 1.12, 3),
            "pm10": round(pm25 * 1.18, 3),
            "tps": round(0.5 + 0.05 * math.sin(t / 3000), 3),
            "nc05": round(pm25 * 5.2, 2),
            "nc10": round(pm25 * 6.1, 2),
            "nc25": round(pm25 * 6.25, 2),
            "nc40": round(pm25 * 6.3, 2),
            "nc100": round(pm25 * 6.3, 2),
        }

    def row(self, t: float) -> Dict[str, Any]:
        """The 12 row metrics (filtered the way the collector would: garbage → None)."""
        s = self.sample(t)
        return {
            "co2": None if s["co2"] < 350 else int(round(s["co2"])),
            "co2_temp": s["co2_temp"], "co2_humid": s["co2_humid"],
            "temp": s["temp"], "humid": s["humid"],
            "pm1": s["pm1"], "pm25": s["pm25"], "pm10": s["pm10"], "tps": s["tps"],
            "nc05": s["nc05"], "nc1": s["nc10"], "nc25": s["nc25"],
        }

    def vitals(self, t: float, collector_lag: int = 4) -> Dict[str, Any]:
        day = (t % DAY) / DAY
        return {
            "recorded_at": int(t),
            "cpu_temp": round(46 + 6 * math.sin(day * 2 * math.pi) + self._noise(t, 5, 0.5), 1),
            "load": round(0.25 + 0.1 * math.sin(t / 700) + abs(self._noise(t, 6, 0.05)), 2),
            "mem_free": int(210 + 15 * math.sin(t / 5000)),
            "disk_free": int(11800 - (t % DAY) / DAY * 3),
            "db_size": round(40 + (t % (30 * DAY)) / DAY * 1.1, 1),
            "wifi_rssi": int(-58 + 6 * math.sin(t / 1300) + self._noise(t, 7, 2)),
            "wifi_link": round(43.3 + 20 * max(0.0, math.sin(t / 4100)), 1),
            "lan_ms": round(2.5 + abs(self._noise(t, 8, 1.5)), 1),
            "wan_ms": round(17 + 8 * abs(math.sin(t / 2300)) + abs(self._noise(t, 9, 3)), 1),
            "throttled": 0x50005 if 0.62 < day < 0.64 else 0,   # a short brown-out mid-afternoon
            "uptime": int(t % (7 * DAY)),
            "collector_lag": collector_lag,
        }


class GeneratedScd41(FakeScd41Device):
    def __init__(self, world: World, clock=None):
        super().__init__()
        self.world = world
        self.clock = clock

    def _now(self) -> float:
        import time
        return self.clock() if self.clock else time.time()

    @property
    def CO2(self) -> float:  # noqa: N802 (Adafruit API)
        return self.world.sample(self._now())["co2"]

    @property
    def temperature(self) -> float:
        return self.world.sample(self._now())["co2_temp"]

    @temperature.setter
    def temperature(self, _value):
        pass

    @property
    def relative_humidity(self) -> float:
        return self.world.sample(self._now())["co2_humid"]

    @relative_humidity.setter
    def relative_humidity(self, _value):
        pass


class GeneratedSht41(FakeSht41Device):
    def __init__(self, world: World, clock=None):
        super().__init__()
        self.world = world
        self.clock = clock

    def _now(self) -> float:
        import time
        return self.clock() if self.clock else time.time()

    @property
    def temperature(self) -> float:
        return self.world.sample(self._now())["temp"]

    @property
    def relative_humidity(self) -> float:
        return self.world.sample(self._now())["humid"]


class GeneratedSps30(FakeSps30Device):
    def __init__(self, world: World, clock=None):
        super().__init__()
        self.world = world
        self.clock = clock

    def read(self) -> Dict[str, float]:
        import time
        s = self.world.sample(self.clock() if self.clock else time.time())
        return {k: s[k] for k in ("pm1", "pm25", "pm4", "pm10", "tps", "nc05", "nc10", "nc25", "nc40", "nc100")}


def install_generated_devices(world: Optional[World] = None):
    """Point the fake Adafruit modules at generated devices; returns an SPS30 factory."""
    import sys

    world = world or World()
    sys.modules["adafruit_scd4x"].SCD4X = lambda _i2c: GeneratedScd41(world)
    sys.modules["adafruit_sht4x"].SHT4x = lambda _i2c: GeneratedSht41(world)
    return lambda _i2c: GeneratedSps30(world)


def seed_history(db, hours: float, now: Optional[float] = None, world: Optional[World] = None,
                 raw_hours: Optional[float] = None) -> Dict[str, int]:
    """Fill raw rows (every 10 s), hourly rollups and vitals (every minute) for the past ``hours``.

    ``raw_hours`` limits how far back 10 s rows go (default: all of it, capped
    at 30 days by the caller's patience); older hours get hourly rows only.
    """
    import time

    world = world or World()
    now = int(now if now is not None else time.time())
    start = now - int(hours * 3600)
    raw_start = now - int((raw_hours if raw_hours is not None else hours) * 3600)
    counts = {"raw": 0, "hourly": 0, "vitals": 0}
    statements = []
    from shared.db import METRICS
    columns = ", ".join(("recorded_at", *METRICS))
    marks = ", ".join("?" for _ in range(len(METRICS) + 1))
    t = (raw_start // 10) * 10
    while t <= now:
        row = world.row(t)
        statements.append((f"INSERT OR REPLACE INTO raw_measurements ({columns}) VALUES ({marks})",
                           [t, *(row.get(m) for m in METRICS)]))
        counts["raw"] += 1
        t += 10
    db.write_many(statements)
    # hourly rows for hours before the raw window come from the generator directly
    hour = (start // 3600) * 3600
    while hour + 3600 <= raw_start:
        samples = [world.row(hour + i * 10) for i in range(360)]
        cols = ["hour", "samples"]
        params = [hour, 360]
        for m in METRICS:
            values = [s[m] for s in samples if s[m] is not None]
            cols += [f"{m}_min", f"{m}_max", f"{m}_avg"]
            params += [min(values), max(values), sum(values) / len(values)] if values else [None, None, None]
        db.write(f"INSERT OR REPLACE INTO hourly_measurements ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", params)
        counts["hourly"] += 1
        hour += 3600
    counts["hourly"] += db.rollup_catchup(now)["rolled"]
    vitals = []
    t = (start // 60) * 60
    while t <= now:
        v = world.vitals(t)
        vitals.append(("INSERT OR REPLACE INTO vitals (recorded_at, cpu_temp, load, mem_free, disk_free, db_size, "
                       "wifi_rssi, wifi_link, lan_ms, wan_ms, throttled, uptime, collector_lag) VALUES "
                       "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [v[k] for k in ("recorded_at", "cpu_temp", "load", "mem_free", "disk_free", "db_size",
                                       "wifi_rssi", "wifi_link", "lan_ms", "wan_ms", "throttled", "uptime",
                                       "collector_lag")]))
        counts["vitals"] += 1
        t += 60
    db.write_many(vitals)
    return counts
