from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import threading

import grpc
import pytest

from coolercontrol.device_service.v1 import (
    device_service_pb2_grpc as rpc, fixed_duty_pb2, health_pb2, initialize_device_pb2,
    lighting_pb2 as light, list_devices_pb2, reset_channel_pb2, status_pb2,
)
from lianli_lcd.controller import Controller
from lianli_lcd.aura import AuraDevice, Layout
from lianli_lcd.discovery import simulated_screens
from lianli_lcd.rgb import Lighting
from lianli_lcd.rgb_protocol import WirelessGroup, WirelessRpm
from lianli_lcd.rgb_transport import Endpoint
from lianli_lcd.service import DeviceService
from lianli_lcd.transport import SimulatedTransport
from lianli_lcd.telemetry import Telemetry, DEVICE_ID


@pytest.fixture
def running(tmp_path):
    lcds = {s.id: Controller(SimulatedTransport(s), [tmp_path]) for s in simulated_screens()}
    lighting = Lighting(simulate=True)
    lighting.discover()
    server = grpc.server(ThreadPoolExecutor(max_workers=8))
    telemetry = Telemetry(simulate=True)
    telemetry.sample()
    rpc.add_DeviceServiceServicer_to_server(
        DeviceService(lcds, threading.Event(), lighting, telemetry), server)
    address = f"unix:{tmp_path}/rgb.sock"
    server.add_insecure_port(address)
    server.start()
    with grpc.insecure_channel(address) as channel:
        grpc.channel_ready_future(channel).result(timeout=3)
        yield rpc.DeviceServiceStub(channel), lighting, lcds
    server.stop(0).wait()
    for controller in lcds.values():
        controller.close()


def test_native_rgb_contract_preserves_seven_lcds_and_fan_control(running):
    stub, lighting, lcds = running
    devices = stub.ListDevices(list_devices_pb2.ListDevicesRequest()).devices
    assert len(devices) == 11
    assert {d.id for d in devices if "lcd" in d.info.channels} == set(lcds)
    for device in devices:
        stub.InitializeDevice(initialize_device_pb2.InitializeDeviceRequest(device_id=device.id))
        if device.id not in lighting.devices:
            continue
        assert list(device.info.lighting_speeds) == ["Slowest", "Slow", "Normal", "Fast", "Fastest"]
        for channel, info in device.info.channels.items():
            if info.WhichOneof("options") is None:
                assert channel in lighting.devices[device.id].rpm_channels
                continue
            assert info.WhichOneof("options") == "lighting_modes"
            for mode in info.lighting_modes.lighting_mode:
                setting = light.LightingSetting(mode=mode.name, speed="Slow",
                                                colors=[light.RGB(r=64)] * mode.min_colors)
                stub.Lighting(light.LightingRequest(device_id=device.id, channel_id=channel,
                                                     setting=setting))
                assert lighting.last_packets[(device.id, channel)]
            stub.ResetChannel(reset_channel_pb2.ResetChannelRequest(
                device_id=device.id, channel_id=channel))
    assert stub.Health(health_pb2.HealthRequest()).status == 1
    system_status = stub.Status(status_pb2.StatusRequest(device_id=DEVICE_ID))
    assert len(system_status.status) == 7
    assert all(s.speed.HasField("duty") and not s.speed.HasField("rpm")
               for s in system_status.status)
    with pytest.raises(grpc.RpcError) as error:
        stub.FixedDuty(fixed_duty_pb2.FixedDutyRequest())
    assert error.value.code() == grpc.StatusCode.UNIMPLEMENTED


def test_aura_native_lighting_modes_and_reset(running):
    stub, lighting, _ = running
    aura = AuraDevice(Endpoint("aura", "sim-aura", 0, 0), Layout("AULA3-AR32-0201", 3, 1))
    lighting.devices[aura.id] = aura
    devices = stub.ListDevices(list_devices_pb2.ListDevicesRequest()).devices
    device = next(d for d in devices if d.id == aura.id)
    assert set(device.info.channels) == {"argb-1", "argb-2", "argb-3"}
    assert not device.info.lighting_speeds
    stub.InitializeDevice(initialize_device_pb2.InitializeDeviceRequest(device_id=aura.id))
    assert not lighting.last_packets
    for channel, info in device.info.channels.items():
        for mode in info.lighting_modes.lighting_mode:
            assert not mode.speed_enabled and not mode.backward_enabled
            stub.Lighting(light.LightingRequest(
                device_id=aura.id, channel_id=channel, setting=light.LightingSetting(
                    mode=mode.name, colors=[light.RGB(r=120)] * mode.min_colors)))
            assert len(lighting.last_packets[(aura.id, channel)]) == 3
        stub.ResetChannel(reset_channel_pb2.ResetChannelRequest(
            device_id=aura.id, channel_id=channel))
        assert lighting.last_packets[(aura.id, channel)][1][5] == 0
    assert not stub.Status(status_pb2.StatusRequest(device_id=aura.id)).status


