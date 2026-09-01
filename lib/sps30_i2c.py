import time
from struct import pack, unpack
from typing import Dict, Iterable, List, Optional, Tuple

import adafruit_bus_device.i2c_device as i2c_device


class SPS30Error(RuntimeError):
    """Raised when the SPS30 returns invalid data or an invalid state."""


class SPS30:
    FIELD_NAMES = (
        "pm1",
        "pm25",
        "pm4",
        "pm10",
        "nc05",
        "nc10",
        "nc25",
        "nc40",
        "nc100",
        "tps",
    )

    _CMD_START = 0x0010
    _CMD_STOP = 0x0104
    _CMD_DATA_READY = 0x0202
    _CMD_READ_MEASURED_VALUES = 0x0300
    _CMD_SLEEP = 0x1001
    _CMD_WAKEUP = 0x1103
    _CMD_FAN_CLEANING = 0x5607
    _CMD_AUTO_CLEANING_INTERVAL = 0x8004
    _CMD_VERSION = 0xD100
    _CMD_RESET = 0xD304

    _FLOATING_POINT_MODE = 0x0300
    _MEASUREMENT_RESPONSE_BYTES = 60

    def __init__(self, i2c_bus, address: int = 0x69):
        self._device = i2c_device.I2CDevice(i2c_bus, address)
        self._rx_buffer = bytearray(self._MEASUREMENT_RESPONSE_BYTES)
        self._tx_buffer = bytearray(8)
        self.firmware_version: Optional[Tuple[int, int]] = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.stop_measurement()
        finally:
            self.sleep()

    @staticmethod
    def _crc8(data: bytes) -> int:
        crc = 0xFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x80:
                    crc = ((crc << 1) ^ 0x31) & 0xFF
                else:
                    crc = (crc << 1) & 0xFF
        return crc

    def _command(
        self,
        command: int,
        arguments: Iterable[int] = (),
        response_bytes: int = 0,
        response_delay: float = 0.01,
    ) -> memoryview:
        tx_length = 2
        self._tx_buffer[0] = (command >> 8) & 0xFF
        self._tx_buffer[1] = command & 0xFF

        for argument in arguments:
            word = pack(">H", argument)
            self._tx_buffer[tx_length : tx_length + 2] = word
            self._tx_buffer[tx_length + 2] = self._crc8(word)
            tx_length += 3

        with self._device as device:
            device.write(self._tx_buffer, end=tx_length)
            if response_bytes:
                time.sleep(response_delay)
                device.readinto(self._rx_buffer, end=response_bytes)
                return memoryview(self._rx_buffer)[:response_bytes]

        return memoryview(bytearray())

    def _decode_words(self, response: memoryview) -> List[int]:
        if len(response) % 3 != 0:
            raise SPS30Error("Unexpected SPS30 response size")

        words: List[int] = []
        for offset in range(0, len(response), 3):
            chunk = bytes(response[offset : offset + 2])
            crc = response[offset + 2]
            if self._crc8(chunk) != crc:
                raise SPS30Error("SPS30 CRC mismatch")
            words.append(unpack(">H", chunk)[0])
        return words

    def wakeup(self) -> None:
        # In sleep the I2C interface is off: the first 0x1103 is expected to
        # NAK (it only powers the interface), the second must ACK. Swallowing
        # BOTH would make a dead/absent sensor "wake" successfully and push
        # the failure to a later, more confusing step.
        try:
            self._command(self._CMD_WAKEUP)
        except OSError:
            pass
        time.sleep(0.02)
        self._command(self._CMD_WAKEUP)
        time.sleep(0.02)

    def sleep(self) -> None:
        self._command(self._CMD_SLEEP)
        time.sleep(0.02)

    def start_measurement(self) -> None:
        self._command(self._CMD_START, arguments=(self._FLOATING_POINT_MODE,))
        time.sleep(0.05)
        if self.firmware_version is None:
            self.firmware_version = self.read_firmware()

    def stop_measurement(self) -> None:
        self._command(self._CMD_STOP)
        time.sleep(0.05)

    def reset(self) -> None:
        self._command(self._CMD_RESET)
        time.sleep(0.1)

    def force_clean(self) -> None:
        self._command(self._CMD_FAN_CLEANING)

    @property
    def auto_cleaning_interval(self) -> int:
        response = self._command(
            self._CMD_AUTO_CLEANING_INTERVAL, response_bytes=6, response_delay=0.02
        )
        words = self._decode_words(response)
        return (words[0] << 16) | words[1]

    @auto_cleaning_interval.setter
    def auto_cleaning_interval(self, seconds: int) -> None:
        if seconds < 0:
            raise ValueError("auto cleaning interval must be non-negative")
        self._command(
            self._CMD_AUTO_CLEANING_INTERVAL,
            arguments=((seconds >> 16) & 0xFFFF, seconds & 0xFFFF),
        )

    @property
    def data_ready(self) -> bool:
        response = self._command(
            self._CMD_DATA_READY, response_bytes=3, response_delay=0.02
        )
        words = self._decode_words(response)
        return bool(words[0])

    def read_firmware(self) -> Tuple[int, int]:
        response = self._command(
            self._CMD_VERSION, response_bytes=3, response_delay=0.02
        )
        words = self._decode_words(response)
        version = words[0]
        return ((version >> 8) & 0xFF, version & 0xFF)

    def read(self) -> Dict[str, float]:
        response = self._command(
            self._CMD_READ_MEASURED_VALUES,
            response_bytes=self._MEASUREMENT_RESPONSE_BYTES,
            response_delay=0.02,
        )
        words = self._decode_words(response)

        if len(words) != len(self.FIELD_NAMES) * 2:
            raise SPS30Error("Unexpected SPS30 measurement payload length")

        payload = bytearray()
        for word in words:
            payload.extend(pack(">H", word))

        values = unpack(">" + "f" * len(self.FIELD_NAMES), payload)
        return {
            field_name: round(value, 3)
            for field_name, value in zip(self.FIELD_NAMES, values)
        }
