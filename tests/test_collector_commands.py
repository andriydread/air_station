"""The collector's two commands through the real mailbox."""

import pytest

from collector.commands import CommandRunner
from collector.sensors import CAL_MIN_RUNTIME
from tests.test_sampling import Rig


@pytest.fixture
def runner(db, log, tmp_config, monkeypatch):
    rig = Rig(db, log, tmp_config, monkeypatch)
    rig.warm()
    rig.beat()
    r = CommandRunner(db, log, rig.sampler, tmp_config, monotonic=rig.clock.monotonic)
    r.rig = rig
    return r


def _ready_to_calibrate(rig):
    rig.clock.advance(CAL_MIN_RUNTIME)
    now = rig.clock.now()
    rig.scd41.recent.clear()  # the warm-up beat recorded a 600 ppm reading
    for i in range(3):
        rig.scd41.record_valid(now - 20 + i * 10, 430)
    return now


def test_fan_clean_succeeds_and_completes_the_row(runner, db):
    cid = db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    assert runner.process(runner.rig.clock.now()) == 1
    row = db.recent_commands()[0]
    assert row["id"] == cid and row["status"] == "success" and row["result"]["blank_s"] == 15
    assert db.recent_events()[0]["type"] == "command_done"
    assert runner.rig.sps.clean_calls == 1


def test_calibrate_success_writes_last_calibration(runner, db):
    now = _ready_to_calibrate(runner.rig)
    runner.rig.scd.calibration_result = 7
    db.queue_command("scd41_calibrate", "dashboard", "collector",
                     {"target_ppm": 420, "allow_large_offset": False, "persist": True})
    runner.process(now)
    row = db.recent_commands()[0]
    assert row["status"] == "success" and row["result"]["correction_ppm"] == 7
    last = db.get_state("last_calibration")["value"]
    assert last == {"at": int(now), "target_ppm": 420, "correction_ppm": 7, "persisted": True}
    types = [e["type"] for e in db.recent_events()]
    assert "calibration_done" in types and "command_done" in types


def test_calibrate_refusal_lands_in_the_result(runner, db):
    db.queue_command("scd41_calibrate", "dashboard", "collector", {"target_ppm": 420})
    runner.process(runner.rig.clock.now())  # sensor has not run 3 minutes yet
    row = db.recent_commands()[0]
    assert row["status"] == "fail" and "must run for" in row["result"]["error"]
    types = [e["type"] for e in db.recent_events()]
    assert "calibration_refused" in types and "command_failed" in types
    assert db.get_state("last_calibration") is None


def test_calibrate_target_validation_and_default(runner, db):
    now = _ready_to_calibrate(runner.rig)
    db.queue_command("scd41_calibrate", "dashboard", "collector", {"target_ppm": 300})
    runner.process(now)
    assert "between 400 and 2000" in db.recent_commands()[0]["result"]["error"]
    db.queue_command("scd41_calibrate", "dashboard", "collector", {})
    runner.process(now)
    assert db.recent_commands()[0]["result"]["target_ppm"] == 420  # config default


def test_unknown_type_and_crashing_handler_fail_cleanly(runner, db, monkeypatch):
    db.queue_command("make_coffee", "dashboard", "collector", {})
    runner.process(runner.rig.clock.now())
    assert db.recent_commands()[0]["result"] == {"error": "unsupported command: make_coffee"}

    def boom(*_a, **_k):
        raise RuntimeError("fan exploded")

    monkeypatch.setattr(runner.rig.sps30, "force_clean", boom)
    a = db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    b = db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    assert runner.process(runner.rig.clock.now()) == 2   # the second still ran
    rows = {r["id"]: r for r in db.recent_commands()}
    assert rows[a]["status"] == "fail" and rows[b]["status"] == "fail"
    assert rows[a]["result"] == {"error": "fan exploded"}


def test_only_collector_commands_are_claimed(runner, db):
    db.queue_command("reboot", "dashboard", "manager", {"confirmed": True})
    assert runner.process(runner.rig.clock.now()) == 0
    assert db.recent_commands()[0]["status"] == "pending"
