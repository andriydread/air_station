"""`tools/status.py` against a seeded database with systemd faked."""

import os
import time as _time

import pytest

from tests.mocks.fake_devices import FakeRunner
from tools import status

NOW = 1_800_000_000.0  # 2027-01-15 08:00:00 UTC


def systemd(runner: FakeRunner, states=None):
    states = states or {}
    for unit in status.UNITS:
        active, sub = states.get(unit, ("active", "running"))
        runner.results[("systemctl", "show", "-p", "ActiveState,SubState,ActiveEnterTimestamp", unit)] = \
            FakeRunner.Completed(stdout=f"ActiveState={active}\nSubState={sub}\n"
                                        f"ActiveEnterTimestamp=Tue 2027-01-12 09:12:03 EET\n")
    return runner


@pytest.fixture
def kyiv(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    yield
    monkeypatch.delenv("TZ")
    _time.tzset()


def test_ages():
    assert status.age(None, NOW) == "never"
    assert status.age(NOW - 4, NOW) == "4 s ago"
    assert status.age(NOW - 130, NOW) == "2 min ago"
    assert status.age(NOW - 7200, NOW) == "2 h ago"
    assert status.age(NOW - 3 * 86400, NOW) == "3 d ago"


def test_full_screen_from_a_seeded_database(tmp_config, db, kyiv):
    db.insert_raw(int(NOW) - 4, {"co2": 812, "temp": 22.5})
    db._now = lambda: NOW - 22  # state rows stamp themselves with the db clock
    db.set_state("display_data", {"values": {}})
    db.insert_vitals({"recorded_at": int(NOW) - 22, "cpu_temp": 51.2})
    db.insert_event("manager", "info", "storage", "rollup", "hour 07:00 · 360 samples", ts=int(NOW) - 3600)
    db.insert_event("manager", "warning", "wifi", "internet_down", "wan probe failed (3 in a row)",
                    ts=int(NOW) - 60)
    db.close()
    backup = str(tmp_config.paths.database) + ".bak"
    open(backup, "wb").close()
    os.utime(backup, (NOW - 8 * 3600, NOW - 8 * 3600))  # Fri 02:00 local → "Fri 02:00"

    text = status.render(tmp_config, runner=systemd(FakeRunner()), now=NOW, hostname="airstation")
    lines = text.splitlines()
    assert lines[0] == "airstation-collector   active (running) since Tue 09:12 · last raw row 4 s ago"
    assert lines[1] == "airstation-manager     active (running) since Tue 09:12 · display_data 22 s ago · vitals 22 s ago"
    assert lines[2] == "airstation-dashboard   active (running) since Tue 09:12 · http://airstation.local:8080"
    assert lines[3].startswith("database 0.") and " MB · backup Fri 02:00 · disk free " in lines[3]
    assert " GB · log level debug · commit " in lines[3]
    assert lines[4] == "last events:"
    assert lines[5].startswith("  Fri 09:59  warning  manager    wifi        internet_down    wan probe failed")
    assert lines[6].startswith("  Fri 09:00  info     manager    storage     rollup           hour 07:00")


def test_missing_database_is_reported_not_created(tmp_config):
    runner = systemd(FakeRunner(), {"airstation-manager": ("failed", "failed")})
    text = status.render(tmp_config, runner=runner, now=NOW, hostname="pi")
    assert not tmp_config.paths.database.exists()
    assert "last raw row never" in text and "display_data never · vitals never" in text
    assert "airstation-manager     failed (failed) since Tue 09:12" in text
    assert "database not created yet" in text and "backup none" in text and "  (none)" in text


def test_no_systemd_here_is_not_an_error(tmp_config):
    runner = FakeRunner()
    runner.results["systemctl"] = FileNotFoundError("systemctl")
    text = status.render(tmp_config, runner=runner, now=NOW, hostname="pi")
    assert "unknown (FileNotFoundError)" in text
    runner.results["systemctl"] = FakeRunner.Completed(returncode=1)
    assert "unknown (systemctl exit 1)" in status.render(tmp_config, runner=runner, now=NOW, hostname="pi")


def test_main_prints_the_screen(tmp_config, capsys, monkeypatch):
    monkeypatch.setattr(status, "Config", type("C", (), {"load": staticmethod(lambda _p: tmp_config)}))
    assert status.main(["--config", "x"]) == 0
    assert "airstation-dashboard" in capsys.readouterr().out
