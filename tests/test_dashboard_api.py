"""Dashboard API tests via the Flask test client (no server, no hardware)."""

import pytest

from airmonitor.storage import AirMonitorDatabase


@pytest.fixture
def client(monkeypatch, tmp_path):
    db_path = str(tmp_path / "dash.db")
    monkeypatch.setenv("AIRMONITOR_DATABASE_PATH", db_path)

    from dashboard.app import create_app

    app = create_app()
    app.config["TESTING"] = True

    seed = AirMonitorDatabase(db_path)
    seed.insert_measurement({"co2": 800, "temp": 22.0, "humid": 50.0, "pm25": 12.0, "pm10": 54.0})
    seed.set_state("collector_status", {"running": True})
    seed.close()

    with app.test_client() as test_client:
        yield test_client


def test_health_reflects_collector_state(client):
    body = client.get("/api/health").get_json()
    assert body["ok"] is True


def test_summary_includes_backend_aqi(client):
    body = client.get("/api/summary").get_json()
    # pm25=12, pm10=54 both sit exactly on the AQI-50 breakpoint
    assert body["aqi"]["value"] == 50
    assert body["aqi"]["category"] == "Good"
    assert body["aqi"]["co2_category"] == "Good"
    assert body["latest_measurement"]["co2"] == 800


def test_history_rows_carry_aqi(client):
    body = client.get("/api/history?hours=1").get_json()
    assert body["rows"], "seeded measurement should appear"
    assert body["rows"][-1]["aqi"] == 50


def test_history_hours_clamped(client):
    body = client.get("/api/history?hours=99999").get_json()
    assert body["to_ts"] - body["from_ts"] == 24 * 30 * 3600


def test_history_rejects_non_integer_hours(client):
    response = client.get("/api/history?hours=abc")
    assert response.status_code == 400


def test_events_endpoint(client):
    response = client.get("/api/events?limit=10")
    assert response.status_code == 200
    assert "events" in response.get_json()


def test_command_queueing_and_validation(client):
    accepted = client.post("/api/commands", json={"command": "sps30_force_clean", "payload": {}})
    assert accepted.status_code == 202
    assert accepted.get_json()["status"] == "pending"

    unknown = client.post("/api/commands", json={"command": "rm_rf_slash", "payload": {}})
    assert unknown.status_code == 400

    bad_payload = client.post(
        "/api/commands",
        json={"command": "scd41_force_calibration", "payload": {"target_co2": 10_000}},
    )
    assert bad_payload.status_code == 400

    missing_confirm = client.post(
        "/api/commands",
        json={"command": "scd41_force_calibration", "payload": {"target_co2": 420}},
    )
    assert missing_confirm.status_code == 400


def test_delete_history_requires_server_side_confirm(client):
    refused = client.delete("/api/history", json={})
    assert refused.status_code == 400
    assert client.get("/api/history?hours=1").get_json()["rows"], "data must survive"

    accepted = client.delete("/api/history", json={"confirm": "delete"})
    assert accepted.status_code == 200
    assert client.get("/api/history?hours=1").get_json()["rows"] == []


# --- R5 API additions -------------------------------------------------------


def test_history_custom_range_and_stats(client):
    import time as time_module

    now = int(time_module.time())
    body = client.get(f"/api/history?from={now - 3600}&to={now}").get_json()
    assert body["from_ts"] == now - 3600
    assert body["rows"]
    stats = body["stats"]
    assert stats["sample_count"] == 1
    assert stats["co2"] == {"min": 800, "avg": 800.0, "max": 800}
    assert stats["temp"]["avg"] == 22.0


def test_history_range_validation(client):
    assert client.get("/api/history?from=100&to=50").status_code == 400
    assert client.get("/api/history?from=0&to=999999999999").status_code == 400
    assert client.get("/api/history?from=garbage").status_code == 400


def test_history_accepts_iso_dates(client):
    response = client.get("/api/history?from=2026-08-30&to=2026-08-31T23:59:59")
    assert response.status_code == 200


def test_csv_export(client):
    body = client.get("/api/export.csv?hours=1")
    assert body.status_code == 200
    assert body.mimetype == "text/csv"
    lines = body.get_data(as_text=True).strip().splitlines()
    assert lines[0].startswith("timestamp,co2,temp")
    assert len(lines) == 2  # header + the seeded measurement
    assert ",800," in lines[1]


def test_flags_endpoint(client):
    body = client.get("/api/flags").get_json()
    assert body == {"flagged": []}


def test_display_preview_renders_png(client, monkeypatch, tmp_path):
    # 404 before any snapshot exists
    assert client.get("/api/display-preview.png").status_code == 404

    import os

    seed = AirMonitorDatabase(os.environ["AIRMONITOR_DATABASE_PATH"])
    seed.set_state(
        "latest_display_snapshot",
        {"mode": "partial", "snapshot": {"co2": 700, "temp": 21.0, "humid": 50.0}},
    )
    seed.close()
    response = client.get("/api/display-preview.png")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_system_commands_require_confirmation(client):
    refused = client.post(
        "/api/commands", json={"command": "system_reboot", "payload": {}}
    )
    assert refused.status_code == 400
    accepted = client.post(
        "/api/commands",
        json={"command": "system_restart_web", "payload": {"confirmed": True}},
    )
    assert accepted.status_code == 202
