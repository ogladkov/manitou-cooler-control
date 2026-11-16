from types import SimpleNamespace

import pytest
import usb.core

from lianli_lcd.discovery import Screen
from lianli_lcd import transport as module
from lianli_lcd.transport import LcdTransport


class Usb:
    def __init__(self):
        self.writes = []
        self.short = False

    def is_kernel_driver_active(self, interface):
        assert interface == 0
        return False

    def get_active_configuration(self):
        return SimpleNamespace(bConfigurationValue=1)

    def write(self, endpoint, data, timeout):
        assert endpoint == 1 and timeout <= 500
        self.writes.append(data)
        return len(data) - int(self.short)

    def read(self, endpoint, size, timeout):
        assert endpoint == 0x81 and size == 512 and timeout <= 100
        raise usb.core.USBTimeoutError("timeout")


@pytest.fixture
def fake_usb(monkeypatch, tmp_path):
    screen = Screen("tl-wireless-3-1", "wireless", "3-1", 3, 12, "test")
    device = Usb()
    monkeypatch.setattr(module, "discover", lambda: [screen])
    def find(**kwargs):
        assert kwargs == dict(idVendor=0x1CBE, idProduct=6, bus=3, address=12)
        return device
    monkeypatch.setattr(module.usb.core, "find", find)
    monkeypatch.setattr(module.usb.util, "claim_interface", lambda *_: None)
    monkeypatch.setattr(module.usb.util, "release_interface", lambda *_: None)
    monkeypatch.setattr(module.usb.util, "dispose_resources", lambda *_: None)
    transport = LcdTransport(screen, tmp_path)
    yield transport, device
    transport.close()


def test_wireless_init_and_frame_accept_optional_read_timeout(fake_usb):
    transport, device = fake_usb
    transport.settings(50)
    transport.frame(b"jpeg")
    assert [len(p) for p in device.writes] == [512, 512, 512, 102400]


def test_short_usb_write_fails_and_open_cleans_up(fake_usb):
    transport, device = fake_usb
    device.short = True
    with pytest.raises(OSError, match="Short"):
        transport.open()
    assert transport.usb is None and transport.lock_file is None


def test_lcd_lock_prevents_two_instances(fake_usb, tmp_path):
    transport, _ = fake_usb
    transport.open()
    second = LcdTransport(transport.screen, tmp_path)
    with pytest.raises(BlockingIOError):
        second.open()
    assert second.lock_file is None


def test_disconnect_does_not_fall_back_to_different_device(fake_usb, monkeypatch):
    transport, _ = fake_usb
    monkeypatch.setattr(module, "discover", lambda: [])
    with pytest.raises(OSError, match="disconnected"):
        transport.open()
