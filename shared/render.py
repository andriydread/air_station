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


# WMO weather code -> icon file (carried over). Codes 0/1 at night use the
# moon when assets/icons/moon.png exists; until the operator adds it, the sun.
WMO_ICON = {
    0: "sun.png", 1: "sun.png", 2: "partly_cloudy.png", 3: "cloud.png",
    45: "fog.png", 48: "fog.png",
    51: "rain.png", 53: "rain.png", 55: "rain.png", 56: "rain.png", 57: "rain.png",
    61: "rain.png", 63: "rain.png", 65: "rain.png", 66: "rain.png", 67: "rain.png",
    71: "snow.png", 73: "snow.png", 75: "snow.png", 77: "snow.png",
    80: "rain.png", 81: "rain.png", 82: "rain.png", 85: "snow.png", 86: "snow.png",
    95: "storm.png", 96: "storm.png", 99: "storm.png",
}
NIGHT_CLEAR_ICON = "moon.png"
_icon_cache: Dict[Tuple[str, int], Image.Image] = {}


def icon_file(wmo: Any, is_night: bool = False, icons_dir: Path = ICONS_DIR) -> Optional[str]:
    """Which icon file a block shows, or None when the code is unknown."""
    if isinstance(wmo, bool) or not isinstance(wmo, (int, float)):
        return None
    name = WMO_ICON.get(int(wmo))
    if name is None:
        return None
    if is_night and int(wmo) in (0, 1) and (icons_dir / NIGHT_CLEAR_ICON).exists():
        return NIGHT_CLEAR_ICON
    return name


def _load_icon(path: Path, size: int) -> Optional[Image.Image]:
    key = (str(path), size)
    if key in _icon_cache:
        return _icon_cache[key]
    if not path.exists():
        return None
    try:
        icon = Image.open(path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        background = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        # threshold instead of dithering: fuzzy dots look wrong on e-paper
        final = Image.alpha_composite(background, icon).convert("L").point(lambda v: 0 if v < 140 else 255).convert("1")
    except OSError:
        return None
    _icon_cache[key] = final
    return final


# --- Status glyphs: tiny 1-bit header icons so the panel can say "station broken".

def _draw_wifi_down(draw, x, y, size):
    cx, cy = x + size / 2, y + size * 0.9
    for radius in (size * 0.45, size * 0.25):
        draw.arc([cx - radius, cy - radius, cx + radius, cy + radius], 225, 315, fill=0, width=2)
    draw.ellipse([cx - 2, cy - 3, cx + 2, cy + 1], fill=0)
    draw.line([x + 1, y + size - 2, x + size - 1, y + 1], fill=0, width=2)


def _draw_power_issue(draw, x, y, size):
    draw.polygon([(x + size / 2, y), (x, y + size - 1), (x + size - 1, y + size - 1)], outline=0)
    cx = x + size / 2
    draw.line([cx, y + size * 0.35, cx, y + size * 0.65], fill=0, width=2)
    draw.ellipse([cx - 1, y + size * 0.75, cx + 1, y + size * 0.75 + 2], fill=0)


def _draw_sensor_fault(draw, x, y, size):
    draw.ellipse([x, y, x + size - 1, y + size - 1], outline=0, width=2)
    cx = x + size / 2
    draw.line([cx, y + size * 0.25, cx, y + size * 0.55], fill=0, width=2)
    draw.ellipse([cx - 1, y + size * 0.68, cx + 1, y + size * 0.68 + 2], fill=0)


GLYPHS = (("wifi", _draw_wifi_down), ("power", _draw_power_issue), ("sensor", _draw_sensor_fault))


def _glyphs(p: "_Painter", data: Dict[str, Any], left_start: float) -> List[str]:
    flags = dict(data.get("glyphs") or {})
    if data.get("collector_silent"):
        flags["sensor"] = True
    shown = []
    size, y = 18, 6
    for name, painter in GLYPHS:
        if flags.get(name):
            painter(p.draw, left_start + len(shown) * (size + 8), y, size)
            shown.append(name)
    p.painted.extend(f"glyph:{name}" for name in shown)
    return shown


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
    values = {} if data.get("collector_silent") else (data.get("values") or {})
    aqi = None if data.get("collector_silent") else data.get("aqi")
    if data.get("collector_silent"):
        data = {**data, "aqi_short": None, "co2_category": None}
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


def _weather(p: _Painter, fonts, width: int, height: int, data: Dict[str, Any],
             icons_dir: Path = ICONS_DIR) -> None:
    """Three forecast columns: label, icon, max/min, rain."""
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
        name = icon_file(block.get("wmo"), bool(block.get("is_night")), icons_dir) if block else None
        icon = _load_icon(icons_dir / name, icon_size) if name else None
        if icon is not None:
            p.image.paste(icon, (int(icon_x), int(icon_y)))
            p.painted.append(f"icon:{name}")
        else:
            p.draw.rectangle([icon_x, icon_y, icon_x + icon_size, icon_y + icon_size], outline=0)
        t_max = _finite(block.get("t_max")) if block else None
        t_min = _finite(block.get("t_min")) if block else None
        temps = f"{t_max:.1f}/{t_min:.1f}" if t_max is not None and t_min is not None else f"{DASH}/{DASH}"
        p.center(temps, font_md, col_start, col_end, icon_y + icon_size - 4)
        rain = block.get("rain") if block else None
        rain_text = f"Rain:{int(rain)}%" if isinstance(rain, (int, float)) and not isinstance(rain, bool) else f"Rain:{DASH}"
        p.center(rain_text, font_xs, col_start, col_end, icon_y + icon_size + 15)


def render(display_data: Optional[Dict[str, Any]], width: int = WIDTH, height: int = HEIGHT,
           font_path: Optional[str] = None, now: Optional[float] = None,
           icons_dir: Path = ICONS_DIR) -> Tuple[Image.Image, List[str]]:
    """Draw the panel from a ``display_data`` document (may be empty)."""
    data = display_data or {}
    fonts = _load_fonts(font_path)
    image = Image.new("1", (width, height), 255)
    p = _Painter(image)
    when = clock.local_now(now)

    glyph_start = _header(p, fonts, width, when)
    _glyphs(p, data, glyph_start)
    p.line((0, _Y_LINE_1, width, _Y_LINE_1))
    if data.get("warming_up"):
        _warming(p, fonts, width)
    else:
        _numbers(p, fonts, width, data)
    p.line((0, _Y_LINE_2, width, _Y_LINE_2))
    p.line((0, _Y_LINE_3, width, _Y_LINE_3))
    _weather(p, fonts, width, height, data, icons_dir)
    p.draw.rectangle([0, 0, width - 1, height - 1], outline=0, width=1)
    return image, p.painted
