"""events table: insert, filtered reads, polling by id, counts, prune."""

from shared.db import Database


def _seed(db):
    ids = []
    ids.append(db.insert_event("collector", "info", "app", "started", "collector started", ts=100))
    ids.append(db.insert_event("collector", "warning", "scd41", "value_dropped", "co2 0", {"value": 0}, ts=200))
    ids.append(db.insert_event("manager", "error", "display", "display_error", "busy timeout", ts=300))
    ids.append(db.insert_event("manager", "info", "app", "started", "manager started", ts=400))
    return ids


def test_insert_and_newest_first_with_details(db):
    ids = _seed(db)
    rows = db.recent_events()
    assert [r["id"] for r in rows] == list(reversed(ids))
    dropped = rows[2]
    assert dropped["details"] == {"value": 0} and dropped["type"] == "value_dropped"
    assert set(dropped) == {"id", "ts", "app", "level", "source", "type", "message", "details"}


def test_filters_combine(db):
    _seed(db)
    assert [r["type"] for r in db.recent_events(app="manager")] == ["started", "display_error"]
    assert [r["type"] for r in db.recent_events(app="manager", level="error")] == ["display_error"]
    assert [r["type"] for r in db.recent_events(source="scd41")] == ["value_dropped"]
    assert db.recent_events(app="dashboard") == []
    assert [r["type"] for r in db.recent_events(limit=1)] == ["started"]


def test_since_id_for_polling_and_newest_id(db):
    ids = _seed(db)
    assert db.newest_event_id() == ids[-1]
    new = db.recent_events(since_id=ids[1])
    assert [r["id"] for r in new] == [ids[3], ids[2]]
    assert db.recent_events(since_id=ids[-1]) == []


def test_events_between_is_oldest_first(db):
    _seed(db)
    rows = db.events_between(200, 400)
    assert [r["ts"] for r in rows] == [200, 300]
    assert [r["ts"] for r in db.events_between(0, 1000, app="collector")] == [100, 200]


def test_count_and_latest(db):
    _seed(db)
    assert db.count_events("started", since_ts=0) == 2
    assert db.count_events("started", since_ts=0, app="collector") == 1
    assert db.count_events("started", since_ts=350) == 1
    assert db.latest_event("started")["app"] == "manager"
    assert db.latest_event("started", app="collector")["ts"] == 100
    assert db.latest_event("nightly") is None


def test_prune_and_default_timestamp(tmp_path):
    db = Database(tmp_path / "e.db", now=lambda: 12345)
    db.insert_event("dashboard", "info", "web", "command_created", "x")
    assert db.recent_events()[0]["ts"] == 12345
    _seed(db)
    assert db.prune_events(before_ts=300) == 2
    assert db.newest_event_id() == 5 and len(db.recent_events()) == 3
    db.close()
