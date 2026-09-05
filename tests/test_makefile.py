"""The Makefile on the dev server: every operator target dry-runs (`make -n`),
the `_pi` guard refuses to run them here, `help` lists them all."""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OPERATOR = ("init", "deploy", "restart", "status", "logs", "export", "recovery", "delete-data")
AGENT = ("agent-venv", "agent-test", "agent-demo", "agent-demo-stop", "agent-import", "agent-clean")


def make(*args, **kwargs):
    return subprocess.run(["make", "-C", str(REPO), *args], capture_output=True, text=True, **kwargs)


@pytest.mark.parametrize("target", OPERATOR)
def test_operator_targets_dry_run(target):
    result = make("-n", target)
    assert result.returncode == 0, result.stderr
    assert "/etc/systemd/system" in result.stdout  # the guard is always first


def test_dry_run_recipes_do_not_recurse():
    # `$(MAKE)` lines run even under -n; the operator targets must not use them
    text = (REPO / "Makefile").read_text()
    assert "$(MAKE)" not in text


@pytest.mark.skipif(os.path.exists("/dev/i2c-1"), reason="this is a Pi")
@pytest.mark.parametrize("target", ("status", "restart", "init"))
def test_guard_refuses_to_run_off_the_pi(target):
    result = make(target)
    assert result.returncode != 0
    assert "runs ON the Pi" in result.stdout


def test_init_renders_units_and_sudoers_and_enables_four_units():
    out = make("-n", "init").stdout
    for unit in ("airstation-collector", "airstation-manager", "airstation-dashboard"):
        assert f"systemd/{unit}.service.in" in out or f"systemd/$unit.service.in" in out
    assert "visudo -c" in out and "/etc/sudoers.d/airstation" in out
    assert "enable-watchdog.sh" in out
    assert "journald-airstation.conf" in out and "/var/log/journal" in out
    assert "enable --now wifi-powersave-off airstation-collector airstation-manager airstation-dashboard" in out
    assert "/dev/spidev0.0" in out and "apt-get install" in out and "requirements.txt" in out


def test_deploy_installs_and_restarts_without_apt_or_watchdog():
    out = make("-n", "deploy").stdout
    assert "restart airstation-collector airstation-manager airstation-dashboard" in out
    assert "apt-get" not in out and "enable-watchdog" not in out
    assert "journald-airstation.conf" in out  # the journal drop-in lands on every deploy


def test_status_runs_the_status_tool():
    assert "python -m tools.status" in make("-n", "status").stdout


def test_help_lists_every_target():
    out = make("help").stdout
    listed = set(re.findall(r"^  ([a-z-]+)\s", out, re.M))
    assert listed >= set(OPERATOR) | set(AGENT) | {"help"}
    assert not any(name.startswith("_") for name in listed)
