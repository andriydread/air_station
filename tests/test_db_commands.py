"""commands table: queue → claim → complete, crash and orphan handling."""

from shared.db import Database


def test_queue_claim_complete_flow(db):
    cid = db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    row = db.recent_commands()[0]
    assert (row["id"], row["status"], row["from_whom"], row["to_whom"]) == (cid, "pending", "dashboard", "collector")
    claimed = db.claim_pending("collector")
    assert [c["id"] for c in claimed] == [cid] and claimed[0]["type"] == "sps30_fan_clean"
    assert db.recent_commands()[0]["status"] == "running"
    db.complete_command(cid, True, {"blanked_s": 15})
    row = db.recent_commands()[0]
    assert row["status"] == "success" and row["result"] == {"blanked_s": 15}
    db.complete_command(cid, False, {"error": "x"})
    assert db.recent_commands()[0]["status"] == "fail"


def test_claim_only_own_target_and_oldest_first(tmp_path):
    ticks = iter(range(1000, 2000))
    db = Database(tmp_path / "c.db", now=lambda: next(ticks))
    a = db.queue_command("reboot", "dashboard", "manager", {"confirmed": True})
    b = db.queue_command("scd41_calibrate", "dashboard", "collector", {"target_ppm": 420})
    c = db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    assert [x["id"] for x in db.claim_pending("collector")] == [b, c]
    assert db.claim_pending("collector") == []          # nothing pending any more
    assert [x["id"] for x in db.claim_pending("manager")] == [a]
    assert db.claim_pending("manager")[0:0] == []
    db.close()


def test_a_claim_never_returns_the_same_row_twice(db):
    cid = db.queue_command("sps30_fan_clean", "dashboard", "collector")
    first = db.claim_pending("collector")
    second = db.claim_pending("collector")
    assert [c["id"] for c in first] == [cid] and second == []


def test_fail_running_at_start_touches_only_own_rows(db):
    mine = db.queue_command("sps30_fan_clean", "dashboard", "collector")
    theirs = db.queue_command("reboot", "dashboard", "manager", {"confirmed": True})
    db.claim_pending("collector")
    db.claim_pending("manager")
    assert db.fail_running("collector", "collector restarted") == 1
    rows = {r["id"]: r for r in db.recent_commands()}
    assert rows[mine]["status"] == "fail" and rows[mine]["result"] == {"error": "collector restarted"}
    assert rows[theirs]["status"] == "running"


def test_fail_unclaimed_respects_age(tmp_path):
    ticks = iter([1000, 1500])  # one now() per queued command
    db = Database(tmp_path / "c.db", now=lambda: next(ticks))
    old = db.queue_command("sps30_fan_clean", "dashboard", "collector")   # created 1000
    new = db.queue_command("reboot", "dashboard", "manager", {"confirmed": True})  # created 1500
    assert db.fail_unclaimed(older_than_s=600, now=1700) == 1
    rows = {r["id"]: r for r in db.recent_commands()}
    assert rows[old]["status"] == "fail" and rows[old]["result"] == {"error": "not picked up"}
    assert rows[new]["status"] == "pending"
    db.close()


def test_newest_id_and_prune(tmp_path):
    ticks = iter([100, 200, 300])
    db = Database(tmp_path / "c.db", now=lambda: next(ticks))
    assert db.newest_command_id() == 0
    db.queue_command("sps30_fan_clean", "dashboard", "collector")
    last = db.queue_command("sps30_fan_clean", "dashboard", "collector")
    assert db.newest_command_id() == last
    assert db.prune_commands(before_ts=150) == 1
    assert len(db.recent_commands()) == 1
    db.close()
