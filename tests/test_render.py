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


def test_icons_are_pasted_for_known_codes_and_boxed_for_unknown():
    doc = _doc()
    doc["weather"]["blocks"][2]["wmo"] = 123  # not a WMO code we know
    image, painted = render(doc, now=NOW)
    assert "icon:partly_cloudy.png" in painted and "icon:rain.png" in painted
    assert sum(1 for s in painted if s.startswith("icon:")) == 2
    _save(image, "render_icons")


def test_night_uses_the_moon_only_when_the_file_exists(tmp_path):
    from shared.render import ICONS_DIR, icon_file
    assert icon_file(0, is_night=True) == "sun.png"           # no moon.png shipped yet
    icons = tmp_path / "icons"
    icons.mkdir()
    (icons / "moon.png").write_bytes((ICONS_DIR / "sun.png").read_bytes())
    assert icon_file(0, is_night=True, icons_dir=icons) == "moon.png"
    assert icon_file(0, is_night=False, icons_dir=icons) == "sun.png"
    assert icon_file(61, is_night=True, icons_dir=icons) == "rain.png"
    assert icon_file(None) is None and icon_file(True) is None
    doc = _doc()
    doc["weather"]["blocks"][0].update(wmo=0, is_night=True)
    _, painted = render(doc, now=NOW, icons_dir=icons)
    assert "icon:moon.png" in painted


def test_each_glyph_flag_adds_exactly_one_glyph():
    _, none = render(_doc(), now=NOW)
    assert not any(s.startswith("glyph:") for s in none)
    image, painted = render(_doc(glyphs={"wifi": True, "power": True, "sensor": True}), now=NOW)
    assert [s for s in painted if s.startswith("glyph:")] == ["glyph:wifi", "glyph:power", "glyph:sensor"]
    _, one = render(_doc(glyphs={"power": True}), now=NOW)
    assert [s for s in one if s.startswith("glyph:")] == ["glyph:power"]
    _save(image, "render_glyphs")


def test_silent_collector_forces_dashes_and_the_sensor_glyph():
    image, painted = render(_doc(collector_silent=True), now=NOW)
    assert f"AQI: {DASH}" in painted and f"CO2: {DASH}" in painted and f"Temp: {DASH}" in painted
    assert "Good" not in painted and "glyph:sensor" in painted
    assert "12–15" in painted  # the forecast is still valid
    _save(image, "render_silent")
