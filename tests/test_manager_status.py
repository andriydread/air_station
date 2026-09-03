"""manager_status shape and the frame / weather debug lines."""

from manager.display import Panel
from manager.machine import Machine, Sources
from manager.maintenance import Hourly, Nightly
from manager.network import WifiWatch
from manager.status import build_status, debug_frame_line, debug_weather_line
from tests.mocks.fake_devices import FakeRunner


def test_status_shape_is_frozen(db, log, tmp_config, tmp_path):
    runner = FakeRunner()
    runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x0\n")
    panel = Panel(log, driver_factory=lambda: (_ for _ in ()).throw(OSError("no spi")))
    machine = Machine(db, log, runner=runner, sources=Sources(data_dir=str(tmp_path)))
    machine.tick(now=100)
    wifi = WifiWatch(log, runner=runner, route_path=str(tmp_path / "none"))
    nightly = Nightly(db, log, tmp_config)
    hourly = Hourly(db, log)
    status = build_status(
        started_at=0, now=120, log_failures=0, panel=panel,
        weather_state={"ok": True, "fetched_at": 50, "error": None, "pressure_hpa": 1001.2, "fetches": 1},
        wifi=wifi, machine=machine, db=db, nightly=nightly, hourly=hourly,
        collector_state={"last_row_at": 90, "silent": False, "warming_up": False, "unhealthy": []},
    )
    assert set(status) == {"started_at", "uptime", "log_failures", "display", "weather", "wifi", "power",
                           "storage", "collector"}
    assert status["uptime"] == 120 and status["weather"]["pressure_hpa"] == 1001.2
    assert set(status["storage"]) == {"db_mb", "last_backup_at", "last_backup_mb", "last_prune_at",
                                      "last_rollup_hour", "vitals_write_failures"}
    assert status["collector"] == {"last_row_at": 90, "silent": False, "warming_up": False, "unhealthy": []}
    assert status["power"]["available"] is True and status["display"]["available"] is False
    assert set(status["wifi"]) >= {"router_ok", "internet_ok", "lan_ms", "wan_ms", "last_bounce_at"}


def test_debug_lines(log):
    doc = {"values": {"co2": 800, "temp": None, "pm25": 3.0}, "samples": {"co2": 6, "temp": 0, "pm25": 6},
           "aqi": 12, "aqi_short": "Good", "co2_category": "Good", "warming_up": False,
           "collector_silent": False, "weather": {"stale": True}, "glyphs": {"wifi": True, "power": False}}
    debug_frame_line(log, doc, "partial", 41.2, 900)
    debug_weather_line(log, True, 812.4, {"bytes": 4321, "hourly": {"time": [1] * 48}, "pressure_hpa": 1002.0,
                                         "timezone": "Europe/Kyiv"})
    debug_weather_line(log, False, 10000.0, error="WeatherError: timeout")
    log.close()
    lines = log.path.read_text().splitlines()
    frame = next(l for l in lines if " display frame " in l)
    assert "mode=partial" in frame and "metrics=co2,pm25" in frame and "samples=co2:6,pm25:6" in frame
    assert "glyphs=wifi" in frame and "weather_stale=1" in frame
    ok_line, bad_line = [l for l in lines if " weather fetch " in l]
    assert "ok=1" in ok_line and "hours=48" in ok_line and "bytes=4321" in ok_line
    assert "ok=0" in bad_line and 'error="WeatherError: timeout"' in bad_line
