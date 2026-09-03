"""
UC8253C E-Paper Display Driver for the WeAct 3.7" 240x416 panel.
"""

import time

import RPi.GPIO as GPIO
import spidev


class UC8253C_SPI:
    _CMD_PANEL_SETTING = 0x00
    _CMD_POWER_OFF = 0x02
    _CMD_POWER_ON = 0x04
    _CMD_DEEP_SLEEP = 0x07
    _CMD_OLD_IMAGE = 0x10
    _CMD_DISPLAY_REFRESH = 0x12
    _CMD_NEW_IMAGE = 0x13
    _CMD_VCOM_AND_DATA_INTERVAL = 0x50
    _CMD_CASCADE = 0xE0
    _CMD_FORCE_TEMPERATURE = 0xE5

    _NATIVE_WIDTH = 240
    _NATIVE_HEIGHT = 416

    MODE_FULL = "FULL"
    MODE_FAST = "FAST"
    MODE_PARTIAL = "PARTIAL"

    def __init__(
        self,
        rst_pin: int = 17,
        dc_pin: int = 25,
        busy_pin: int = 24,
        spi_bus: int = 0,
        spi_device: int = 0,
        rotation: int = 90,
    ):
        self.rst_pin = rst_pin
        self.dc_pin = dc_pin
        self.busy_pin = busy_pin
        self.rotation = rotation
        self.spi = None
        self.is_sleeping = True
        self.current_mode = self.MODE_FULL
        self._bank_swapped = False
        self._framebuffer = bytearray(b"\xFF" * self.buffer_size)

        if rotation in (90, 270):
            self.width = self._NATIVE_HEIGHT
            self.height = self._NATIVE_WIDTH
        else:
            self.width = self._NATIVE_WIDTH
            self.height = self._NATIVE_HEIGHT

        self._init_gpio()
        self._init_spi(spi_bus, spi_device)

    @property
    def buffer_size(self) -> int:
        return (self._NATIVE_WIDTH * self._NATIVE_HEIGHT) // 8

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _init_gpio(self) -> None:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.busy_pin, GPIO.IN)
        GPIO.setup(self.rst_pin, GPIO.OUT)
        GPIO.setup(self.dc_pin, GPIO.OUT)

    def _init_spi(self, bus: int, device: int) -> None:
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = 4_000_000
        self.spi.mode = 0b00

    def _write_command(self, command: int) -> None:
        GPIO.output(self.dc_pin, GPIO.LOW)
        self.spi.writebytes([command])

    def _write_data(self, data) -> None:
        GPIO.output(self.dc_pin, GPIO.HIGH)
        if isinstance(data, int):
            self.spi.writebytes([data])
        elif isinstance(data, list):
            self.spi.writebytes(data)
        else:
            self.spi.writebytes2(data)

    def _write(self, command: int, data=None) -> None:
        self._write_command(command)
        if data is not None:
            self._write_data(data)

    def _wait_until_idle(self, timeout_seconds: float = 15.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        time.sleep(0.02)
        while GPIO.input(self.busy_pin) == 0:
            if time.monotonic() >= deadline:
                raise TimeoutError("UC8253C busy pin timeout")
            time.sleep(0.01)
        time.sleep(0.02)

    def _hardware_reset(self) -> None:
        GPIO.output(self.rst_pin, GPIO.LOW)
        time.sleep(0.05)
        GPIO.output(self.rst_pin, GPIO.HIGH)
        time.sleep(0.05)
        self._bank_swapped = False
        self.is_sleeping = False

    def _wake(self) -> None:
        self._hardware_reset()
        try:
            self._wait_until_idle()
            self._write(self._CMD_POWER_ON)
            self._wait_until_idle()
            self._write(self._CMD_PANEL_SETTING, [0x1F, 0x0D])
            self._apply_mode(self.current_mode)
        except TimeoutError:
            # _hardware_reset marked us awake, but the init never finished —
            # a half-initialized controller must not count as awake, or the
            # next display_image writes frames into the void. Re-arm the
            # full reset+init for the next attempt.
            self.is_sleeping = True
            raise
        self.is_sleeping = False

    def _ensure_awake(self) -> None:
        if self.is_sleeping:
            self._wake()

    def _apply_mode(self, mode: str) -> None:
        self.current_mode = mode

        if mode == self.MODE_FULL:
            self._write(self._CMD_VCOM_AND_DATA_INTERVAL, 0x97)
            return

        self._write(self._CMD_CASCADE, 0x02)
        if mode == self.MODE_FAST:
            self._write(self._CMD_FORCE_TEMPERATURE, 0x5F)
        elif mode == self.MODE_PARTIAL:
            self._write(self._CMD_FORCE_TEMPERATURE, 0x6E)
        else:
            raise ValueError(f"Unsupported refresh mode: {mode}")
        self._write(self._CMD_VCOM_AND_DATA_INTERVAL, 0xD7)

    def _frame_commands(self) -> tuple[int, int]:
        if self._bank_swapped:
            return (self._CMD_NEW_IMAGE, self._CMD_OLD_IMAGE)
        return (self._CMD_OLD_IMAGE, self._CMD_NEW_IMAGE)

    def clear(self, auto_sleep: bool = True) -> None:
        self._ensure_awake()
        self._apply_mode(self.MODE_FULL)

        blank = bytearray(b"\xFF" * self.buffer_size)
        old_command, new_command = self._frame_commands()
        self._write(old_command, blank)
        self._write(new_command, blank)
        self._write(self._CMD_DISPLAY_REFRESH)
        self._wait_until_idle()

        self._framebuffer = blank
        self._bank_swapped = not self._bank_swapped

        if auto_sleep:
            self.sleep()

    def display_image(
        self, image, mode: str = MODE_PARTIAL, auto_sleep: bool = True
    ) -> None:
        if image.width != self.width or image.height != self.height:
            raise ValueError(
                f"Expected image size {self.width}x{self.height}, got {image.width}x{image.height}"
            )

        self._ensure_awake()
        self._apply_mode(mode)

        if self.rotation:
            image = image.rotate(self.rotation, expand=True)

        new_frame = bytearray(image.convert("1").tobytes())
        old_command, new_command = self._frame_commands()
        try:
            self._write(old_command, self._framebuffer)
            self._write(new_command, new_frame)
            self._write(self._CMD_DISPLAY_REFRESH)
            self._wait_until_idle()
        except TimeoutError:
            # Both RAM banks may already hold the new frame while our
            # shadow state does not — recovering without a reset would
            # ghost/invert the next partial refresh. Route the next
            # operation through _wake's hardware reset.
            self.is_sleeping = True
            raise

        self._framebuffer = new_frame
        self._bank_swapped = not self._bank_swapped

        if auto_sleep:
            self.sleep()

    def update(self, image, auto_sleep: bool = True) -> None:
        self.display_image(image, mode=self.current_mode, auto_sleep=auto_sleep)

    def sleep(self) -> None:
        if self.is_sleeping:
            return
        try:
            self._write(self._CMD_POWER_OFF)
            self._wait_until_idle()
            self._write(self._CMD_DEEP_SLEEP, 0xA5)
        except TimeoutError:
            # The panel is now in an unknown power state; claiming "awake"
            # would let the next display_image write frames to a dead
            # controller. Marking sleeping forces the next operation through
            # _wake's hardware reset, from which every state converges.
            self.is_sleeping = True
            raise
        self.is_sleeping = True

    def close(self) -> None:
        try:
            self.sleep()
        finally:
            if self.spi is not None:
                self.spi.close()
                self.spi = None
            GPIO.cleanup([self.rst_pin, self.dc_pin, self.busy_pin])
