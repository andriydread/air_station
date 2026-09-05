"""The generators behind the demo and chart seeding."""

from shared.db import METRICS
from tests.mocks.generators import (
    GeneratedScd41, GeneratedSht41, GeneratedSps30, World, install_generated_devices, seed_history,
)

T = 1_788_436_800  # 2026-09-03 12:00 UTC


def test_sample_has_every_sensor_key_and_plausible_values():
    s = World().sample(T)
    assert set(s) >= {"co2", "co2_temp", "co2_humid", "temp", "humid", "pm1", "pm25", "pm4", "pm10",
                      "tps", "nc05", "nc10", "nc25", "nc40", "nc100"}
    assert 400 <= s["co2"] <= 1100 and 18 <= s["temp"] <= 25 and 30 <= s["humid"] <= 55
    assert 0 < s["pm25"] < 30 and s["pm10"] > s["pm25"] > s["pm1"]


def test_row_uses_the_fifteen_metrics_and_drops_garbage():
    world = World()
    row = world.row(T)
    assert set(row) == set(METRICS) and row["nc1"] == world.sample(T)["nc10"]
    garbage_t = next(t for t in range(T, T + 20000, 30) if world.sample(t)["co2"] == 0.0)
    assert world.row(garbage_t)["co2"] is None


def test_values_drift_and_are_deterministic():
    world = World()
    a, b = world.sample(T), world.sample(T + 3 * 3600)
    assert a["co2"] != b["co2"] and World().sample(T) == a


def test_generated_devices_answer_like_the_real_ones():
    world = World()
    clock = lambda: T  # noqa: E731
    scd = GeneratedScd41(world, clock)
    assert scd.data_ready and scd.CO2 == world.sample(T)["co2"] and scd.temperature == world.sample(T)["co2_temp"]
    assert GeneratedSht41(world, clock).temperature == world.sample(T)["temp"]
    assert set(GeneratedSps30(world, clock).read()) == {"pm1", "pm25", "pm4", "pm10", "tps",
                                                         "nc05", "nc10", "nc25", "nc40", "nc100"}
    factory = install_generated_devices(world)
    import sys
    assert isinstance(sys.modules["adafruit_scd4x"].SCD4X(object()), GeneratedScd41)
    assert isinstance(factory(object()), GeneratedSps30)


def test_seed_history_counts(db):
    counts = seed_history(db, hours=3, now=T + 1800, raw_hours=1)
    assert counts["raw"] == 121                    # one hour of 30 s rows, both ends inclusive
    assert counts["vitals"] == 181                 # three hours of minutes
    assert counts["hourly"] == 3                   # two generated + one rolled from raw
    hours = [r["hour"] for r in db.hourly_between(0, 10**10)]
    assert hours == [T - 3600 * 3, T - 3600 * 2, T - 3600]  # 09:00, 10:00, 11:00; 12:00 is still open
    assert db.latest_vitals()["recorded_at"] == T + 1800
    assert db.raw_between(0, 10**10)[0]["recorded_at"] == T + 1800 - 3600
