"""Makefile sanity checks. The Pi targets are only dry-run here."""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PI_TARGETS = ("init", "deploy", "restart", "status", "logs", "export", "recovery", "delete-data")


def make(*args):
    return subprocess.run(["make", "-C", str(REPO), *args], capture_output=True, text=True)


@pytest.mark.parametrize("target", PI_TARGETS)
def test_pi_targets_dry_run(target):
    result = make("-n", target)
    assert result.returncode == 0, result.stderr
    assert "/etc/systemd/system" in result.stdout  # the _pi guard runs first


@pytest.mark.skipif(os.path.exists("/dev/i2c-1"), reason="running on the Pi")
def test_guard_refuses_off_the_pi():
    result = make("status")
    assert result.returncode != 0 and "runs ON the Pi" in result.stdout


def test_init_installs_everything():
    out = make("-n", "init").stdout
    for unit in ("airstation-collector", "airstation-manager", "airstation-dashboard"):
        assert unit in out
    assert "apt-get install" in out and "requirements.txt" in out
    assert "visudo -c" in out and "enable-watchdog.sh" in out and "journald-airstation.conf" in out


def test_deploy_skips_apt_and_watchdog():
    out = make("-n", "deploy").stdout
    assert "restart airstation-collector" in out
    assert "apt-get" not in out and "enable-watchdog" not in out


def test_readme_only_names_real_targets():
    targets = set(re.findall(r"^([a-z][a-z-]*):", (REPO / "Makefile").read_text(), re.M))
    named = set(re.findall(r"(?:`|^)make ([a-z][a-z-]*)", (REPO / "README.md").read_text(), re.M))
    assert named <= targets, named - targets
