"""/api/changes and /api/live."""

import pytest

from dashboard.app import create_app
from shared.events import Log


@pytest.fixture
def client(tmp_config, db):
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    yield app.test_client()
    log.close()


def test_changes_with_an_empty_database_is_all_nulls_and_zeros(client):
    body = client.get("/api/changes").get_json()
    assert set(body) == {"display_data", "collector_status", "manager_status", "last_weather",
                         "last_calibration", "event_id", "command_id", "vitals_at", "raw_at", "now"}
    assert body["display_data"] is None and body["event_id"] == 0 and body["command_id"] == 0
    assert body["vitals_at"] is None and body["raw_at"] is None and body["now"] > 0


def test_changes_reflect_new_writes(client, db):
    before = client.get("/api/changes").get_json()
    db.set_state("display_data", {"values": {"co2": 800}})
    db.insert_event("manager", "info", "app", "started", "x")
    db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    db.insert_vitals({"recorded_at": 1234})
    db.insert_raw(1230, {"co2": 800})
    after = client.get("/api/changes").get_json()
    assert before["display_data"] is None and after["display_data"] > 0
    assert after["event_id"] == before["event_id"] + 1 and after["command_id"] == before["command_id"] + 1
    assert after["vitals_at"] == 1234 and after["raw_at"] == 1230


def test_live_with_missing_documents_never_500s(client):
    body = client.get("/api/live").get_json()
    assert body["display_data"] is None and body["collector_status"] is None and body["manager_status"] is None
    assert body["version"]["uptimes"]["dashboard"] >= 0 and isinstance(body["version"]["commit"], str)


def test_live_shape(client, db):
    db.set_state("display_data", {"values": {"co2": 812}, "aqi": 17})
    db.set_state("collector_status", {"uptime": 120, "sensors": {}})
    db.set_state("manager_status", {"uptime": 90})
    db.set_state("last_calibration", {"at": 1, "target_ppm": 420})
    body = client.get("/api/live").get_json()
    assert body["display_data"]["value"]["aqi"] == 17 and body["display_data"]["updated_at"] > 0
    assert body["collector_status"]["value"]["uptime"] == 120
    assert body["version"]["uptimes"] == {"collector": 120, "manager": 90, "dashboard": body["version"]["uptimes"]["dashboard"]}
    assert body["last_calibration"]["value"]["target_ppm"] == 420
