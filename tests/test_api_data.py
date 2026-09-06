"""/api/data: the tables as they are, newest first, paged."""

import json

import pytest

from dashboard.app import create_app
from shared import clock
from shared.db import TABLES
from shared.events import Log

NOW = 1_788_436_800


@pytest.fixture
def client(tmp_config, db, monkeypatch):
    monkeypatch.setattr(clock, "now", lambda: float(NOW))
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    yield app.test_client()
    log.close()


def test_tables_with_counts(client, db):
    for i in range(3):
        db.insert_raw(NOW - 10 * i, {"co2": 600 + i})
    db.set_state("display_data", {"x": 1})
    body = client.get("/api/data/tables").get_json()
    assert [t["name"] for t in body["tables"]] == list(TABLES)
    counts = {t["name"]: t["rows"] for t in body["tables"]}
    assert counts["raw_measurements"] == 3 and counts["state"] == 1 and counts["events"] == 0
    assert {t["name"]: t["order_by"] for t in body["tables"]}["events"] == "id"


def test_rows_newest_first_and_paged(client, db):
    for i in range(7):
        db.insert_raw(NOW + 10 * i, {"co2": 600 + i, "temp": 21.0})
    page = client.get("/api/data/rows?table=raw_measurements&limit=3").get_json()
    assert page["table"] == "raw_measurements" and page["order_by"] == "recorded_at"
    assert page["columns"][:2] == ["recorded_at", "co2"] and len(page["columns"]) == 16
    assert [r["co2"] for r in page["rows"]] == [606, 605, 604] and page["next"] == NOW + 40
    page = client.get(f"/api/data/rows?table=raw_measurements&limit=3&before={page['next']}").get_json()
    assert [r["co2"] for r in page["rows"]] == [603, 602, 601] and page["next"] == NOW + 10
    page = client.get(f"/api/data/rows?table=raw_measurements&limit=3&before={page['next']}").get_json()
    assert [r["co2"] for r in page["rows"]] == [600] and page["next"] is None


def test_json_columns_stay_as_stored(client, db):
    db.insert_event("collector", "info", "app", "started", "hi", {"a": 1}, ts=NOW)
    body = client.get("/api/data/rows?table=events").get_json()
    row = body["rows"][0]
    assert json.loads(row["details"]) == {"a": 1} and isinstance(row["details"], str)
    assert row["type"] == "started" and row["ts"] == NOW


def test_bad_requests_are_400(client):
    assert client.get("/api/data/rows?table=sqlite_master").status_code == 400
    assert client.get("/api/data/rows?table=events&before=x").status_code == 400
    assert client.get("/api/data/rows?table=events&limit=9999").get_json()["rows"] == []  # clamped, not an error
