"""Import-time stand-ins for the Pi hardware libraries.

``install()`` puts fake ``board``, ``busio``, ``RPi.GPIO``, ``spidev``,
``adafruit_bus_device``, ``adafruit_scd4x`` and ``adafruit_sht4x`` modules
into ``sys.modules`` so the apps import on a machine with none of them. The
test suite calls it from ``conftest.py``; ``python -m collector --fake`` and
the demo call it too. The fakes have benign defaults; tests that need to
script behaviour use ``tests/mocks/fake_devices.py``.
"""

import sys
import types


def _module(name: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def install() -> None:
    if "board" in sys.modules:  # already installed
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
    # Scriptable: set RPi.GPIO.pin_values[pin] to an int or a zero-arg
    # callable to script the display's BUSY pin; RPi.GPIO.outputs records
    # every output() call as (pin, value).
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

        def measure_single_shot(self):
            pass

        def self_test(self):
            pass

        def power_down(self):
            pass

        def wake_up(self):
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

        @property
        def ambient_pressure(self):
            return int(self.ambient_pressures[-1]) if self.ambient_pressures else 0

        sensor_variant_name = "SCD41"

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
