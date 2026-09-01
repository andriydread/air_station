"""
Display Renderer Utility
Generates the 1-bit Black/White Image to be pushed to the E-Paper display.
"""

import logging
import os
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from utils.aqi import calculate_aqi, get_aqi_category, get_co2_category

LOGGER = logging.getLogger("airmonitor")

# Location of icon PNGs
ICONS_DIR = os.path.join("assets", "icons")

ACTIVE_ICON_MAP = {
    0: "sun.png",
    1: "sun.png",
    2: "partly_cloudy.png",
    3: "cloud.png",
    45: "fog.png",
    48: "fog.png",
    51: "rain.png",
    53: "rain.png",
    55: "rain.png",
    56: "rain.png",
    57: "rain.png",
    61: "rain.png",
    63: "rain.png",
    65: "rain.png",
    66: "rain.png",
    67: "rain.png",
    71: "snow.png",
    73: "snow.png",
    75: "snow.png",
    77: "snow.png",
    80: "rain.png",
    81: "rain.png",
    82: "rain.png",
    85: "snow.png",
    86: "snow.png",
    95: "storm.png",
    96: "storm.png",
    99: "storm.png",
}

# --- Text Alignment Helpers ---


def center_text(draw, text, font, x_start, x_end, y_pos):
    text_w = draw.textlength(text, font=font)
    center_x = x_start + (x_end - x_start - text_w) / 2
    draw.text((center_x, y_pos), text, font=font, fill=0)


def draw_left_text(draw, text, font, x_pad, y_pos):
    draw.text((x_pad, y_pos), text, font=font, fill=0)


def draw_right_text(draw, text, font, width, x_pad, y_pos):
    text_w = draw.textlength(text, font=font)
    draw.text((width - text_w - x_pad, y_pos), text, font=font, fill=0)


# --- Status Glyphs ---
# Tiny 1-bit header icons so the physical screen can say "station broken"
# instead of silently showing dashes (R9). Drawn with PIL primitives only.


def _draw_wifi_down(draw, x, y, size):
    cx = x + size / 2
    cy = y + size * 0.9
    for radius in (size * 0.45, size * 0.25):
        draw.arc(
            [cx - radius, cy - radius, cx + radius, cy + radius],
            225, 315, fill=0, width=2,
        )
    draw.ellipse([cx - 2, cy - 3, cx + 2, cy + 1], fill=0)
    draw.line([x + 1, y + size - 2, x + size - 1, y + 1], fill=0, width=2)


def _draw_power_issue(draw, x, y, size):
    draw.polygon(
        [(x + size / 2, y), (x, y + size - 1), (x + size - 1, y + size - 1)],
        outline=0,
    )
    cx = x + size / 2
    draw.line([cx, y + size * 0.35, cx, y + size * 0.65], fill=0, width=2)
    draw.ellipse([cx - 1, y + size * 0.75, cx + 1, y + size * 0.75 + 2], fill=0)


def _draw_sensor_fault(draw, x, y, size):
    draw.ellipse([x, y, x + size - 1, y + size - 1], outline=0, width=2)
    cx = x + size / 2
    draw.line([cx, y + size * 0.25, cx, y + size * 0.55], fill=0, width=2)
    draw.ellipse([cx - 1, y + size * 0.68, cx + 1, y + size * 0.68 + 2], fill=0)


def _draw_status_glyphs(draw, status, left_start):
    """Draw up to 3 problem glyphs rightwards from `left_start` (after the clock)."""
    size = 18
    y = 6
    glyphs = []
    if status.get("network") is False:
        glyphs.append(_draw_wifi_down)
    if status.get("power") is False:
        glyphs.append(_draw_power_issue)
    if status.get("sensors") is False:
        glyphs.append(_draw_sensor_fault)
    for index, glyph in enumerate(glyphs):
        x = left_start + index * (size + 8)
        glyph(draw, x, y, size)


# --- Font Loading ---

_FONT_SIZES = (36, 24, 18, 16, 14)  # huge, large, medium, small, extra-small
_font_cache = {}


def _load_fonts(font_path):
    """Load the display fonts once and cache them.

    Falls back to common Pi OS fonts, then to Pillow's built-in bitmap font.
    """
    if font_path in _font_cache:
        return _font_cache[font_path]

    paths_to_try = [
        font_path,
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]
    fonts = None
    for path in paths_to_try:
        if path and os.path.exists(path):
            try:
                fonts = tuple(ImageFont.truetype(path, size) for size in _FONT_SIZES)
                break
            except Exception as exc:
                LOGGER.warning("Failed to load font %s: %s", path, exc)

    if fonts is None:
        LOGGER.warning("No TrueType font available; using Pillow's default bitmap font")
        fonts = (ImageFont.load_default(),) * len(_FONT_SIZES)

    _font_cache[font_path] = fonts
    return fonts


# --- Main Render Pipeline ---


