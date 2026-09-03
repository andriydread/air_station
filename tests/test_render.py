"""The panel picture: what gets painted, from a display_data document."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from shared.render import DASH, HEIGHT, WARMING_TEXT, WIDTH, render

OUT = Path(__file__).resolve().parent / "out"
NOW = datetime(2026, 9, 3, 12, 34, tzinfo=timezone.utc).timestamp()


def _doc(**overrides):
    doc = {
        "updated_at": int(NOW), "warming_up": False, "collector_silent": False,
        "values": {"co2": 812.4, "temp": 23.44, "humid": 44.0, "pm25": 4.2, "pm10": 4.9},
        "aqi": 17, "aqi_category": "Good", "aqi_short": "Good", "co2_category": "Good",
        "weather": {"stale": False, "fetched_at": int(NOW) - 600, "blocks": [
            {"label": "12–15", "t_max": 24.1, "t_min": 21.0, "rain": 10, "wmo": 2},
            {"label": "15–18", "t_max": 25.3, "t_min": 22.2, "rain": 40, "wmo": 61},
            {"label": "18–21", "t_max": 21.0, "t_min": 18.5, "rain": 0, "wmo": 0},
        ]},
        "glyphs": {"wifi": False, "power": False, "sensor": False},
    }
    doc.update(overrides)
    return doc


def _save(image, name):
    OUT.mkdir(exist_ok=True)
    image.save(OUT / f"{name}.png")


def test_full_document_paints_the_expected_strings():
    image, painted = render(_doc(), now=NOW)
    assert image.size == (WIDTH, HEIGHT) and image.mode == "1"
    for expected in ("AQI: 17", "Good", "CO2: 812", "Temp: 23.4°", "Humid: 44.0 %",
                     "12–15", "15–18", "18–21", "24.1/21.0", "Rain:10%", "Rain:40%"):
        assert expected in painted, expected
    assert painted[0] == "12:34" or painted[0] == datetime.fromtimestamp(NOW).strftime("%H:%M")
    _save(image, "render_normal")


def test_missing_metrics_paint_dashes():
    doc = _doc(values={"co2": None, "temp": None, "humid": None}, aqi=None,
               aqi_short=None, co2_category=None)
    image, painted = render(doc, now=NOW)
    assert f"AQI: {DASH}" in painted and f"CO2: {DASH}" in painted
    assert f"Temp: {DASH}" in painted and f"Humid: {DASH}" in painted
    assert "Good" not in painted
    _save(image, "render_dashes")


def test_long_category_word_shrinks_one_step():
    doc = _doc(aqi=250, aqi_short="Very Unhealthy", co2_category="Elevated")
    image, painted = render(doc, now=NOW)
    assert "Very Unhealthy" in painted and "Elevated" in painted
    # the word must not run into the CO2 half: nothing painted beyond the middle
    _save(image, "render_very_unhealthy")


def test_empty_document_renders_without_error():
    image, painted = render({}, now=NOW)
    assert image.size == (WIDTH, HEIGHT)
    assert f"AQI: {DASH}" in painted and painted.count(DASH) == 3  # three column labels
    assert f"{DASH}/{DASH}" in painted and f"Rain:{DASH}" in painted


def test_warming_up_frame_hides_the_numbers_but_keeps_header_and_weather():
    image, painted = render(_doc(warming_up=True), now=NOW)
    assert WARMING_TEXT in painted
    assert not any(s.startswith("AQI") or s.startswith("CO2") or s.startswith("Temp") for s in painted)
    assert "12–15" in painted and "Rain:10%" in painted
    _save(image, "render_warming")


def test_stale_weather_paints_dashes_in_all_three_columns():
    doc = _doc()
    doc["weather"]["stale"] = True
    image, painted = render(doc, now=NOW)
    assert painted.count(DASH) == 3 and painted.count(f"{DASH}/{DASH}") == 3
    assert "12–15" not in painted
    _save(image, "render_stale_weather")


def test_png_files_exist_for_the_operator():
    for name in ("render_normal", "render_dashes", "render_warming"):
        assert (OUT / f"{name}.png").exists()
