"""The e-paper picture, drawn from a ``display_data`` document.

Used by the manager (the panel) and the dashboard (the preview PNG), so both
show exactly the same image. 416×240, 1-bit (white=255, black=0). Layout as
the station has always had it: clock row with problem glyphs, AQI and CO2
huge with their category words, temperature/humidity, three forecast
columns. A metric with no value paints "—"; while the collector's sensors
warm up the numbers area says "Warming up…".

``render()`` returns the image and the list of strings it painted, so tests
check *what* is on the panel without comparing pixels.
"""

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from shared import clock

WIDTH, HEIGHT = 416, 240
ASSETS = Path(__file__).resolve().parents[1] / "assets"
FONT_PATH = ASSETS / "fonts" / "dejavu-sans-bold.ttf"
ICONS_DIR = ASSETS / "icons"

DASH = "—"
WARMING_TEXT = "Warming up…"
_FONT_SIZES = (36, 24, 18, 16, 14)  # huge, large, medium, small, extra-small
_EDGE_PAD = 12
_Y_LINE_1, _Y_LINE_2, _Y_LINE_3 = 30, 92, 122

_font_cache: Dict[str, tuple] = {}


def _load_fonts(font_path: Optional[str]):
    key = str(font_path or FONT_PATH)
    if key in _font_cache:
        return _font_cache[key]
    candidates = [key, "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    fonts = None
    for candidate in candidates:
        if Path(candidate).exists():
            try:
                fonts = tuple(ImageFont.truetype(candidate, size) for size in _FONT_SIZES)
                break
            except OSError:
                continue
    if fonts is None:
        fonts = tuple(ImageFont.load_default() for _ in _FONT_SIZES)
    _font_cache[key] = fonts
    return fonts


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class _Painter:
    """A drawing surface that remembers every string it paints."""

    def __init__(self, image: Image.Image):
        self.image = image
        self.draw = ImageDraw.Draw(image)
        self.painted: List[str] = []

    def text(self, xy, text: str, font) -> None:
        self.draw.text(xy, text, font=font, fill=0)
        self.painted.append(text)

    def width(self, text: str, font) -> float:
        return self.draw.textlength(text, font=font)

    def left(self, text: str, font, x_pad: float, y: float) -> None:
        self.text((x_pad, y), text, font)

    def right(self, text: str, font, width: float, x_pad: float, y: float) -> None:
        self.text((width - self.width(text, font) - x_pad, y), text, font)

    def center(self, text: str, font, x_start: float, x_end: float, y: float) -> None:
        x = x_start + (x_end - x_start - self.width(text, font)) / 2
        self.text((x, y), text, font)

    def line(self, xy) -> None:
        self.draw.line(xy, fill=0, width=1)


def _header(p: _Painter, fonts, width: int, when: datetime) -> float:
    _, _, _, font_sm, _ = fonts
    time_text = when.strftime("%H:%M")
    p.left(time_text, font_sm, _EDGE_PAD, 5)
    p.center(when.strftime("%A"), font_sm, 0, width, 5)
    p.right(when.strftime("%d/%m/%Y"), font_sm, width, _EDGE_PAD, 5)
    return _EDGE_PAD + p.width(time_text, font_sm) + 12  # where glyphs start


def _fit(p: _Painter, text: str, fonts_in_order, max_width: float):
    """The largest of the given fonts that fits ``text`` in ``max_width``."""
    for font in fonts_in_order:
        if p.width(text, font) <= max_width:
            return font
    return fonts_in_order[-1]


def _numbers(p: _Painter, fonts, width: int, data: Dict[str, Any]) -> None:
    font_huge, font_lg, font_md, font_sm, _ = fonts
    values = data.get("values") or {}
    aqi = data.get("aqi")
    aqi_text = f"AQI: {int(aqi)}" if isinstance(aqi, (int, float)) and not isinstance(aqi, bool) else f"AQI: {DASH}"
    p.left(aqi_text, font_huge, _EDGE_PAD, _Y_LINE_1 + 2)
    half = width / 2 - _EDGE_PAD
    aqi_word = data.get("aqi_short")
    if aqi_word:
        p.left(str(aqi_word), _fit(p, str(aqi_word), (font_md, font_sm), half), _EDGE_PAD, _Y_LINE_1 + 38)
    co2 = _finite(values.get("co2"))
    co2_text = f"CO2: {int(round(co2))}" if co2 is not None else f"CO2: {DASH}"
    p.right(co2_text, font_huge, width, _EDGE_PAD, _Y_LINE_1 + 2)
    co2_word = data.get("co2_category")
    if co2_word:
        p.right(str(co2_word), _fit(p, str(co2_word), (font_md, font_sm), half), width, _EDGE_PAD, _Y_LINE_1 + 38)

    temp = _finite(values.get("temp"))
    humid = _finite(values.get("humid"))
    temp_text = f"Temp: {temp:.1f}°" if temp is not None else f"Temp: {DASH}"
    humid_text = f"Humid: {humid:.1f} %" if humid is not None else f"Humid: {DASH}"
    p.left(temp_text, font_lg, _EDGE_PAD, _Y_LINE_2 + 2)
    p.right(humid_text, font_lg, width, _EDGE_PAD, _Y_LINE_2 + 2)


def _warming(p: _Painter, fonts, width: int) -> None:
    _, font_lg, _, _, _ = fonts
    p.center(WARMING_TEXT, font_lg, 0, width, _Y_LINE_1 + 30)


def _weather(p: _Painter, fonts, width: int, height: int, data: Dict[str, Any]) -> None:
    """Three forecast columns: label, icon slot, max/min, rain. (Icons: T024.)"""
    _, _, font_md, _, font_xs = fonts
    weather = data.get("weather") or {}
    blocks = list(weather.get("blocks") or [])
    stale = bool(weather.get("stale"))
    col_w = width // 3
    p.line((col_w, _Y_LINE_3, col_w, height))
    p.line((col_w * 2, _Y_LINE_3, col_w * 2, height))
    icon_size = 70
    for i in range(3):
        col_start, col_end = i * col_w, (i + 1) * col_w
        icon_x = col_start + (col_w - icon_size) // 2
        icon_y = _Y_LINE_3 + 18
        block = blocks[i] if i < len(blocks) and isinstance(blocks[i], dict) and not stale else None
        label = str(block.get("label") or DASH) if block else DASH
        p.center(label, font_xs, col_start, col_end, _Y_LINE_3 + 2)
        p.draw.rectangle([icon_x, icon_y, icon_x + icon_size, icon_y + icon_size], outline=0)
        t_max = _finite(block.get("t_max")) if block else None
        t_min = _finite(block.get("t_min")) if block else None
        temps = f"{t_max:.1f}/{t_min:.1f}" if t_max is not None and t_min is not None else f"{DASH}/{DASH}"
        p.center(temps, font_md, col_start, col_end, icon_y + icon_size - 4)
        rain = block.get("rain") if block else None
        rain_text = f"Rain:{int(rain)}%" if isinstance(rain, (int, float)) and not isinstance(rain, bool) else f"Rain:{DASH}"
        p.center(rain_text, font_xs, col_start, col_end, icon_y + icon_size + 15)


def render(display_data: Optional[Dict[str, Any]], width: int = WIDTH, height: int = HEIGHT,
           font_path: Optional[str] = None, now: Optional[float] = None) -> Tuple[Image.Image, List[str]]:
    """Draw the panel from a ``display_data`` document (may be empty)."""
    data = display_data or {}
    fonts = _load_fonts(font_path)
    image = Image.new("1", (width, height), 255)
    p = _Painter(image)
    when = clock.local_now(now)

    _header(p, fonts, width, when)
    p.line((0, _Y_LINE_1, width, _Y_LINE_1))
    if data.get("warming_up"):
        _warming(p, fonts, width)
    else:
        _numbers(p, fonts, width, data)
    p.line((0, _Y_LINE_2, width, _Y_LINE_2))
    p.line((0, _Y_LINE_3, width, _Y_LINE_3))
    _weather(p, fonts, width, height, data)
    p.draw.rectangle([0, 0, width - 1, height - 1], outline=0, width=1)
    return image, p.painted
