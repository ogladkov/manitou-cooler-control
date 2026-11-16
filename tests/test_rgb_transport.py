from dataclasses import replace

import pytest
import usb.core

from lianli_lcd.rgb_protocol import WirelessGroup, wireless_upload
from lianli_lcd.rgb_transport import Bulk, Endpoint, RgbTransport
from coolercontrol.device_service.v1.lighting_pb2 import LightingSetting


class Usb:
    def __init__(self):
        self.writes = []
        self.pending = []
        self.reply = b""
        self.short = False

    def write(self, endpoint, packet, timeout):
        assert endpoint == 1 and timeout == 500
        self.writes.append(packet)
        self.pending = [self.reply[i:i + 64] for i in range(0, len(self.reply), 64)]
        return len(packet) - int(self.short)

    def read(self, endpoint, size, timeout):
        assert endpoint == 0x81 and timeout <= 500
        if not self.pending:
            raise usb.core.USBTimeoutError("timeout")
        return self.pending.pop(0)


def test_receiver_collects_fragmented_interrupt_transfers():
    usb = Usb()
    usb.reply = bytes(i % 256 for i in range(130))
    assert Bulk(usb).query(bytes(64), 512) == usb.reply
    assert len(usb.writes) == 1
    usb.short = True
    with pytest.raises(OSError, match="Short"):
        Bulk(usb).query(bytes(64), 512)


def test_air_gap_survives_reopen_and_failed_write(monkeypatch):
    sleeps = []
    monkeypatch.setattr("lianli_lcd.rgb_transport.time.sleep", sleeps.append)
    monkeypatch.setattr("lianli_lcd.rgb_transport.time.monotonic", lambda: 10)
    group = WirelessGroup(bytes([1]) * 6, bytes([9]) * 6, 8, 1, 3,
                          bytes(4), False, 0, 0)
    _, packets = wireless_upload(group, LightingSetting(mode="off"))
    pacer = [None]
    first = Usb()
    first.short = True
    with pytest.raises(OSError):
        Bulk(first, pacer).send(group, packets[0])
    assert len(first.writes) == 1  # never replay a partially sent frame
    second = Usb()
    Bulk(second, pacer).send(replace(group, mac=bytes([2]) * 6), packets[0])
    assert sleeps[0] == .010 and len(second.writes) == 4


def test_usb_cleanup_on_claim_failure_and_disconnect(monkeypatch, tmp_path):
    endpoint = Endpoint("tx", "fake-tx", 3, 20)
    events = []
    class Device:
        def get_active_configuration(self):
            return type("Config", (), {"bConfigurationValue": 1})()
        def is_kernel_driver_active(self, interface):
            return True
        def detach_kernel_driver(self, interface):
            events.append("detach")
        def attach_kernel_driver(self, interface):
            events.append("attach")
    device = Device()
    monkeypatch.setattr("lianli_lcd.rgb_transport.discover_rgb", lambda: [endpoint])
    monkeypatch.setattr("usb.core.find", lambda **_: device)
    monkeypatch.setattr("usb.util.dispose_resources", lambda _: events.append("dispose"))
    monkeypatch.setattr("usb.util.release_interface", lambda *_: events.append("release"))
    def fail(*_):
        raise OSError("claimed elsewhere")
    monkeypatch.setattr("usb.util.claim_interface", fail)
    transport = RgbTransport(tmp_path)
    with pytest.raises(OSError, match="claimed elsewhere"):
        with transport.open(endpoint):
            pytest.fail("must not open")
    assert events == ["detach", "attach", "dispose"]
    events.clear()
    monkeypatch.setattr("usb.util.claim_interface", lambda *_: events.append("claim"))
    with pytest.raises(OSError, match="unplugged"):
        with transport.open(endpoint):
            raise OSError("unplugged")
    assert events == ["detach", "claim", "release", "attach", "dispose"]
