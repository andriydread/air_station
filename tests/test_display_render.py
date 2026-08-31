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


def test_renders_with_unknown_wmo_code_and_missing_font():
    data = _full_data()
    data[1][4] = 12345  # unmapped WMO code falls back to a default icon
    image = create_display_image(WIDTH, HEIGHT, data, None)
    assert image.size == (WIDTH, HEIGHT)
