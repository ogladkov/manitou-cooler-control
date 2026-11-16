"""TL lighting commands only. No PWM, binding, firmware or clock commands.

Wire formats: sgtaziz/lian-li-linux (MIT), see THIRD_PARTY.md.
"""

import colorsys
from dataclasses import dataclass
import math


SPEEDS = ("Slowest", "Slow", "Normal", "Fast", "Fastest")
# name, label, minimum colors, maximum colors, speed, direction, TL mode byte
MODES = (
    ("off", "Off", 0, 0, False, False, 3),
    ("static", "Static", 1, 1, False, False, 3),
    ("rainbow", "Rainbow", 0, 0, True, True, 1),
    ("color_cycle", "Color cycle", 0, 0, True, False, 7),
    ("meteor", "Meteor", 1, 1, True, True, 6),
    ("wave", "Wave", 1, 1, True, True, 21),
)
WIRELESS_MODES = MODES + (("breathing", "Breathing", 1, 1, True, False, 4),)


def validate(setting, wireless=False):
    modes = WIRELESS_MODES if wireless else MODES
    mode = next((m for m in modes if m[0] == setting.mode), None)
    if mode is None:
        raise ValueError(f"Unsupported lighting mode: {setting.mode}")
    if not mode[2] <= len(setting.colors) <= mode[3]:
        raise ValueError(f"{mode[1]} requires {mode[2]} color(s)")
    colors = [bytes((c.r, c.g, c.b)) for c in setting.colors]
    speed = setting.speed if setting.HasField("speed") else "Normal"
    if speed not in SPEEDS:
        raise ValueError(f"Unknown lighting speed: {speed}")
    return mode, colors, SPEEDS.index(speed)


def tl_packet(command, payload=b""):
    if command not in (0xA1, 0xA3) or len(payload) > 58:
        raise ValueError("Not a supported TL lighting/query command")
    return bytes((1, command, 0, 0, 0, len(payload))) + payload + bytes(58 - len(payload))


def tl_fans(reply):
    if len(reply) < 6 or reply[:2] != b"\x01\xa1":
        raise OSError("Invalid TL handshake")
    size = reply[5]
    if size > 58 or size % 3 or len(reply) < 6 + size:
        raise OSError("Truncated TL handshake")
    ports = {}
    for offset in range(6, 6 + size, 3):
        info = reply[offset]
        if info & 0x80 and not info & 0x40:
            ports.setdefault((info >> 4) & 3, set()).add(info & 15)
    return {p: tuple(sorted(fans)) for p, fans in ports.items()}


def tl_lighting(port, fan, setting):
    mode, colors, speed = validate(setting)
    if not 0 <= port <= 3 or not 0 <= fan <= 15:
        raise ValueError("Invalid TL fan address")
    payload = bytearray(20)
    payload[:5] = bytes((port << 4, (port << 4) | fan, mode[6], 4, speed))
    for i, color in enumerate(colors):
        payload[5 + 3 * i:8 + 3 * i] = color
    payload[17:20] = bytes((int(setting.backward), int(setting.mode == "off"), len(colors)))
    return tl_packet(0xA3, payload)


def tl_rpms(reply):
    ports = tl_fans(reply)
    return {f"port-{(reply[i] >> 4 & 3) + 1}-fan-{(reply[i] & 15) + 1}":
            int.from_bytes(reply[i + 1:i + 3], "big")
            for i in range(6, 6 + reply[5], 3)
            if (reply[i] & 15) in ports.get(reply[i] >> 4 & 3, ())
            and reply[i] & 0x80 and not reply[i] & 0x40}


@dataclass(frozen=True)
class WirelessGroup:
    mac: bytes
    master: bytes
    channel: int
    rx: int
    fans: int
    effect: bytes
    mb_sync: bool
    sequence: int
    index: int
    rpms: tuple[int, ...] = ()

    @property
    def id(self):
        return f"tl-rgb-wireless-{self.mac.hex()}"


def wireless_groups(reply, pages=1):
    if len(reply) < 4 or reply[0] != 0x10:
        raise OSError("Invalid Wireless GetDev response")
    count = min(reply[1], pages * 10)
    if len(reply) < 4 + count * 42:
        raise OSError("Truncated Wireless GetDev response")
    groups = []
    records = [reply[4 + i * 42:46 + i * 42] for i in range(count)]
    for i, r in enumerate(records):
        # Only homogeneous TL LCD Wireless groups. Do not guess another fan's layout.
        if (r[41] != 0x1C or r[18] != 0 or not 1 <= r[19] <= 4
                or not any(r[:6]) or not any(r[6:12])
                or not 1 <= r[12] <= 39 or not 1 <= r[13] <= 13
                or any(t not in (27, 32, 33, 34, 35) for t in r[24:24 + r[19]])):
            continue
        # Include other fan models in collision detection and MB-sync slot numbering.
        if sum(other[41] == 0x1C and other[18] != 255 and other[12:14] == r[12:14]
               for other in records) != 1:
            continue
        slot = sum(other[41] == 0x1C and other[18] != 255 and other[6:12] == r[6:12]
                   for other in records[:i])
        groups.append(WirelessGroup(bytes(r[:6]), bytes(r[6:12]), r[12], r[13], r[19],
                                    bytes(r[20:24]), bool(r[28] & 0x40), r[40], slot,
                                    tuple(((r[28 + n * 2] & 15) << 8) | r[29 + n * 2]
                                          for n in range(r[19]))))
    return groups


