from contextlib import contextmanager
from dataclasses import replace

import pytest

from coolercontrol.device_service.v1.lighting_pb2 import LightingSetting, RGB
from lianli_lcd.aura import AuraDevice, AuraHid, Layout
from lianli_lcd.rgb import Lighting
from lianli_lcd.rgb_transport import Endpoint, RgbTransport, discover_rgb


def replies(firmware=b"AULA3-AR32-0201", headers=3, onboard=1):
    version = bytearray(65)
    version[:2] = b"\xec\x02"
    version[2:2 + len(firmware)] = firmware
    config = bytearray(65)
    config[:2] = b"\xec\x30"
    config[6], config[31] = headers, onboard
    return bytes(version), bytes(config)


@pytest.mark.parametrize("firmware,expected_channel", [
    (b"AULA3-AR32-0201", 1), (b"AULA3-6K75-0109", 2),
])
def test_one_header_effect_and_color_mask(firmware, expected_channel):
    layout = Layout.parse(*replies(firmware=firmware, onboard=4))
    effect, color = layout.effects(1, LightingSetting(mode="static", colors=[RGB(r=17,g=38,b=59)]))
    assert effect == bytes((0xEC, 0x35, expected_channel, 0, 0, 1)) + bytes(59)
    # Onboard slots 0..3, header 1 slot 4, selected header 2 slot 5.
    assert color == bytes((0xEC, 0x36, 0, 0x20, 0)) + bytes(15) + b"\x11\x26\x3b" + bytes(42)
    assert all(len(p) == 65 for p in (effect, color))


def test_high_byte_mask_and_no_onboard_zone():
    layout = Layout.parse(*replies(onboard=13))
    _, color = layout.effects(2, LightingSetting(mode="off"))
    assert color[2:4] == b"\x80\0"
    layout = Layout.parse(*replies(firmware=b"AULA3-6K75-0109", onboard=0))
    effect, _ = layout.effects(0, LightingSetting(mode="off"))
    assert effect[2] == 0


@pytest.mark.parametrize("headers,onboard", [(0, 1), (9, 0), (3, 14)])
def test_unsupported_layout_rejected(headers, onboard):
    with pytest.raises(ValueError, match="layout"):
        Layout.parse(*replies(headers=headers, onboard=onboard))


@pytest.mark.parametrize("which", ["version-short", "config-short", "wrong-id", "empty-version"])
def test_malformed_query_response(which):
    version, config = replies()
    if which == "version-short":
        version = version[:10]
    elif which == "config-short":
        config = config[:32]
    elif which == "wrong-id":
        config = b"\xec\xb0" + config[2:]
    else:
        version = b"\xec\x02" + bytes(63)
    with pytest.raises(ValueError):
        Layout.parse(version, config)


class Transport:
    def __init__(self):
        self.current = Layout.parse(*replies())
        self.writes = []
        self.opens = 0

    @contextmanager
    def open(self, endpoint):
        assert endpoint.kind == "aura"
        self.opens += 1
        yield self

    def layout(self):
        return self.current

    def write(self, data):
        self.writes.append(data)


def discovered():
    transport = Transport()
    lighting = Lighting(transport)
    lighting.discover([Endpoint("aura", "3-11", 3, 10, "/dev/hidraw9")])
    return lighting, transport, next(iter(lighting.devices.values()))


def test_discovery_and_status_do_not_set_effects_and_layout_rechecked():
    lighting, transport, device = discovered()
    assert isinstance(device, AuraDevice)
    assert list(device.channels) == ["argb-1", "argb-2", "argb-3"]
    assert lighting.status(device) == []
    assert transport.writes == []
    lighting.apply(device, "argb-3", LightingSetting(mode="rainbow"))
    assert [p[1] for p in transport.writes] == [0x35, 0x36]
    transport.writes.clear()
    transport.current = replace(transport.current, headers=2)
    with pytest.raises(OSError, match="layout changed"):
        lighting.apply(device, "argb-3", LightingSetting(mode="off"))
    assert not transport.writes and device.error
    transport.current = device.layout
    lighting.apply(device, "argb-3", LightingSetting(mode="off"))
    assert device.error is None


@pytest.mark.parametrize("channel,setting", [
    ("argb-0", LightingSetting(mode="off")),
    ("argb-4", LightingSetting(mode="off")),
    ("argb-1", LightingSetting(mode="unknown")),
    ("argb-1", LightingSetting(mode="static")),
    ("argb-1", LightingSetting(mode="off", colors=[RGB(r=1)])),
    ("argb-1", LightingSetting(mode="static", colors=[RGB(r=256)])),
    ("argb-1", LightingSetting(mode="rainbow", speed="Fast")),
    ("argb-1", LightingSetting(mode="rainbow", backward=True)),
])
def test_invalid_settings_rejected_before_usb(channel, setting):
    lighting, transport, device = discovered()
    with pytest.raises(ValueError):
        lighting.apply(device, channel, setting)
    assert transport.opens == 1 and not transport.writes


def test_failed_probe_keeps_other_devices_available():
    transport = Transport()
    def fail():
        raise TimeoutError("no reply")
    transport.layout = fail
    lighting = Lighting(transport)
    lighting.discover([Endpoint("aura", "3-11", 3, 10)])
    assert not lighting.devices and "no reply" in lighting.discovery_errors[0]


def test_sysfs_discovery_and_disconnect(tmp_path, monkeypatch):
    root, hidroot = tmp_path / "usb", tmp_path / "hid"
    usb = root / "3-11"
    usb.mkdir(parents=True)
    for filename, value in {"idVendor": "0b05", "idProduct": "1aa6",
                            "busnum": "3", "devnum": "10"}.items():
        (usb / filename).write_text(value)
    node = hidroot / "hidraw9"
    node.mkdir(parents=True)
    (node / "device").symlink_to(usb / "3-11:1.2" / "hid-device")
    endpoints = discover_rgb(root, hidroot)
    assert endpoints == [Endpoint("aura", "3-11", 3, 10, "/dev/hidraw9")]
    monkeypatch.setattr("lianli_lcd.rgb_transport.discover_rgb", lambda: [])
    with pytest.raises(OSError, match="disconnected"):
        with RgbTransport(tmp_path / "locks").open(endpoints[0]):
            pytest.fail("must not open stale hidraw")


def test_hid_query_matches_reply_and_short_write_fails(monkeypatch):
    pending, writes = [], []
    def write(fd, data):
        writes.append(data)
        pending.extend([b"\xec\x02" + bytes(63), replies()[1]])
        return len(data)
    def select(read, write, error, timeout):
        return (read if pending else [], write, [])
    monkeypatch.setattr("lianli_lcd.aura.select.select", select)
    monkeypatch.setattr("lianli_lcd.aura.os.write", write)
    monkeypatch.setattr("lianli_lcd.aura.os.read", lambda *_: pending.pop(0))
    hid = AuraHid(123)
    assert hid.query(0xB0, 0x30) == replies()[1]
    assert writes == [b"\xec\xb0" + bytes(63)]
    monkeypatch.setattr("lianli_lcd.aura.os.write", lambda *_: 10)
    with pytest.raises(OSError, match="Short"):
        hid.query(0xB0, 0x30)
    monkeypatch.setattr("lianli_lcd.aura.os.write", lambda _, data: len(data))
    with pytest.raises(TimeoutError, match="No ASUS Aura response"):
        hid.query(0xB0, 0x30)
