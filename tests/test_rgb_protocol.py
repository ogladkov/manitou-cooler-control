import ctypes
from pathlib import Path
import subprocess

import pytest

from coolercontrol.device_service.v1.lighting_pb2 import LightingSetting, RGB
from lianli_lcd.rgb_protocol import (
    WIRELESS_MODES, WirelessGroup, disable_mb_rgb, rf_usb_packets, tinyuz_literals,
    tl_fans, tl_lighting, validate, wireless_frames, wireless_groups, wireless_upload,
)


@pytest.fixture(scope="module")
def decode(tmp_path_factory):
    # An independent implementation, not a Python decoder mirroring our encoder.
    library = tmp_path_factory.mktemp("tinyuz") / "reference.so"
    source = Path(__file__).parent / "vendor/tinyuz/tuz_dec.c"
    subprocess.run(["cc", "-shared", "-fPIC", "-O2", str(source), "-o", str(library)],
                   check=True, capture_output=True)
    lib = ctypes.CDLL(str(library))
    lib.tuz_decompress_mem.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                                      ctypes.POINTER(ctypes.c_uint)]
    lib.tuz_decompress_mem.restype = ctypes.c_uint

    def decompress(data, length):
        output = ctypes.create_string_buffer(length)
        size = ctypes.c_uint(length)
        assert lib.tuz_decompress_mem(data, len(data), output, ctypes.byref(size)) == 1
        assert size.value == length
        return output.raw
    return decompress


@pytest.mark.parametrize("size", [1, 2, 7, 8, 9, 15, 78, 312, 9984])
def test_tinyuz_against_upstream_decoder(decode, size):
    raw = bytes(i % 256 for i in range(size))
    assert decode(tinyuz_literals(raw), size) == raw


@pytest.mark.parametrize("mode", [m[0] for m in WIRELESS_MODES])
@pytest.mark.parametrize("fans", [1, 3, 4])
def test_rf_upload_roundtrip_and_firmware_limits(decode, mode, fans):
    spec = next(m for m in WIRELESS_MODES if m[0] == mode)
    setting = LightingSetting(mode=mode, colors=[RGB(r=17, g=38, b=59)] * spec[2],
                              speed="Fast", backward=True)
    group = WirelessGroup(bytes([1]) * 6, bytes([9]) * 6, 8, 2, fans,
                          bytes(4), False, 0, 0)
    effect, packets = wireless_upload(group, setting)
    header = packets[0]
    size = int.from_bytes(header[20:24], "big")
    assert size <= 12288 and header[19] == len(packets)
    assert header[:14] == b"\x12\x20" + group.mac + group.master
    assert effect[0] >= 128  # never a valid RX slot
    assert header[27] == fans * 26
    raw, frames, interval = wireless_frames(setting, fans)
    assert int.from_bytes(header[25:27], "big") == frames
    assert int.from_bytes(header[32:34], "big") * 100 + header[34] == interval * 160
    encoded = b"".join(p[20:] for p in packets[1:])[:size]
    assert decode(encoded, len(raw)) == raw
    for i, packet in enumerate(packets):
        chunks = rf_usb_packets(group, packet)
        assert b"".join(c[4:] for c in chunks) == packet
        assert [c[:4] for c in chunks] == [bytes((0x10, j, 8, 2)) for j in range(4)]
        assert packet[18] == i
    if mode in ("static", "off"):
        assert raw == (bytes((17, 38, 59)) if mode == "static" else bytes(3)) * fans * 26
    else:
        frame_size = fans * 26 * 3
        assert len({raw[i:i + frame_size] for i in range(0, len(raw), frame_size)}) > 2


def test_wired_packet_golden_and_discovery():
    setting = LightingSetting(mode="static", colors=[RGB(r=255, g=128, b=0)])
    packet = tl_lighting(2, 1, setting)
    assert packet[:14] == bytes.fromhex("01a3000000142021030402ff8000")
    assert packet[23:26] == bytes((0, 0, 1))
    assert len(packet) == 64 and packet[26:] == bytes(38)
    off = tl_lighting(0, 0, LightingSetting(mode="off"))
    assert off[24] == 1
    # Non-contiguous fan indices and an upgrading fan must not be inferred as present.
    assert tl_fans(bytes.fromhex("01a10000000c8003e8a003e8a203e8c003e8")) == {
        0: (0,), 2: (0, 2)}
    with pytest.raises(OSError):
        tl_fans(bytes.fromhex("01a10000000680"))


def record(mac=1, master=9, rx=1, kind=32, count=3):
    r = bytearray(42)
    r[:6], r[6:12] = bytes([mac]) * 6, bytes([master]) * 6
    r[12:14] = bytes((8, rx))
    r[19] = count
    r[24:28] = bytes([kind]) * 4
    r[41] = 0x1C
    return r


def response(*records):
    return bytes((0x10, len(records), 0, 0)) + b"".join(records)


def test_wireless_discovery_filters_bad_records_and_other_models():
    good = record()
    group = wireless_groups(response(good))[0]
    assert group.fans == 3 and group.id == "tl-rgb-wireless-010101010101"
    for offset, value in [(41, 0), (12, 0), (12, 40), (13, 0), (13, 128),
                          (19, 0), (19, 5), (18, 255), (24, 23), (25, 28)]:
        bad = good.copy()
        bad[offset] = value
        assert wireless_groups(response(bad)) == []
    assert wireless_groups(response(good, record(mac=2, kind=23))) == []  # RX collision
    group = wireless_groups(response(record(mac=2, rx=2, kind=23), good))[0]
    assert group.index == 1  # count other owned fan models for MB sync too
    with pytest.raises(OSError):
        wireless_groups(response(good)[:-1])
    packet = disable_mb_rgb(group)
    assert packet[:2] == b"\x12\x27" and packet[14:18] == bytes((1, 8, 1, 1))
    assert not any(packet[18:])


@pytest.mark.parametrize("setting", [
    LightingSetting(mode="unknown"), LightingSetting(mode="static"),
    LightingSetting(mode="off", colors=[RGB(r=255)]),
    LightingSetting(mode="rainbow", speed="bogus"),
    LightingSetting(mode="static", colors=[RGB(r=256)]),
])
def test_rejects_invalid_settings(setting):
    with pytest.raises(ValueError):
        validate(setting, wireless=True)
