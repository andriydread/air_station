"""The manager's four commands through the real mailbox."""

import pytest

from manager.commands import CommandRunner


class _Spawner:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))


@pytest.fixture
def runner(db, log):
    spawner = _Spawner()
    r = CommandRunner(db, log, spawner=spawner, monotonic=lambda: 0.0)
    r.spawner_ = spawner
    return r


@pytest.mark.parametrize(
    "type_, payload, command",
    [
        ("restart_collector", {}, "sudo systemctl restart airstation-collector"),
        ("restart_dashboard", {}, "sudo systemctl restart airstation-dashboard"),
        ("reboot", {"confirmed": True}, "sudo reboot"),
    ],
)
def test_system_commands_are_deferred_fixed_strings(runner, db, type_, payload, command):
    cid = db.queue_command(type_, "dashboard", "manager", payload)
    assert runner.process(now=0) == 1
    argv, kwargs = runner.spawner_.calls[0]
    assert argv == ["sh", "-c", f"sleep 2; exec {command}"] and kwargs == {"start_new_session": True}
    row = db.recent_commands()[0]
    assert row["id"] == cid and row["status"] == "success" and row["result"]["scheduled"] == command
    assert db.recent_events()[0]["type"] == "command_done"


def test_reboot_and_delete_require_confirmation(runner, db):
    db.queue_command("reboot", "dashboard", "manager", {})
    db.queue_command("delete_history", "dashboard", "manager", {"confirmed": "yes"})
    runner.process(now=0)
    rows = db.recent_commands()
    assert all(r["status"] == "fail" and "confirmed=true" in r["result"]["error"] for r in rows)
    assert runner.spawner_.calls == []


def test_delete_history_clears_measurements_only(runner, db):
    db.insert_raw(3600, {"co2": 700})
    db.rollup_hour(3600)
    db.insert_vitals({"recorded_at": 3600})
    db.set_state("display_data", {"x": 1})
    db.queue_command("delete_history", "dashboard", "manager", {"confirmed": True})
    runner.process(now=0)
    row = db.recent_commands()[0]
    assert row["status"] == "success" and row["result"] == {"deleted": {"raw": 1, "hourly": 1, "vitals": 1}}
    assert db.raw_between(0, 10**10) == [] and db.get_state("display_data")["value"] == {"x": 1}
    assert len(db.recent_commands()) == 1  # the command row itself survives


def test_unknown_type_and_other_apps_commands(runner, db):
    db.queue_command("make_tea", "dashboard", "manager", {})
    db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    assert runner.process(now=0) == 1
    rows = {r["type"]: r for r in db.recent_commands()}
    assert rows["make_tea"]["status"] == "fail" and rows["sps30_fan_clean"]["status"] == "pending"


def test_spawn_failure_fails_the_command(runner, db):
    def boom(argv, **kwargs):
        raise OSError("no sh")

    runner.spawner = boom
    db.queue_command("restart_collector", "dashboard", "manager", {})
    runner.process(now=0)
    assert db.recent_commands()[0]["result"] == {"error": "no sh"}