@pytest.mark.parametrize("device,channel,setting,code", [
    ("sim-1", "lcd", light.LightingSetting(mode="off"), grpc.StatusCode.NOT_FOUND),
    ("tl-rgb-wired-sim-hub", "lcd", light.LightingSetting(mode="off"), grpc.StatusCode.NOT_FOUND),
    ("tl-rgb-wired-sim-hub", "port-1", None, grpc.StatusCode.INVALID_ARGUMENT),
    ("tl-rgb-wired-sim-hub", "port-1", light.LightingSetting(mode="static"),
     grpc.StatusCode.INVALID_ARGUMENT),
])
def test_invalid_rgb_requests_do_not_write(running, device, channel, setting, code):
    stub, lighting, _ = running
    with pytest.raises(grpc.RpcError) as error:
        stub.Lighting(light.LightingRequest(device_id=device, channel_id=channel, setting=setting))
    assert error.value.code() == code
    assert not lighting.last_packets


def test_transport_failure_is_reported_without_breaking_lcd(running, monkeypatch):
    stub, lighting, _ = running
    def fail(*_):
        raise OSError("RF disconnected")
    monkeypatch.setattr(lighting, "_wireless", fail)
    device = next(d for d in lighting.devices.values() if d.group)
    with pytest.raises(grpc.RpcError) as error:
        stub.Lighting(light.LightingRequest(device_id=device.id, channel_id="rgb",
                                             setting=light.LightingSetting(mode="off")))
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE
    assert stub.Health(health_pb2.HealthRequest()).status == 2
    stub.Status(status_pb2.StatusRequest(device_id="sim-1"))
    # A lighting error does not hide independently readable fan RPM.
    status = stub.Status(status_pb2.StatusRequest(device_id=device.id))
    assert len(status.status) == 3
    assert all(s.speed.HasField("rpm") and not s.speed.HasField("duty") for s in status.status)


class Radio:
    def __init__(self):
        self.group = WirelessGroup(bytes([1]) * 6, bytes([9]) * 6, 8, 1, 3,
                                   bytes(4), True, 0, 0)
        self.sent = []
        self.master_mac = self.group.master
        self.ack = True

    @contextmanager
    def wireless(self, *_):
        yield self, self

    def groups(self):
        return [self.group]

    def master(self, *_):
        return self.master_mac

    def send(self, group, packet):
        self.sent.append(packet)
        if packet[1] == 0x27:
            self.group = replace(self.group, mb_sync=False, sequence=packet[17])
        elif self.ack and packet[18] == packet[19] - 1:
            self.group = replace(self.group, effect=packet[14:18])


def radio_lighting():
    radio = Radio()
    lighting = Lighting(radio)
    lighting.discover([Endpoint("tx", "fake-tx", 0, 0), Endpoint("rx", "fake-rx", 0, 0)])
    return lighting, radio, next(iter(lighting.devices.values()))


def test_only_rgb_commands_and_rf_ack_required(monkeypatch):
    lighting, radio, device = radio_lighting()
    lighting.apply(device, "rgb", light.LightingSetting(mode="rainbow"))
    assert {packet[1] for packet in radio.sent} == {0x27, 0x20}
    assert radio.sent[0][1] == 0x27
    assert device.error is None
    # No RGB payload before ownership/layout verification.
    radio.sent.clear()
    radio.master_mac = bytes([2]) * 6
    with pytest.raises(OSError, match="another transmitter"):
        lighting.apply(device, "rgb", light.LightingSetting(mode="off"))
    assert not radio.sent
    radio.master_mac = radio.group.master
    radio.group = replace(radio.group, fans=4)
    with pytest.raises(OSError, match="layout changed"):
        lighting.apply(device, "rgb", light.LightingSetting(mode="off"))
    assert not radio.sent


def test_missing_rf_ack_is_failure():
    lighting, radio, device = radio_lighting()
    radio.ack = False
    with pytest.raises(TimeoutError, match="acknowledge"):
        lighting.apply(device, "rgb", light.LightingSetting(mode="off"))
    assert device.error


def test_startup_discovery_failure_keeps_service_available():
    class Missing:
        def wireless(self, *_):
            raise PermissionError("no USB access")
    lighting = Lighting(Missing())
    lighting.discover([Endpoint("tx", "fake-tx", 0, 0), Endpoint("rx", "fake-rx", 0, 0)])
    assert not lighting.devices
    assert "no USB access" in lighting.discovery_errors[0]


def test_wireless_rpm_grpc_failure_and_recovery_keep_lcd_available(running, monkeypatch):
    stub, lighting, _ = running
    device = next(d for d in lighting.devices.values() if d.group)
    group = device.group
    good = {group.mac: WirelessRpm(group.master, (32,) * group.fans,
                                  (1200,) * group.fans)}
    replies = iter([good, {}, {}, {}, good])
    class Receiver:
        @contextmanager
        def open(self, endpoint):
            assert endpoint.kind == "rx"
            yield self
        def rpm_records(self):
            return next(replies)
    lighting.simulate = False
    lighting.transport = Receiver()
    monkeypatch.setattr("lianli_lcd.rgb.time.sleep", lambda _: None)
    request = status_pb2.StatusRequest(device_id=device.id)
    assert len(stub.Status(request).status) == group.fans
    with pytest.raises(grpc.RpcError) as error:
        stub.Status(request)
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE
    assert "3 fresh reads failed" in error.value.details()
    stub.Status(status_pb2.StatusRequest(device_id="sim-1"))
    assert len(stub.Status(request).status) == group.fans
