from Crypto.Cipher import DES
from Crypto.Util.Padding import unpad
import pytest

from lianli_lcd.protocol import (
    MAX_JPEG, WirelessPackets, wired_frame, wired_settings, wireless_brightness,
)


def decrypt(packet):
    return unpad(DES.new(b"slv3tuzx", DES.MODE_CBC, b"slv3tuzx").decrypt(packet[:512]), 8)


@pytest.mark.parametrize("length", [1, 500, 501, 502, MAX_JPEG])
def test_wired_reassembly_and_headers(length):
    payload = bytes(i % 251 for i in range(length))
    packets = list(wired_frame(payload))
    restored = bytearray()
    for index, packet in enumerate(packets):
        assert len(packet) == 512
        assert packet[:2] == b"\x02\x46"
        assert int.from_bytes(packet[2:6], "big") == length
        assert int.from_bytes(packet[6:9], "big") == index
        size = int.from_bytes(packet[9:11], "big")
        restored.extend(packet[11:11 + size])
        assert not any(packet[11 + size:])
    assert restored == payload


def test_settings_wire_bytes():
    assert wired_settings(75)[:22] == bytes.fromhex(
        "02400000000b000000000b050000004b1e0000000000"
    )


def test_wireless_legacy_envelope_not_winusb():
    builder = WirelessPackets()
    builder.started += 10  # force deterministic timestamp
    frame = builder.frame(b"jpeg")
    assert len(frame) == 102_400
    plain = decrypt(frame)
    assert len(plain) == 504
    assert plain[:12] == bytes.fromhex("65001a6d0100000000000004")
    assert not any(plain[12:])
    assert frame[512:516] == b"jpeg"
    assert not any(frame[516:])
    assert int.from_bytes(decrypt(builder.header(0x0E, b"\x1e"))[4:8], "little") == 2


@pytest.mark.parametrize("value,expected", [(0, 0), (25, 10), (37, 20), (50, 30),
                                           (62, 35), (75, 40), (100, 100)])
def test_wireless_brightness(value, expected):
    assert wireless_brightness(value) == expected


@pytest.mark.parametrize("data", [b"", bytes(MAX_JPEG + 1)])
def test_payload_limits(data):
    with pytest.raises(ValueError):
        list(wired_frame(data))
    with pytest.raises(ValueError):
        WirelessPackets().frame(data)
