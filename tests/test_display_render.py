"""utils.display render smoke tests: correct canvas for any input shape."""

from utils.display import create_display_image

WIDTH, HEIGHT = 416, 240  # the panel after 90-degree rotation
FONT = "assets/fonts/dejavu-sans-bold.ttf"


def _full_data():
    return {
        "co2": 812,
        "temp": 21.7,
        "humid": 44.3,
        "pm25": 12.4,
        "pm10": 33.0,
        1: ["09:00-12:00", 18.5, 12.0, 30, 61],
        2: ["13:00-17:00", 22.0, 16.5, 10, 2],
        3: ["18:00-22:00", 17.0, 11.0, 80, 95],
    }


def test_renders_full_data():
    image = create_display_image(WIDTH, HEIGHT, _full_data(), FONT)
    assert image.size == (WIDTH, HEIGHT)
    assert image.mode == "1"
    # something was actually drawn (not a blank white canvas)
    assert image.getextrema()[0] == 0


def test_renders_with_everything_missing():
    image = create_display_image(WIDTH, HEIGHT, {}, FONT)
    assert image.size == (WIDTH, HEIGHT)
    assert image.mode == "1"


def test_renders_with_string_weather_keys():
    """State-table JSON round-trips turn the int weather keys into strings."""
    data = {str(key): value for key, value in _full_data().items()}
    image = create_display_image(WIDTH, HEIGHT, data, FONT)
    assert image.size == (WIDTH, HEIGHT)


def test_renders_with_partial_weather_block():
    data = _full_data()
    data[2] = ["13:00-17:00", None, None, None, None]
    data[3] = None
    image = create_display_image(WIDTH, HEIGHT, data, FONT)
    assert image.size == (WIDTH, HEIGHT)


def test_status_glyphs_appear_only_for_problems(monkeypatch):
    import utils.display as display_module

    class FrozenDatetime:
        @staticmethod
        def now():
            from datetime import datetime as real
            return real(2026, 9, 1, 12, 0, 0)

    monkeypatch.setattr(display_module, "datetime", FrozenDatetime)

    all_ok = create_display_image(
        WIDTH, HEIGHT,
        {**_full_data(), "status": {"network": True, "power": True, "sensors": True}},
        FONT,
    )
    all_bad = create_display_image(
        WIDTH, HEIGHT,
        {**_full_data(), "status": {"network": False, "power": False, "sensors": False}},
        FONT,
    )
    no_status = create_display_image(WIDTH, HEIGHT, _full_data(), FONT)

    # Healthy status draws nothing extra; problems visibly change the header.
    assert list(all_ok.getdata()) == list(no_status.getdata())
    assert list(all_bad.getdata()) != list(all_ok.getdata())

    # Each single fault draws its own distinct glyph (not one shared mark).
    single_faults = []
    for key in ("network", "power", "sensors"):
        status = {"network": True, "power": True, "sensors": True, key: False}
        frame = create_display_image(WIDTH, HEIGHT, {**_full_data(), "status": status}, FONT)
        single_faults.append(list(frame.getdata()))
    assert single_faults[0] != single_faults[1]
    assert single_faults[1] != single_faults[2]
    for frame in single_faults:
        assert frame != list(all_ok.getdata())


def test_renders_with_unknown_wmo_code_and_missing_font():
    data = _full_data()
    data[1][4] = 12345  # unmapped WMO code falls back to a default icon
    image = create_display_image(WIDTH, HEIGHT, data, None)
    assert image.size == (WIDTH, HEIGHT)


def test_renders_with_hostile_values():
    """NaN/inf/bool/malformed weather must degrade to dashes, not crash."""
    data = {
        "co2": float("nan"),
        "temp": float("inf"),
        "humid": True,
        "pm25": float("nan"),
        "pm10": 700.0,
        1: ["09:00-12:00", 3.0],  # wrong-length block
        2: "not-a-list",
        3: None,
    }
    image = create_display_image(WIDTH, HEIGHT, data, FONT)
    assert image.size == (WIDTH, HEIGHT)
