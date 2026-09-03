"""Test bootstrap: make the apps importable on a machine with no Pi hardware.

The collector and the manager import `board`, `busio`, `RPi.GPIO`, `spidev`
and the Adafruit sensor drivers at module level. None of those exist off-Pi,
so fake modules are injected into ``sys.modules`` BEFORE any app import.

The fakes here are import-time stand-ins with benign defaults. Tests that
need to script sensor behaviour use the classes in ``tests/mocks/fake_devices.py``.
"""

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _install_fake_hardware() -> None:
    if "board" in sys.modules:  # already installed (pytest re-imports conftest)
        return

    # --- busio / board ---------------------------------------------------
    class FakeI2C:
        def __init__(self, *args, **kwargs):
            pass

        def deinit(self):
            pass

    busio = _module("busio")
    busio.I2C = FakeI2C

    board = _module("board")
    board.SCL = "SCL"
    board.SDA = "SDA"
    board.I2C = lambda: FakeI2C()

    # --- RPi.GPIO ---------------------------------------------------------
    # Scriptable: tests may set RPi.GPIO.pin_values[pin] to an int or a
    # zero-arg callable (e.g. "busy for N polls, then idle") to exercise the
    # display driver's BUSY handling; RPi.GPIO.outputs records every
    # output() call as (pin, value). Defaults keep the benign behaviour.
    class FakeGPIO:
        BCM = "BCM"
        IN = "IN"
        OUT = "OUT"
        LOW = 0
        HIGH = 1
        pin_values = {}
        outputs = []

        @staticmethod
        def setwarnings(_flag):
            pass

        @staticmethod
        def setmode(_mode):
            pass

        @staticmethod
        def setup(_pin, _direction):
            pass

        @classmethod
        def output(cls, pin, value):
            cls.outputs.append((pin, value))

        @classmethod
        def input(cls, pin):
            value = cls.pin_values.get(pin, 1)  # default: busy pin idle
            return value() if callable(value) else value

        @staticmethod
        def cleanup(_pins=None):
            pass

    rpi = _module("RPi")
    gpio_module = _module("RPi.GPIO")
    for name in dir(FakeGPIO):
        if not name.startswith("_"):
            setattr(gpio_module, name, getattr(FakeGPIO, name))
    rpi.GPIO = gpio_module

    # --- spidev -----------------------------------------------------------
    class FakeSpiDev:
        def __init__(self):
            self.max_speed_hz = 0
            self.mode = 0
            self.written = []
            self.raise_on_write = None  # set an Exception to fail SPI writes

        def open(self, _bus, _device):
            pass

        def writebytes(self, data):
            if self.raise_on_write is not None:
                raise self.raise_on_write
            self.written.append(bytes(data))

        def writebytes2(self, data):
            if self.raise_on_write is not None:
                raise self.raise_on_write
            self.written.append(bytes(data))

        def close(self):
            pass

    spidev = _module("spidev")
    spidev.SpiDev = FakeSpiDev

    # --- Adafruit sensor drivers -----------------------------------------
    class FakeSCD4X:
        """Benign default: always ready, plausible CO2."""

        def __init__(self, _i2c):
            self.data_ready = True
            self.CO2 = 600.0
            self.temperature = 23.0
            self.relative_humidity = 40.0
            self.self_calibration_enabled = False
            self.altitude = 0
            self.temperature_offset = 4.0
            self.serial_number = (0x11, 0x22, 0x33)
            self.ambient_pressures = []

        def start_periodic_measurement(self):
            pass

        def stop_periodic_measurement(self):
            pass

        def reinit(self):
            pass

        def force_calibration(self, _target):
            return 0

        def persist_settings(self):
            pass

        def set_ambient_pressure(self, hpa):
            self.ambient_pressures.append(hpa)

    adafruit_scd4x = _module("adafruit_scd4x")
    adafruit_scd4x.SCD4X = FakeSCD4X

    class FakeMode:
        NOHEAT_HIGHPRECISION = 0xFD
        NOHEAT_MEDPRECISION = 0xF6
        NOHEAT_LOWPRECISION = 0xE0

    class FakeSHT4x:
        def __init__(self, _i2c):
            self.temperature = 22.5
            self.relative_humidity = 45.0
            self.mode = FakeMode.NOHEAT_HIGHPRECISION
            self.serial_number = 0xABCD

    adafruit_sht4x = _module("adafruit_sht4x")
    adafruit_sht4x.SHT4x = FakeSHT4x
    adafruit_sht4x.Mode = FakeMode

    class FakeI2CDevice:
        """Minimal I2CDevice: records writes, reads back zeros."""

        def __init__(self, _i2c, _address):
            self.written = []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def write(self, buffer, end=None):
            self.written.append(bytes(buffer[: end if end is not None else len(buffer)]))

        def readinto(self, buffer, end=None):
            for index in range(end if end is not None else len(buffer)):
                buffer[index] = 0

    adafruit_bus_device = _module("adafruit_bus_device")
    i2c_device = _module("adafruit_bus_device.i2c_device")
    i2c_device.I2CDevice = FakeI2CDevice
    adafruit_bus_device.i2c_device = i2c_device


_install_fake_hardware()


import pytest  # noqa: E402  (after the fakes exist)


@pytest.fixture
def tmp_config(tmp_path):
    """The shipped config with database and logs redirected under tmp_path."""
    import tomllib

    from shared.config import DEFAULT_PATH, Config

    with open(DEFAULT_PATH, "rb") as handle:
        raw = tomllib.load(handle)
    raw["paths"]["database"] = str(tmp_path / "data" / "airstation.db")
    raw["paths"]["logs"] = str(tmp_path / "data" / "logs")
    raw["logging"]["level"] = "debug"
    return Config.from_dict(raw, repo_root=REPO_ROOT, source=tmp_path / "config.toml")


@pytest.fixture
def db(tmp_config):
    from shared.db import Database

    database = Database(tmp_config.paths.database)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def _reset_fake_gpio():
    """Scripted GPIO state must never leak between tests."""
    import RPi.GPIO as gpio

    gpio.pin_values.clear()
    gpio.outputs.clear()
    yield
    gpio.pin_values.clear()
    gpio.outputs.clear()
