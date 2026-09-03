"""Power bits and the vitals row from fake proc files and a scripted runner."""

import pytest

from manager.machine import CPU_HOT_C, Machine, Sources, flag_names, parse_throttled, read_rssi
from tests.mocks.fake_devices import FakeRunner

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   49.  -61.  -256        0      0      0      0     45        0
"""


@pytest.fixture
def sources(tmp_path):
    (tmp_path / "thermal").write_text("48250\n")
    (tmp_path / "loadavg").write_text("0.31 0.20 0.15 1/123 4567\n")
    (tmp_path / "meminfo").write_text("MemTotal:  420000 kB\nMemFree: 100000 kB\nMemAvailable: 215040 kB\n")
    (tmp_path / "uptime").write_text("86520.12 170000.00\n")
    (tmp_path / "wireless").write_text(WIRELESS)
    return Sources(thermal=str(tmp_path / "thermal"), loadavg=str(tmp_path / "loadavg"),
                   meminfo=str(tmp_path / "meminfo"), uptime=str(tmp_path / "uptime"),
                   wireless=str(tmp_path / "wireless"), data_dir=str(tmp_path), interface="wlan0")


@pytest.fixture
def runner():
    r = FakeRunner()
    r.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x0\n")
    r.results["iw"] = FakeRunner.Completed(stdout="Connected to aa:bb\n\ttx bitrate: 43.3 MBit/s MCS 4\n")
    return r


class _Net:
    last_lan_ms = 3.2
    last_wan_ms = 18.0


def test_parse_and_names():
    raw = parse_throttled("throttled=0x50005\n")
    assert raw == 0x50005
    assert flag_names(raw) == ["undervoltage_now".removesuffix("_now"), "throttled_now".removesuffix("_now")] or \
        flag_names(raw) == ["undervoltage", "throttled"]
    assert flag_names(raw, since_boot=True) == ["undervoltage", "throttled"]
    assert flag_names(0) == [] and flag_names(None) == []
    assert read_rssi.__name__ == "read_rssi"


def test_vitals_row_from_fake_files(db, log, sources, runner):
    db.insert_raw(1000, {"co2": 700})
    machine = Machine(db, log, network=_Net(), runner=runner, sources=sources)
    row = machine.tick(now=1004)
    assert row["cpu_temp"] == 48.2 and row["load"] == 0.31 and row["mem_free"] == 210
    assert row["uptime"] == 86520 and row["wifi_rssi"] == -61 and row["wifi_link"] == 43.3
    assert row["lan_ms"] == 3.2 and row["wan_ms"] == 18.0 and row["throttled"] == 0
    assert row["collector_lag"] == 4 and row["disk_free"] > 0 and row["db_size"] >= 0
    assert db.latest_vitals()["recorded_at"] == 1004
    assert machine.glyph() is False and machine.status()["available"] is True


def test_missing_sources_and_tools_give_nulls(db, log, tmp_path):
    runner = FakeRunner()
    runner.results["iw"] = FileNotFoundError("no iw")
    runner.results["vcgencmd"] = FileNotFoundError("no vcgencmd")
    machine = Machine(db, log, runner=runner, sources=Sources(
        thermal=str(tmp_path / "nope"), loadavg=str(tmp_path / "nope"), meminfo=str(tmp_path / "nope"),
        uptime=str(tmp_path / "nope"), wireless=str(tmp_path / "nope"), data_dir=str(tmp_path)))
    row = machine.tick(now=10)
    for key in ("cpu_temp", "load", "mem_free", "uptime", "wifi_rssi", "wifi_link", "throttled",
                "lan_ms", "wan_ms", "collector_lag"):
        assert row[key] is None, key
    assert machine.status()["available"] is False and machine.glyph() is False


def test_power_events_on_change_only(db, log, sources, runner):
    machine = Machine(db, log, runner=runner, sources=sources)
    machine.tick(now=0)
    runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x50005\n")
    machine.tick(now=60)
    machine.tick(now=120)  # unchanged: no second event
    runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x50000\n")  # only since-boot left
    machine.tick(now=180)
    types = [e["type"] for e in db.recent_events() if e["source"] == "power"]
    assert types == ["power_ok", "power_issue"]
    issue = [e for e in db.recent_events() if e["type"] == "power_issue"][0]
    assert issue["details"]["now"] == ["undervoltage", "throttled"]
    assert machine.status() == {"now": [], "since_boot": ["undervoltage", "throttled"],
                                "raw": 0x50000, "available": True}
    assert machine.glyph() is False


def test_power_issue_present_at_the_first_tick_is_reported(db, log, sources, runner):
    runner.results["vcgencmd"] = FakeRunner.Completed(stdout="throttled=0x1\n")
    Machine(db, log, runner=runner, sources=sources).tick(now=0)
    assert db.recent_events()[0]["type"] == "power_issue"


def test_threshold_events_fire_once_per_episode(db, log, sources, runner, tmp_path):
    machine = Machine(db, log, runner=runner, sources=sources)
    (tmp_path / "thermal").write_text(f"{int((CPU_HOT_C + 1) * 1000)}\n")
    machine.tick(now=0)
    machine.tick(now=60)
    (tmp_path / "thermal").write_text("50000\n")
    machine.tick(now=120)
    (tmp_path / "thermal").write_text(f"{int((CPU_HOT_C + 2) * 1000)}\n")
    machine.tick(now=180)
    assert [e["type"] for e in db.recent_events() if e["type"] == "cpu_hot"] == ["cpu_hot", "cpu_hot"]
    (tmp_path / "meminfo").write_text("MemAvailable: 20000 kB\n")
    machine.tick(now=240)
    machine.tick(now=300)
    assert len([e for e in db.recent_events() if e["type"] == "memory_low"]) == 1


def test_vitals_write_failure_is_counted(db, log, sources, runner, monkeypatch):
    machine = Machine(db, log, runner=runner, sources=sources)

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "insert_vitals", boom)
    machine.tick(now=0)
    assert machine.storage_failures == 1
