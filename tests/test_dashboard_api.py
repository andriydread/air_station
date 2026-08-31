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
    assert body["hours"] == 24 * 30


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
