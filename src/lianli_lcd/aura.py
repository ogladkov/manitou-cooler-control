"""ASUS Aura USB motherboard ARGB effects (0b05:1aa6).

Protocol adapted from OpenRGB's AsusAuraUSBController / AsusAuraMainboardController,
Copyright Martin Hartl and OpenRGB contributors, GPL-2.0-or-later.
See THIRD_PARTY.md. No global reset, Gen1 override or persistent commit is sent.
"""

from dataclasses import dataclass
import os
import select
import time

from coolercontrol.models.v1 import device_pb2

from . import __version__


# name, frontend label, firmware effect, required colors
MODES = (
    ("off", "Off", 0, 0),
    ("static", "Static", 1, 1),
    ("breathing", "Breathing", 2, 1),
    ("flashing", "Flashing", 3, 1),
    ("spectrum-cycle", "Spectrum Cycle", 4, 0),
    ("rainbow", "Rainbow", 5, 0),
)


def packet(command):
    data = bytearray(65)
    data[:2] = (0xEC, command)
    return data


@dataclass(frozen=True)
class Layout:
    firmware: str
    headers: int
    onboard_leds: int

    @classmethod
    def parse(cls, version, config):
        if len(version) < 18 or version[:2] != b"\xec\x02":
            raise ValueError("Invalid ASUS Aura firmware reply")
        if len(config) < 64 or config[:2] != b"\xec\x30":
            raise ValueError("Invalid ASUS Aura configuration reply")
        firmware = version[2:18].split(b"\0", 1)[0].decode("ascii")
        headers, onboard = config[4 + 2], config[4 + 0x1B]
        # Effect-color slots use a 16-bit mask. Reject layouts that cannot
        # be addressed independently instead of truncating a mask.
        if (not firmware or not firmware.isprintable() or not 1 <= headers <= 8
                or onboard + headers > 16):
            raise ValueError("Unsupported ASUS Aura ARGB layout")
        return cls(firmware, headers, onboard)

    def effects(self, index, setting):
        if not 0 <= index < self.headers:
            raise ValueError("Unknown ASUS Aura ARGB header")
        spec = next((m for m in MODES if m[0] == setting.mode), None)
        if spec is None:
            raise ValueError("Unsupported ASUS Aura lighting mode")
        if len(setting.colors) != spec[3]:
            raise ValueError(f"{setting.mode} requires {spec[3]} color(s)")
        if setting.speed or setting.backward:
            raise ValueError("ASUS Aura effect speed/direction is not configurable")
        color = (0, 0, 0)
        if setting.colors:
            c = setting.colors[0]
            color = (c.r, c.g, c.b)
            if any(not 0 <= v <= 255 for v in color):
                raise ValueError("RGB components must be between 0 and 255")
        effect = packet(0x35)
        # AR32 firmware numbers ARGB effects before the onboard zone.
        offset = 0 if self.firmware.startswith("AULA3-AR32") else bool(self.onboard_leds)
        effect[2], effect[5] = index + offset, spec[2]
        colors = packet(0x36)
        slot = self.onboard_leds + index
        colors[2:4] = (1 << slot).to_bytes(2, "big")
        colors[5 + 3 * slot:8 + 3 * slot] = bytes(color)
        return [bytes(effect), bytes(colors)]


class AuraHid:
    """Bounded HID queries; lighting writes have no acknowledgement in this protocol."""

    def __init__(self, fd):
        self.fd = fd

    def write(self, data):
        if len(data) != 65 or data[0] != 0xEC:
            raise ValueError("Invalid ASUS Aura report")
        if not select.select([], [self.fd], [], .5)[1]:
            raise TimeoutError("ASUS Aura write timed out")
        if os.write(self.fd, data) != len(data):
            raise OSError("Short ASUS Aura write")

    def query(self, command, response):
        for _ in range(16):
            if not select.select([self.fd], [], [], 0)[0]:
                break
            os.read(self.fd, 65)
        self.write(packet(command))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if not select.select([self.fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            data = os.read(self.fd, 65)
            if data[:2] == bytes((0xEC, response)):
                return data
        raise TimeoutError("No ASUS Aura response")

    def layout(self):
        return Layout.parse(self.query(0x82, 0x02), self.query(0xB0, 0x30))


@dataclass
class AuraDevice:
    endpoint: object
    layout: Layout
    error: str | None = None

    @property
    def id(self):
        return f"asus-aura-argb-{self.endpoint.topology}"

    @property
    def name(self):
        return f"ASUS Aura ARGB ({self.endpoint.topology})"

    @property
    def channels(self):
        return {f"argb-{i + 1}": f"ARGB · Header {i + 1}"
                for i in range(self.layout.headers)}

    def descriptor(self):
        device = device_pb2.Device(id=self.id, uid_info=self.id, name=self.name)
        device.info.model = "ASUS Aura USB motherboard ARGB"
        device.info.driver_info.name = "coolercontrol-lianli-lcd"
        device.info.driver_info.version = __version__
        device.info.driver_info.locations.append(f"/sys/bus/usb/devices/{self.endpoint.topology}")
        for key, label in self.channels.items():
            channel = device.info.channels[key]
            channel.label = label
            for name, title, _, count in MODES:
                channel.lighting_modes.lighting_mode.add(
                    name=name, frontend_name=title, min_colors=count, max_colors=count,
                    speed_enabled=False, backward_enabled=False)
        return device

    def packets(self, channel, setting):
        if channel not in self.channels:
            raise ValueError("Unknown ASUS Aura ARGB header")
        return self.layout.effects(int(channel.removeprefix("argb-")) - 1, setting)
