"""/api/display-preview.png: the panel picture, ETag/304, 404 without data."""

import io

import pytest
from PIL import Image

from dashboard.app import create_app
from shared.events import Log


@pytest.fixture
def client(tmp_config, db):
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    yield app.test_client()
    log.close()


def test_404_without_display_data(client):
    response = client.get("/api/display-preview.png")
    assert response.status_code == 404 and "no display data" in response.get_json()["error"]


def test_png_and_etag_304(client, db, monkeypatch):
    monkeypatch.setattr(db, "now", lambda: 1_788_436_800)
    db.set_state("display_data", {"updated_at": 1_788_436_800, "values": {"co2": 812, "temp": 23.4, "humid": 44.0},
                                  "aqi": 17, "aqi_short": "Good", "co2_category": "Good",
                                  "weather": {"stale": True, "blocks": []}, "glyphs": {}})
    response = client.get("/api/display-preview.png")
    assert response.status_code == 200 and response.mimetype == "image/png"
    image = Image.open(io.BytesIO(response.data))
    assert image.size == (416, 240)
    etag = response.headers["ETag"]
    assert response.headers["Cache-Control"] == "no-cache"
    again = client.get("/api/display-preview.png", headers={"If-None-Match": etag})
    assert again.status_code == 304 and again.headers["ETag"] == etag
    monkeypatch.setattr(db, "now", lambda: 1_788_436_860)  # a minute later, as in life
    db.set_state("display_data", {"updated_at": 1_788_436_860, "values": {"co2": 900}, "weather": {}, "glyphs": {}})
    changed = client.get("/api/display-preview.png", headers={"If-None-Match": etag})
    assert changed.status_code == 200 and changed.headers["ETag"] != etag
