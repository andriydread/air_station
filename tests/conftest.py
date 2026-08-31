"""Test bootstrap: make the app importable on a machine with no Pi hardware.

The collector imports `board`, `busio`, `RPi.GPIO`, `spidev` and the
Adafruit sensor drivers at module level. None of those exist off-Pi, so
fake modules are injected into ``sys.modules`` BEFORE any app import.

The fakes here are import-time stand-ins with benign defaults. Tests that
need to script sensor behaviour replace the device classes per-test (see
``tests/mocks/fake_devices.py``).
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

    busio = _module("busio")
    busio.I2C = FakeI2C

    board = _module("board")
    board.SCL = "SCL"
    board.SDA = "SDA"
    board.I2C = lambda: FakeI2C()

    # --- RPi.GPIO ---------------------------------------------------------
    class FakeGPIO:
        BCM = "BCM"
        IN = "IN"
        OUT = "OUT"
        LOW = 0
        HIGH = 1

        @staticmethod
        def setwarnings(_flag):
            pass

        @staticmethod
        def setmode(_mode):
            pass

        @staticmethod
        def setup(_pin, _direction):
            pass

        @staticmethod
        def output(_pin, _value):
            pass

        @staticmethod
        def input(_pin):
            return 1  # busy pin idle: display never blocks in tests

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

        def open(self, _bus, _device):
            pass

        def writebytes(self, data):
            self.written.append(bytes(data))

        def writebytes2(self, data):
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

    adafruit_scd4x = _module("adafruit_scd4x")
    adafruit_scd4x.SCD4X = FakeSCD4X

    class FakeSHT4x:
        def __init__(self, _i2c):
            self.temperature = 22.5
            self.relative_humidity = 45.0

    adafruit_sht4x = _module("adafruit_sht4x")
    adafruit_sht4x.SHT4x = FakeSHT4x

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
