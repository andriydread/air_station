"""state table: JSON documents by key, dedupe, freshness stamps."""

from shared.db import Database


def test_round_trip_and_missing_key(db):
    assert db.get_state("nothing") is None
    assert db.set_state("collector_status", {"a": 1, "nested": {"b": [1, 2]}}) is True
    doc = db.get_state("collector_status")
    assert doc["value"] == {"a": 1, "nested": {"b": [1, 2]}}
    assert isinstance(doc["updated_at"], int)


def test_unchanged_document_is_not_rewritten(tmp_path):
    ticks = iter(range(100, 200))
    db = Database(tmp_path / "s.db", now=lambda: next(ticks))
    assert db.set_state("manager_status", {"x": 1}) is True
    first = db.get_state("manager_status")["updated_at"]
    assert db.set_state("manager_status", {"x": 1}) is False
    assert db.get_state("manager_status")["updated_at"] == first
    assert db.set_state("manager_status", {"x": 2}) is True
    assert db.get_state("manager_status")["updated_at"] > first
    db.close()


def test_display_data_always_bumps_its_timestamp(tmp_path):
    ticks = iter(range(100, 200))
    db = Database(tmp_path / "s.db", now=lambda: next(ticks))
    db.set_state("display_data", {"co2": 800})
    first = db.get_state("display_data")["updated_at"]
    assert db.set_state("display_data", {"co2": 800}) is True
    assert db.get_state("display_data")["updated_at"] > first
    db.close()


def test_state_updated_at_for_present_and_missing_keys(db):
    db.set_state("a", 1)
    stamps = db.state_updated_at(["a", "b"])
    assert isinstance(stamps["a"], int) and stamps["b"] is None
    assert db.state_updated_at([]) == {}


def test_dedupe_cache_is_per_connection(tmp_path):
    writer = Database(tmp_path / "s.db")
    other = Database(tmp_path / "s.db")
    writer.set_state("k", {"v": 1})
    assert other.set_state("k", {"v": 1}) is True  # a fresh process does not know the cache
    writer.close()
    other.close()