def create_display_image(width, height, data, font_path=None):
    """
    Builds the UI layer by layer onto a purely 1-bit (White=255, Black=0) canvas.
    """
    image = Image.new("1", (width, height), 255)
    draw = ImageDraw.Draw(image)

    fonts = _load_fonts(font_path)
    font_huge, font_lg, font_md, font_sm, font_xs = fonts

    # Values
    if isinstance(data.get("pm25"), (int, float)) and isinstance(
        data.get("pm10"), (int, float)
    ):
        aqi_val = calculate_aqi(data.get("pm25"), data.get("pm10"))
        aqi_cat = get_aqi_category(aqi_val)
    else:
        aqi_val = None
        aqi_cat = None

    co2_val = int(data.get("co2")) if isinstance(data.get("co2"), (int, float)) else None

    temp = float(data.get("temp")) if isinstance(data.get("temp"), (int, float)) else None
    humid = float(data.get("humid")) if isinstance(data.get("humid"), (int, float)) else None

    # Layout Grid (Horizontal Dividers)
    Y_LINE_1, Y_LINE_2, Y_LINE_3 = 30, 92, 122
    EDGE_PAD = 12

    # --- 1. HEADER (Time & Date + status glyphs) ---
    now = datetime.now()
    time_text = now.strftime("%H:%M")
    draw_left_text(draw, time_text, font_sm, EDGE_PAD, 5)
    center_text(draw, now.strftime("%A"), font_sm, 0, width, 5)
    draw_right_text(draw, now.strftime("%d/%m/%Y"), font_sm, width, EDGE_PAD, 5)
    status = data.get("status") or {}
    if status:
        # Right of the clock: the gap before the centered weekday fits all
        # three glyphs even on the narrow axis.
        time_width = draw.textlength(time_text, font=font_sm)
        _draw_status_glyphs(draw, status, EDGE_PAD + time_width + 12)
    draw.line((0, Y_LINE_1, width, Y_LINE_1), fill=0, width=1)

    # --- 2. SENSOR DATA (AQI & CO2) ---

    aqi_text = f"AQI: {aqi_val}" if aqi_val is not None else "AQI: --"
    aqi_category = aqi_cat if aqi_cat is not None else "N/A"
    draw_left_text(draw, aqi_text, font_huge, EDGE_PAD, Y_LINE_1 + 2)
    draw_left_text(draw, aqi_category, font_md, EDGE_PAD, Y_LINE_1 + 38)

    co2_text = f"CO2: {co2_val}" if co2_val is not None else "CO2: --"
    draw_right_text(draw, co2_text, font_huge, width, EDGE_PAD, Y_LINE_1 + 2)
    draw_right_text(
        draw, get_co2_category(co2_val), font_md, width, EDGE_PAD, Y_LINE_1 + 38
    )

    draw.line((0, Y_LINE_2, width, Y_LINE_2), fill=0, width=1)

    # --- 3. SENSOR DATA (Temp & Humidity) ---

    temp_str = f"Temp: {temp:.1f}°" if temp is not None else "Temp: --"
    humid_str = f"Humid: {humid:.1f} %" if humid is not None else "Humid: --"

    draw_left_text(draw, temp_str, font_lg, EDGE_PAD, Y_LINE_2 + 2)
    draw_right_text(draw, humid_str, font_lg, width, EDGE_PAD, Y_LINE_2 + 2)
    draw.line((0, Y_LINE_3, width, Y_LINE_3), fill=0, width=1)

    # --- 4. WEATHER FORECAST (Hourly Blocks) ---
    col_w = width // 3
    draw.line((col_w, Y_LINE_3, col_w, height), fill=0, width=1)
    draw.line((col_w * 2, Y_LINE_3, col_w * 2, height), fill=0, width=1)

    icon_size = 70

    for i in range(3):
        col_start = i * col_w
        col_end = (i + 1) * col_w
        icon_x = col_start + (col_w - icon_size) // 2
        icon_y = Y_LINE_3 + 18

        # Safely extract data. JSON parses integer dict keys as strings, so check both.
        block_data = data.get(i + 1) or data.get(str(i + 1))

        if block_data and len(block_data) == 5:
            time_str, t_max, t_min, precip, wmo_code = block_data
        else:
            time_str, t_max, t_min, precip, wmo_code = "---", None, None, None, None

        # Draw Time Period
        center_text(draw, time_str, font_xs, col_start, col_end, Y_LINE_3 + 2)

        # Process and Draw Weather Icon
        if wmo_code is not None:
            icon_file = ACTIVE_ICON_MAP.get(wmo_code, "sun.png")
            icon_path = os.path.join(ICONS_DIR, icon_file)

            try:
                if os.path.exists(icon_path):
                    # Load icon, convert transparent backgrounds to pure white, then map grayscale to 1-bit
                    img_icon = (
                        Image.open(icon_path)
                        .convert("RGBA")
                        .resize((icon_size, icon_size), Image.Resampling.LANCZOS)
                    )
                    bg = Image.new("RGBA", (icon_size, icon_size), (255, 255, 255, 255))

                    # Point(< 140) sets a threshold to avoid fuzzy dithering dots on the e-paper screen
                    final_icon = (
                        Image.alpha_composite(bg, img_icon)
                        .convert("L")
                        .point(lambda p: 0 if p < 140 else 255)
                        .convert("1")
                    )
                    image.paste(final_icon, (int(icon_x), int(icon_y)))
                else:
                    # Missing icon placeholder
                    draw.rectangle(
                        [icon_x, icon_y, icon_x + icon_size, icon_y + icon_size],
                        outline=0,
                    )
            except Exception as exc:
                LOGGER.warning("Error drawing icon %s: %s", icon_path, exc)
        else:
            draw.rectangle(
                [icon_x, icon_y, icon_x + icon_size, icon_y + icon_size], outline=0
            )
            center_text(
                draw,
                "N/A",
                font_xs,
                icon_x,
                icon_x + icon_size,
                icon_y + (icon_size // 2) - 8,
            )

        # Draw Min/Max Temps
        temp_text = (
            f"{t_max:.1f}/{t_min:.1f}"
            if (t_max is not None and t_min is not None)
            else "--/--"
        )
        center_text(
            draw, temp_text, font_md, col_start, col_end, icon_y + icon_size - 4
        )

        # Draw Rain %
        rain_text = f"Rain:{precip}%" if precip is not None else "Rain:--%"
        center_text(
            draw, rain_text, font_xs, col_start, col_end, icon_y + icon_size + 15
        )

    # --- 5. BORDER ---
    draw.rectangle([0, 0, width - 1, height - 1], outline=0, width=1)

    return image