def tinyuz_literals(raw):
    """Valid tinyuz stream using literal tokens, bounded to the RF upload memory.

    32 frames × 104 LEDs fit without LZ compression. This avoids a native runtime
    dependency; compatibility is tested with upstream's C decompressor.
    """
    if not raw or len(raw) > 104 * 3 * 32:
        raise ValueError("RGB frame data exceeds supported size")
    out = bytearray((4096).to_bytes(4, "little"))
    bit_count = 0
    bit_index = 0

    def bit(value):
        nonlocal bit_count, bit_index
        if bit_count == 0:
            bit_index = len(out)
            out.append(0)
        out[bit_index] |= value << bit_count
        bit_count = (bit_count + 1) % 8

    for value in raw:
        bit(1)  # literal token
        out.append(value)
    # Dictionary token; packed length 3 = streamEnd; explicit zero distance.
    for value in (0, 0, 1, 1, 0, 0):
        bit(value)
    out.append(0)
    return bytes(out)


def wireless_frames(setting, fans):
    _, colors, speed = validate(setting, wireless=True)
    if not 1 <= fans <= 4:
        raise ValueError("Invalid TL Wireless group size")
    frames = 1 if setting.mode in ("off", "static") else 32
    base = colors[0] if colors else bytes(3)
    raw = bytearray()
    for frame in range(frames):
        phase = frame / frames
        if setting.backward:
            phase = -phase
        for led in range(26 * fans):
            position = led / (26 * fans)
            if setting.mode in ("rainbow", "color_cycle"):
                hue = (phase + (position if setting.mode == "rainbow" else 0)) % 1
                rgb = tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 1, 1))
            else:
                if setting.mode == "breathing":
                    level = (1 - math.cos(phase * math.tau)) / 2
                elif setting.mode == "meteor":
                    level = max(0, 1 - ((position - phase) % 1) * 5)
                elif setting.mode == "wave":
                    level = (1 + math.cos((position - phase) * math.tau)) / 2
                else:
                    level = 0 if setting.mode == "off" else 1
                rgb = tuple(round(c * level) for c in base)
            raw.extend(rgb)
    return bytes(raw), frames, (250, 150, 100, 60, 30)[speed]


def wireless_upload(group, setting):
    raw, frames, interval = wireless_frames(setting, group.fans)
    data = tinyuz_literals(raw)
    if len(data) > 12_288:
        raise ValueError("RGB upload exceeds firmware memory")
    ticks, fraction = divmod(interval * 160, 100)
    # Same FNV identity convention as the reference driver. High bit prevents a
    # stale effect byte being mistaken for a valid RF receiver slot by firmware.
    metadata = (ticks.to_bytes(2, "big") + bytes((fraction,))
                + frames.to_bytes(2, "big") + bytes((26 * group.fans,)) + bytes(5))
    effect = 0x811C9DC5
    for value in data + metadata:
        effect = ((effect ^ value) * 0x01000193) & 0xFFFFFFFF
    effect = (effect | 0x80000000).to_bytes(4, "big")
    packets = []
    chunks = (len(data) + 219) // 220
    for i in range(chunks + 1):
        p = bytearray(240)
        p[:20] = b"\x12\x20" + group.mac + group.master + effect + bytes((i, chunks + 1))
        if i == 0:
            p[20:24] = len(data).to_bytes(4, "big")
            p[25:28] = frames.to_bytes(2, "big") + bytes((26 * group.fans,))
            p[32:35] = ticks.to_bytes(2, "big") + bytes((fraction,))
        else:
            chunk = data[(i - 1) * 220:i * 220]
            p[20:20 + len(chunk)] = chunk
        packets.append(bytes(p))
    return effect, packets


def rf_usb_packets(group, packet):
    if len(packet) != 240 or packet[:2] not in (b"\x12\x20", b"\x12\x27"):
        raise ValueError("Not an RGB RF packet")
    return [bytes((0x10, i, group.channel, group.rx)) + packet[i * 60:(i + 1) * 60]
            for i in range(4)]


def disable_mb_rgb(group):
    p = bytearray(240)
    p[:18] = (b"\x12\x27" + group.mac + group.master
              + bytes((group.rx, group.channel, group.index, (group.sequence + 1) & 255)))
    return bytes(p)
