"""Start/stop classification: reboot vs restart, clean vs killed, trigger."""

import logging
import subprocess

from airmonitor import lifecycle


def _status(running, uptime=7200, stop_reason=None, age=60, now=1_000_000):
    return {
        "value": {"running": running, "uptime_seconds": uptime, "stop_reason": stop_reason},
        "updated_at_ts": now - age,
    }


def test_first_start_on_record():
    context = lifecycle.describe_start(
        boot_id="b1", previous_boot_id=None, system_uptime=40.0, previous_status=None,
        recent_commands=[], unit_info={}, now=1_000_000,
    )
    assert context["level"] == logging.INFO
    assert context["message"].startswith("Air monitor started (first start on record)")
    assert context["details"]["rebooted"] is None
    assert context["details"]["trigger"] == "unknown"


def test_clean_restart_without_reboot_after_deploy():
    context = lifecycle.describe_start(
        boot_id="b1", previous_boot_id="b1", system_uptime=86400.0,
        previous_status=_status(False, stop_reason="SIGTERM", age=12),
        recent_commands=[], unit_info={"n_restarts": 0, "result": "success"}, now=1_000_000,
    )
    assert context["level"] == logging.INFO
    assert "restarted without a reboot" in context["message"]
    assert "stopped cleanly on SIGTERM after 2h 00m" in context["message"]
    assert "station silent for 12s" in context["message"]
    assert context["details"]["trigger"] == "service restart (deploy or manual)"
    assert context["details"]["previous_clean"] is True


def test_reboot_requested_from_the_dashboard():
    now = 1_000_000
    commands = [
        {"command": "sps30_force_clean", "status": "succeeded", "updated_at_ts": now - 5},
        {"command": "system_reboot", "status": "succeeded", "updated_at_ts": now - 90},
    ]
    context = lifecycle.describe_start(
        boot_id="b2", previous_boot_id="b1", system_uptime=48.0,
        previous_status=_status(False, stop_reason="SIGTERM", age=85),
        recent_commands=commands, unit_info={}, now=now,
    )
    assert "after a Pi reboot" in context["message"]
    assert "system up 48s" in context["message"]
    assert context["details"]["trigger"] == "dashboard command system_reboot"
    assert context["details"]["rebooted"] is True


def test_old_dashboard_command_is_not_the_trigger():
    now = 1_000_000
    commands = [{"command": "system_reboot", "status": "succeeded", "updated_at_ts": now - 3600}]
    context = lifecycle.describe_start(
        boot_id="b2", previous_boot_id="b1", system_uptime=30.0,
        previous_status=_status(True, age=120), recent_commands=commands, unit_info={}, now=now,
    )
    assert context["details"]["trigger_command"] is None
    assert context["details"]["trigger"].startswith("power loss or hard reset")
    assert context["level"] == logging.WARNING
    assert "previous run was killed after 2h 00m" in context["message"]


def test_watchdog_kill_is_named():
    context = lifecycle.describe_start(
        boot_id="b1", previous_boot_id="b1", system_uptime=5000.0,
        previous_status=_status(True, age=100), recent_commands=[],
        unit_info={"n_restarts": 3, "result": "watchdog"}, now=1_000_000,
    )
    assert context["level"] == logging.WARNING
    assert context["details"]["trigger"].startswith("systemd watchdog")
    assert context["details"]["systemd"] == {"n_restarts": 3, "result": "watchdog"}


def test_killed_without_reboot_is_a_crash():
    context = lifecycle.describe_start(
        boot_id="b1", previous_boot_id="b1", system_uptime=5000.0,
        previous_status=_status(True), recent_commands=[], unit_info={}, now=1_000_000,
    )
    assert context["details"]["trigger"] == "process killed (crash, watchdog or OOM)"


def test_proc_readers_tolerate_missing_files(tmp_path):
    assert lifecycle.read_boot_id(str(tmp_path / "nope")) is None
    assert lifecycle.read_system_uptime(str(tmp_path / "nope")) is None
    boot = tmp_path / "boot_id"
    boot.write_text("8f2c1a3e-0000-4000-8000-000000000000\n")
    assert lifecycle.read_boot_id(str(boot)) == "8f2c1a3e-0000-4000-8000-000000000000"
    uptime = tmp_path / "uptime"
    uptime.write_text("12345.67 40000.00\n")
    assert lifecycle.read_system_uptime(str(uptime)) == 12345.67


def test_systemd_unit_info_parses_and_degrades():
    def fake_runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="NRestarts=2\nResult=watchdog\n", stderr=""
        )

    assert lifecycle.systemd_unit_info(runner=fake_runner) == {"n_restarts": 2, "result": "watchdog"}

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("systemctl")

    assert lifecycle.systemd_unit_info(runner=missing) == {}
