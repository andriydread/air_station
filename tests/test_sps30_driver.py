"""drivers.sps30_i2c driver tests: CRC math and frame decoding against vectors."""

from struct import pack

import pytest

from tests.mocks.fake_devices import ScriptedI2CDevice
from drivers.sps30_i2c import SPS30, SPS30Error


@pytest.fixture
def sps30():
    device = SPS30(object())  # conftest FakeI2CDevice absorbs the constructor
    device._device = ScriptedIWrapper = ScriptedI2CDevice()
    return device, ScriptedIWrapper


def encode_words(raw: bytes) -> bytes:
    """Sensirion wire format: every 2 data bytes followed by their CRC8."""
    framed = bytearray()
    for offset in range(0, len(raw), 2):
        chunk = raw[offset : offset + 2]
        framed.extend(chunk)
        framed.append(SPS30._crc8(chunk))
    return bytes(framed)


def test_crc8_datasheet_vector():
    # From the Sensirion SPS30 datasheet: CRC of {0xBE, 0xEF} is 0x92.
    assert SPS30._crc8(bytes([0xBE, 0xEF])) == 0x92


def test_decode_words_rejects_corrupt_crc(sps30):
    device, _scripted = sps30
    good = encode_words(pack(">H", 0x1234))
    assert device._decode_words(memoryview(good)) == [0x1234]
    corrupt = bytearray(good)
    corrupt[2] ^= 0xFF
    with pytest.raises(SPS30Error, match="CRC"):
        device._decode_words(memoryview(bytes(corrupt)))


def test_decode_words_rejects_bad_length(sps30):
    device, _scripted = sps30
    with pytest.raises(SPS30Error, match="size"):
        device._decode_words(memoryview(b"\x00\x01"))


def test_data_ready_parses_flag(sps30):
    device, scripted = sps30
    scripted.responses.append(encode_words(pack(">H", 1)))
    assert device.data_ready is True
    scripted.responses.append(encode_words(pack(">H", 0)))
    assert device.data_ready is False


def test_read_decodes_measurement_frame(sps30):
    device, scripted = sps30
    values = (1.5, 2.5, 3.5, 4.5, 10.0, 20.0, 30.0, 40.0, 50.0, 0.65)
    raw = b"".join(pack(">f", value) for value in values)
    scripted.responses.append(encode_words(raw))
    result = device.read()
    assert result["pm1"] == 1.5
    assert result["pm25"] == 2.5
    assert result["pm10"] == 4.5
    assert result["tps"] == 0.65
    # the written command must be READ_MEASURED_VALUES (0x0300)
    assert scripted.written[-1] == bytes([0x03, 0x00])


def test_read_rejects_corrupt_measurement(sps30):
    device, scripted = sps30
    raw = b"".join(pack(">f", 1.0) for _ in range(10))
    frame = bytearray(encode_words(raw))
    frame[5] ^= 0xFF  # corrupt one CRC byte
    scripted.responses.append(bytes(frame))
    with pytest.raises(SPS30Error):
        device.read()


def test_auto_cleaning_interval_roundtrip_encoding(sps30):
    device, scripted = sps30
    scripted.responses.append(encode_words(pack(">HH", 0x0009, 0x3A80)))  # 604800s
    assert device.auto_cleaning_interval == 604800
    device.auto_cleaning_interval = 604800
    written = scripted.written[-1]
    assert written[:2] == bytes([0x80, 0x04])
    assert written[2:4] == pack(">H", 0x0009)
    assert written[5:7] == pack(">H", 0x3A80)


def test_wakeup_tolerates_the_expected_first_nak(sps30):
    device, wire = sps30
    # Sleeping sensor: first 0x1103 NAKs (interface off), second ACKs.
    wire.raise_on_write = [OSError("NAK"), None]
    device.wakeup()
    assert wire.written  # the second send went through


def test_wakeup_raises_on_a_dead_bus(sps30):
    device, wire = sps30
    wire.raise_on_write = OSError("no ack")  # every write fails: sensor absent
    with pytest.raises(OSError):
        device.wakeup()


def test_device_status_register_decodes_documented_bits(sps30):
    device, scripted = sps30
    register = (1 << 21) | (1 << 4)  # SPEED warning + FAN error; reserved bits untouched
    scripted.responses.append(encode_words(pack(">I", register)))
    status = device.read_device_status()
    assert scripted.written[-1] == bytes([0xD2, 0x06])
    assert status == {
        "raw": register, "speed_warning": True, "laser_error": False, "fan_error": True,
    }
    # reserved bits set (they "can be both 0 and 1") must not read as problems
    scripted.responses.append(encode_words(pack(">I", 0xFFFFFFFF & ~((1 << 21) | (1 << 5) | (1 << 4)))))
    status = device.read_device_status()
    assert not (status["speed_warning"] or status["laser_error"] or status["fan_error"])
