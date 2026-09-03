"""/api/export.csv: header, raw vs hourly column sets, local time, streaming."""

import csv
import io
import time as _time

import pytest

from dashboard.app import create_app
from shared import clock
from shared.db import METRICS
from shared.events import Log

NOW = 1_788_436_800


@pytest.fixture
def client(tmp_config, db, monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Kyiv")
    _time.tzset()
    monkeypatch.setattr(clock, "now", lambda: float(NOW))
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    yield app.test_client()
    log.close()
    monkeypatch.delenv("TZ")
    _time.tzset()


def _parse(response):
    return list(csv.reader(io.StringIO(response.get_data(as_text=True))))


def test_raw_export_columns_and_local_time(client, db):
    db.insert_raw(NOW - 20, {"co2": 800, "temp": 22.5, "nc25": 8.1})
    db.insert_raw(NOW - 10, {"co2": None, "temp": 22.6})
    response = client.get(f"/api/export.csv?from={NOW - 60}&to={NOW}")
    assert response.status_code == 200 and response.mimetype == "text/csv"
    assert response.headers["Content-Disposition"] == f'attachment; filename="airstation-{NOW - 60}-{NOW}.csv"'
    rows = _parse(response)
    assert rows[0] == ["unix", "local_time", "resolution", *METRICS]
    assert rows[1][:3] == [str(NOW - 20), "2026-09-03 14:59:40", "raw"]
    assert rows[1][3] == "800" and rows[2][3] == "" and rows[1][rows[0].index("nc25")] == "8.1"
    assert len(rows) == 3


def test_hourly_export_beyond_the_raw_window(client, db, tmp_config):
    horizon = NOW - tmp_config.retention_days.raw * 86400
    hour = (horizon - 3 * 3600) // 3600 * 3600
    for i in range(6):
        db.insert_raw(hour + 600 * i, {"co2": 600 + i * 10, "pm25": 3.0})
    db.rollup_hour(hour)
    rows = _parse(client.get(f"/api/export.csv?from={hour - 3600}&to={hour + 7200}"))
    header = rows[0]
    assert header[:3] == ["unix", "local_time", "resolution"] and "co2_min" in header and "samples" in header
    line = rows[1]
    assert line[2] == "hourly" and line[header.index("co2")] == "625" and line[header.index("co2_min")] == "600"
    assert line[header.index("co2_max")] == "650" and line[header.index("samples")] == "6"


def test_streaming_over_many_rows(client, db):
    statements = [("INSERT INTO raw_measurements (recorded_at, co2) VALUES (?, ?)", (NOW - 120000 + 10 * i, 700))
                  for i in range(12000)]
    db.write_many(statements)
    response = client.get(f"/api/export.csv?from={NOW - 120000}&to={NOW}")
    assert response.is_streamed
    rows = _parse(response)
    assert len(rows) == 12001


def test_validation_applies(client):
    assert client.get(f"/api/export.csv?from={NOW}&to={NOW - 5}").status_code == 400
