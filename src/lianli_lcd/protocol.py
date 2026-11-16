"""LCD wire formats, adapted from sgtaziz/lian-li-linux (MIT; see vendor/).

No fan, RGB, boot image, serial write, firmware or reboot commands are used.
"""

import time

from Crypto.Cipher import DES
from Crypto.Util.Padding import pad

MAX_JPEG = 101_888
WIRED_ID = (0x04FC, 0x7393)
WIRELESS_ID = (0x1CBE, 0x0006)


def wired_packet(command: int, total: int, index: int, payload: bytes) -> bytes:
    if len(payload) > 501:
        raise ValueError("TL HID payload exceeds 501 bytes")
    header = (bytes([2, command]) + total.to_bytes(4, "big")
              + index.to_bytes(3, "big") + len(payload).to_bytes(2, "big"))
    return (header + payload).ljust(512, b"\0")


def wired_frame(jpeg: bytes):
    if not 0 < len(jpeg) <= MAX_JPEG:
        raise ValueError("Invalid JPEG payload size")
    for index, offset in enumerate(range(0, len(jpeg), 501)):
        yield wired_packet(0x46, len(jpeg), index, jpeg[offset:offset + 501])


def wired_settings(brightness: int) -> bytes:
    # Rotation is applied in software for both families, so hardware stays at 0.
    return wired_packet(0x40, 11, 0, bytes([5, 0, 0, 0, brightness, 30, 0, 0, 0, 0, 0]))


def wireless_brightness(value: int) -> int:
    if not 0 <= value <= 100:
        raise ValueError("Brightness must be 0..100")
    anchors = [(0, 0), (25, 10), (50, 30), (75, 40), (100, 100)]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if value <= x1:
            return y0 + ((value - x0) * (y1 - y0) + (x1 - x0) // 2) // (x1 - x0)
    return 100


class WirelessPackets:
    def __init__(self):
        self.started = time.monotonic()
        self.timestamp = 0

    def header(self, command: int, params: bytes = b"") -> bytes:
        if len(params) > 496:
            raise ValueError("Wireless parameters exceed 496 bytes")
        self.timestamp = max(self.timestamp + 1, int((time.monotonic() - self.started) * 1000))
        plain = bytearray(504)
        plain[0] = command
        plain[2:4] = b"\x1a\x6d"
        plain[4:8] = (self.timestamp & 0xFFFFFFFF).to_bytes(4, "little")
        plain[8:8 + len(params)] = params
        return DES.new(b"slv3tuzx", DES.MODE_CBC, b"slv3tuzx").encrypt(pad(plain, 8))

    def frame(self, jpeg: bytes) -> bytes:
        if not 0 < len(jpeg) <= MAX_JPEG:
            raise ValueError("Invalid JPEG payload size")
        return (self.header(0x65, len(jpeg).to_bytes(4, "big")) + jpeg).ljust(102_400, b"\0")
