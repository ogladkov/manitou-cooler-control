from contextlib import contextmanager
from dataclasses import replace

import pytest
import usb.core

from lianli_lcd.rgb import Lighting, RgbDevice
from lianli_lcd.rgb_protocol import wireless_groups, wireless_rpm_records
from lianli_lcd.rgb_transport import Bulk, Endpoint


def record(mac=1, channel=8, rx=1):
    data = bytearray(42)
    data[:6], data[6:12] = bytes([mac]) * 6, bytes([9]) * 6
    data[12:14] = (channel, rx)
    data[19] = 3
    data[24:27] = (32, 32, 32)
    # First RPM includes MB-sync/PWM flags in the upper nibble.
    data[28:34] = bytes.fromhex("64b005140578")
    data[41] = 0x1C
    return data


def reply(*records):
    return bytes((0x10, len(records), 0, 0)) + b"".join(records)


@pytest.mark.parametrize("channel,rx", [(0, 0), (40, 128), (8, 13), (39, 1)])
def test_rpm_reading_survives_rf_route_changes(channel, rx):
    records = wireless_rpm_records(reply(record(channel=channel, rx=rx)))
    reading = records[bytes([1]) * 6]
    assert reading.rpms == (1200, 1300, 1400)
    assert reading.master == bytes([9]) * 6
    assert reading.fan_types == (32, 32, 32)
    if channel in (0, 40):
        assert wireless_groups(reply(record(channel=channel, rx=rx))) == []


def test_rf_collision_is_not_a_mac_collision_for_reading():
    data = reply(record(mac=1), record(mac=2))
    assert wireless_groups(data) == []  # RF writes remain prohibited.
    assert len(wireless_rpm_records(data)) == 2


@pytest.mark.parametrize("field,value", [(41, 0), (18, 255), (19, 0), (19, 5), (25, 23)])
def test_invalid_sensor_records_are_still_rejected(field, value):
    bad = record()
    bad[field] = value
    assert wireless_rpm_records(reply(bad)) == {}


def test_duplicate_mac_is_rejected_even_if_second_record_invalid():
    good, bad = record(), record(rx=2)
    for marker in (0x1C, 0):
        bad[41] = marker
        assert wireless_rpm_records(reply(good, bad)) == {}
    with pytest.raises(OSError, match="Truncated"):
        wireless_rpm_records(reply(good)[:-1])


class Receiver:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.queries = 0

    @contextmanager
    def open(self, endpoint):
        assert endpoint.kind == "rx"  # No TX access or RF commands.
        yield self

    def rpm_records(self):
        self.queries += 1
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return wireless_rpm_records(value)


def lighting_for(replies):
    expected = wireless_groups(reply(record()))[0]
    transport = Receiver(replies)
    lighting = Lighting(transport)
    lighting.rx = Endpoint("rx", "test-rx", 0, 0)
    device = RgbDevice(expected.id, "Test group", lighting.rx, group=expected)
    return lighting, device, transport


def test_changed_rf_route_keeps_rpm_without_updating_write_address():
    lighting, device, transport = lighting_for([reply(record(channel=20, rx=7))])
    original = device.group
    assert [s.speed.rpm for s in lighting.status(device)] == [1200, 1300, 1400]
    assert device.group == original and transport.queries == 1


@pytest.mark.parametrize("first", [reply(), reply(record())[:-1], usb.core.USBTimeoutError("timeout")])
def test_transient_bad_read_is_retried_with_fresh_sample(first, monkeypatch):
    sleeps = []
    monkeypatch.setattr("lianli_lcd.rgb.time.sleep", sleeps.append)
    lighting, device, transport = lighting_for([first, reply(record())])
    assert [s.speed.rpm for s in lighting.status(device)] == [1200, 1300, 1400]
    assert transport.queries == 2 and sleeps == [.025]


@pytest.mark.parametrize("problem,reason", [("owner", "owner changed"),
                                          ("count", "layout changed"),
                                          ("types", "layout changed"),
                                          ("missing", "MAC missing")])
def test_persistent_failure_never_reuses_old_rpm(problem, reason, monkeypatch):
    monkeypatch.setattr("lianli_lcd.rgb.time.sleep", lambda _: None)
    bad = record()
    if problem == "owner":
        bad[6:12] = bytes([10]) * 6
    elif problem == "count":
        bad[19] = 2
    elif problem == "types":
        bad[24] = 33  # Still TL, but a different physical fan layout.
    else:
        bad[:6] = bytes([2]) * 6
    lighting, device, transport = lighting_for([reply(record()), *[reply(bad)] * 3,
                                               reply(record())])
    assert len(lighting.status(device)) == 3
    with pytest.raises(OSError, match=reason):
        lighting.status(device)
    assert transport.queries == 4
    assert len(lighting.status(device)) == 3  # Fresh recovery, no restart needed.


def test_transport_uses_only_getdev_queries_for_rpm():
    class Usb:
        def __init__(self):
            self.pending = []
            self.commands = []
        def write(self, endpoint, packet, timeout):
            assert endpoint == 0x01
            self.commands.append(packet)
            data = reply(record(rx=128), record(mac=2))
            self.pending = [data[:64], data[64:]]
            return len(packet)
        def read(self, endpoint, size, timeout):
            assert endpoint == 0x81
            if self.pending:
                return self.pending.pop(0)
            raise usb.core.USBTimeoutError("timeout")
    device = Usb()
    readings = Bulk(device).rpm_records()
    assert len(readings) == 2
    assert device.commands == [b"\x10\x01" + bytes(62)]


def test_disconnected_receiver_does_not_retry_open(monkeypatch):
    lighting, device, transport = lighting_for([])
    def fail(_):
        raise PermissionError("no access")
    monkeypatch.setattr(transport, "open", fail)
    with pytest.raises(PermissionError):
        lighting.status(device)
    assert transport.queries == 0


def test_device_without_captured_types_still_checks_fan_count():
    lighting, device, _ = lighting_for([reply(record())])
    device.group = replace(device.group, fan_types=())
    assert len(lighting.status(device)) == 3
