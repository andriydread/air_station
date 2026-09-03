"""/api/events, /api/commands (GET + POST), /api/restarts."""

import pytest

from dashboard.app import create_app
from shared import clock
from shared.events import Log

NOW = 1_788_436_800


@pytest.fixture
def client(tmp_config, db, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: float(NOW))
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    yield app.test_client()
    log.close()


def test_events_filters_limit_and_since_id(client, db):
    ids = [
        db.insert_event("collector", "info", "app", "started", "a", ts=NOW - 30),
        db.insert_event("manager", "warning", "wifi", "wifi_down", "b", ts=NOW - 20),
        db.insert_event("manager", "error", "display", "display_error", "c", ts=NOW - 10),
    ]
    body = client.get("/api/events").get_json()
    assert [e["id"] for e in body["events"]] == list(reversed(ids)) and body["newest_id"] == ids[-1]
    assert [e["type"] for e in client.get("/api/events?app=manager&level=error").get_json()["events"]] == ["display_error"]
    assert [e["type"] for e in client.get("/api/events?source=wifi").get_json()["events"]] == ["wifi_down"]
    assert len(client.get("/api/events?limit=1").get_json()["events"]) == 1
    assert [e["id"] for e in client.get(f"/api/events?since_id={ids[0]}").get_json()["events"]] == [ids[2], ids[1]]
    assert client.get("/api/events?limit=x").status_code == 400


def test_commands_listing_and_restart_counts(client, db):
    db.queue_command("sps30_fan_clean", "dashboard", "collector", {})
    body = client.get("/api/commands").get_json()
    assert body["commands"][0]["type"] == "sps30_fan_clean" and body["newest_id"] == 1
    db.insert_event("collector", "info", "app", "started", "x", ts=NOW - 3600)
    db.insert_event("collector", "info", "app", "started", "x", ts=NOW - 2 * 86400)
    db.insert_event("manager", "info", "app", "started", "x", ts=NOW - 60)
    assert client.get("/api/restarts").get_json() == {"hours": 24, "collector": 1, "manager": 1, "dashboard": 0}
    assert client.get("/api/restarts?hours=72").get_json()["collector"] == 2


@pytest.mark.parametrize("type_, payload, to_whom, clean", [
    ("sps30_fan_clean", {}, "collector", {}),
    ("restart_collector", {}, "manager", {}),
    ("restart_dashboard", {"anything": 1}, "manager", {}),
    ("reboot", {"confirmed": True}, "manager", {"confirmed": True}),
    ("delete_history", {"confirmed": "true"}, "manager", {"confirmed": True}),
    ("scd41_calibrate", {"target_ppm": "430", "allow_large_offset": "on"}, "collector",
     {"target_ppm": 430, "allow_large_offset": True, "persist": False}),
    ("scd41_calibrate", {}, "collector", {"target_ppm": 420, "allow_large_offset": False, "persist": False}),
])
def test_post_routes_and_validates(client, db, type_, payload, to_whom, clean):
    response = client.post("/api/commands", json={"type": type_, "payload": payload})
    assert response.status_code == 202
    body = response.get_json()
    assert body["to_whom"] == to_whom and body["status"] == "pending"
    row = db.recent_commands()[0]
    assert row["id"] == body["id"] and row["from_whom"] == "dashboard" and row["to_whom"] == to_whom
    assert row["payload"] == clean
    event = db.recent_events()[0]
    assert event["type"] == "command_created" and event["details"]["type"] == type_


@pytest.mark.parametrize("body, message", [
    ({"type": "make_tea"}, "unsupported command"),
    ({"type": ""}, "unsupported command"),
    ({"type": "reboot"}, "confirmed=true"),
    ({"type": "delete_history", "payload": {"confirmed": False}}, "confirmed=true"),
    ({"type": "scd41_calibrate", "payload": {"target_ppm": 300}}, "between 400 and 2000"),
    ({"type": "scd41_calibrate", "payload": {"target_ppm": "abc"}}, "whole number"),
    ({"type": "scd41_calibrate", "payload": {"persist": "maybe"}}, "true or false"),
    ({"type": "sps30_fan_clean", "payload": [1]}, "JSON object"),
])
def test_post_rejections(client, db, body, message):
    response = client.post("/api/commands", json=body)
    assert response.status_code == 400 and message in response.get_json()["error"]
    assert db.recent_commands() == []


def test_post_needs_json(client):
    response = client.post("/api/commands", data="type=reboot")
    assert response.status_code == 400
