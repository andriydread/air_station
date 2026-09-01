"""UC8253C driver regression tests against the recorded SPI byte stream.

The conftest fakes (GPIO always idle, FakeSpiDev recording writes) let the
real driver run; these tests pin the frame-bank protocol that partial
refresh correctness depends on.
"""

import pytest
from PIL import Image

from lib.uc8253c import UC8253C_SPI

OLD_CMD = UC8253C_SPI._CMD_OLD_IMAGE   # 0x10
NEW_CMD = UC8253C_SPI._CMD_NEW_IMAGE   # 0x13


def _frame_command_order(spi_written):
    """The 0x10/0x13 command bytes in the order they hit the wire."""
    return [entry[0] for entry in spi_written if len(entry) == 1 and entry[0] in (OLD_CMD, NEW_CMD)]


def test_partial_refresh_alternates_frame_banks():
    display = UC8253C_SPI()
    image_a = Image.new("1", (display.width, display.height), 255)
    image_b = Image.new("1", (display.width, display.height), 0)

    display.display_image(image_a, mode=display.MODE_PARTIAL, auto_sleep=False)
    first = _frame_command_order(display.spi.written)
    display.spi.written.clear()
    display.display_image(image_b, mode=display.MODE_PARTIAL, auto_sleep=False)
    second = _frame_command_order(display.spi.written)

    # Bank roles swap between refreshes; a regression here means inverted
    # or ghosted partial refreshes on the physical panel.
    assert first == [OLD_CMD, NEW_CMD]
    assert second == [NEW_CMD, OLD_CMD]


def test_old_frame_payload_is_the_previous_new_frame():
    display = UC8253C_SPI()
    image_a = Image.new("1", (display.width, display.height), 255)
    image_b = Image.new("1", (display.width, display.height), 0)

    display.display_image(image_a, mode=display.MODE_PARTIAL, auto_sleep=False)
    frame_a = bytes(image_a.rotate(display.rotation, expand=True).convert("1").tobytes())

    display.spi.written.clear()
    display.display_image(image_b, mode=display.MODE_PARTIAL, auto_sleep=False)
    written = display.spi.written
    old_index = written.index(bytes([NEW_CMD]))  # swapped: 0x13 now carries OLD
    assert written[old_index + 1] == frame_a


def test_display_image_rejects_wrong_size():
    display = UC8253C_SPI()
    with pytest.raises(ValueError, match="Expected image size"):
        display.display_image(Image.new("1", (10, 10), 255))


def test_hardware_reset_clears_the_bank_swap():
    display = UC8253C_SPI()
    image = Image.new("1", (display.width, display.height), 255)
    display.display_image(image, mode=display.MODE_PARTIAL, auto_sleep=False)
    assert display._bank_swapped is True
    display._hardware_reset()
    assert display._bank_swapped is False  # controller RAM is gone with the reset
