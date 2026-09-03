"""The unit templates and the sudoers template: render like `make init` does
(sed on @USER@/@REPO@) and check the result is what the plan promises."""

import re
from pathlib import Path

import pytest

from manager import commands as manager_commands
from manager import network

SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"
APPS = ("collector", "manager", "dashboard")
USER, REPO = "pi", "/home/pi/air_station"


def render(name: str) -> str:
    text = (SYSTEMD / name).read_text()
    return text.replace("@USER@", USER).replace("@REPO@", REPO)


def unit(app: str) -> dict:
    """Parse a rendered unit into {section: {key: value}} (a key may repeat: last wins)."""
    sections, current = {}, None
    for raw in render(f"airstation-{app}.service.in").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None and "=" in line, f"{app}: stray line {raw!r}"
        key, value = line.split("=", 1)
        current[key] = value
    return sections


@pytest.mark.parametrize("app", APPS)
def test_unit_renders_to_plain_key_value_lines(app):
    text = render(f"airstation-{app}.service.in")
    assert "@USER@" not in text and "@REPO@" not in text
    sections = unit(app)
    assert set(sections) == {"Unit", "Service", "Install"}


@pytest.mark.parametrize("app", APPS)
def test_unit_runs_the_app_from_the_repo_as_the_user(app):
    service = unit(app)["Service"]
    assert service["ExecStart"] == f"{REPO}/.venv/bin/python -m {app}"
    assert service["WorkingDirectory"] == REPO
    assert service["User"] == USER
    assert service["StandardOutput"] == "journal" and service["StandardError"] == "journal"


@pytest.mark.parametrize("app", APPS)
def test_unit_has_the_watchdog_and_restart_policy(app):
    sections = unit(app)
    service = sections["Service"]
    assert service["Type"] == "notify"
    assert service["WatchdogSec"] == "90"
    assert service["Restart"] == "always"
    assert service["RestartSec"] == "5"
    assert service["StartLimitIntervalSec"] == "0"
    assert sections["Unit"]["After"] == "network-online.target"
    assert sections["Unit"]["Wants"] == "network-online.target"
    assert sections["Install"]["WantedBy"] == "multi-user.target"


def test_units_have_no_environment_overrides():
    for app in APPS:
        assert "Environment" not in unit(app)["Service"], app


def sudoers_commands() -> list:
    rules = [line for line in render("airstation-sudoers.in").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    commands = []
    for rule in rules:
        m = re.fullmatch(rf"{USER} ALL=\(root\) NOPASSWD: (.+)", rule)
        assert m, f"unexpected sudoers line: {rule!r}"
        commands += [c.strip() for c in m.group(1).split(",")]
    return commands


def test_sudoers_has_exactly_the_five_commands_the_manager_runs():
    assert sudoers_commands() == [
        "/usr/bin/nmcli radio wifi off",
        "/usr/bin/nmcli radio wifi on",
        "/usr/bin/systemctl restart airstation-collector",
        "/usr/bin/systemctl restart airstation-dashboard",
        "/usr/sbin/reboot",
    ]


def test_sudoers_matches_the_strings_in_the_manager_code():
    allowed = set(sudoers_commands())
    by_basename = {re.sub(r"^/\S+/", "", c): c for c in allowed}  # "nmcli radio wifi off"
    assert " ".join(network.BOUNCE_OFF[1:]) in by_basename
    assert " ".join(network.BOUNCE_ON[1:]) in by_basename
    for name in ("restart_collector", "restart_dashboard", "reboot"):
        command = manager_commands.SYSTEM_COMMANDS[name]
        assert command.startswith("sudo ")
        assert command[len("sudo "):] in by_basename, command


def test_the_other_systemd_files_are_still_there():
    for name in ("wifi-powersave-off.service", "enable-watchdog.sh", "watchdog-system.conf"):
        assert (SYSTEMD / name).exists(), name
    assert "install-watchdog" not in (SYSTEMD / "enable-watchdog.sh").read_text()
    assert "install-watchdog" not in (SYSTEMD / "watchdog-system.conf").read_text()
