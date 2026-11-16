from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import threading
import time

import grpc
from PIL import Image
import pytest

from coolercontrol.device_service.v1 import (
    device_service_pb2_grpc as rpc, fixed_duty_pb2, health_pb2,
    initialize_device_pb2, lcd_pb2, list_devices_pb2, reset_channel_pb2, status_pb2,
)
from lianli_lcd.controller import Controller
from lianli_lcd.discovery import simulated_screens
from lianli_lcd.media import decode_media, read_media
from lianli_lcd.service import DeviceService
from lianli_lcd.transport import SimulatedTransport


@pytest.fixture
def running(tmp_path):
    controllers = {s.id: Controller(SimulatedTransport(s), [tmp_path])
                   for s in simulated_screens()}
    server = grpc.server(ThreadPoolExecutor(max_workers=8))
    rpc.add_DeviceServiceServicer_to_server(DeviceService(controllers, threading.Event()), server)
    socket = f"unix:{tmp_path}/service.sock"
    assert server.add_insecure_port(socket)
    server.start()
    channel = grpc.insecure_channel(socket)
    grpc.channel_ready_future(channel).result(timeout=3)
    yield rpc.DeviceServiceStub(channel), controllers
    channel.close()
    server.stop(0).wait()
    for controller in controllers.values():
        controller.close()


def write_image(tmp_path, animated=False):
    path = tmp_path / ("image.gif" if animated else "image.png")
    first = Image.new("RGB", (400, 400), "red")
    if animated:
        first.save(path, save_all=True, append_images=[Image.new("RGB", (400, 400), "blue")],
                   duration=80, loop=0)
    else:
        first.save(path)
    return str(path)


def test_native_device_contract_and_images_on_seven_screens(running, tmp_path):
    stub, controllers = running
    devices = stub.ListDevices(list_devices_pb2.ListDevicesRequest()).devices
    assert len(devices) == 7
    path = write_image(tmp_path)
    for device in devices:
        assert list(device.info.channels) == ["lcd"]
        assert not device.info.temps
        lcd = device.info.channels["lcd"].lcd_info
        assert lcd.screen_width == lcd.screen_height == 400
        assert [mode.name for mode in lcd.lcd_modes] == ["image", "temp", "carousel"]
        stub.InitializeDevice(initialize_device_pb2.InitializeDeviceRequest(device_id=device.id))
        stub.Lcd(lcd_pb2.LcdRequest(device_id=device.id, channel_id="lcd",
                 setting=lcd_pb2.LcdSetting(mode="image", image_path=path,
                                            brightness=65, orientation=90)))
        transport = controllers[device.id].transport
        assert transport.brightness == 65
        assert Image.open(BytesIO(transport.last_frame)).size == (400, 400)
    assert stub.Health(health_pb2.HealthRequest()).status == 1


def test_gif_stop_resume_and_optional_settings(running, tmp_path):
    stub, controllers = running
    controller = controllers["sim-1"]
    def apply(**kwargs):
        stub.Lcd(lcd_pb2.LcdRequest(device_id="sim-1", channel_id="lcd",
                                   setting=lcd_pb2.LcdSetting(mode="image", **kwargs)))
    apply(image_path=write_image(tmp_path, True), brightness=72, orientation=180)
    time.sleep(0.25)
    assert controller.transport.frames >= 3
    apply(image_path=write_image(tmp_path))
    before = controller.transport.frames
    time.sleep(0.15)
    assert controller.transport.frames == before
    assert controller.brightness == 72 and controller.orientation == 180
    stub.ResetChannel(reset_channel_pb2.ResetChannelRequest(device_id="sim-1", channel_id="lcd"))
    assert controller.transport.brightness == 0
    apply()
    assert controller.transport.brightness == 72


@pytest.mark.parametrize("device,channel,kwargs,code", [
    ("bad", "lcd", {}, grpc.StatusCode.NOT_FOUND),
    ("sim-1", "fan", {}, grpc.StatusCode.NOT_FOUND),
    ("sim-1", "lcd", {"brightness": 101}, grpc.StatusCode.INVALID_ARGUMENT),
    ("sim-1", "lcd", {"orientation": 45}, grpc.StatusCode.INVALID_ARGUMENT),
    ("sim-1", "lcd", {}, grpc.StatusCode.INVALID_ARGUMENT),
])
def test_invalid_requests(running, device, channel, kwargs, code):
    stub, _ = running
    with pytest.raises(grpc.RpcError) as error:
        stub.Lcd(lcd_pb2.LcdRequest(device_id=device, channel_id=channel,
                                   setting=lcd_pb2.LcdSetting(mode="image", **kwargs)))
    assert error.value.code() == code


def test_no_fan_control_and_error_health(running):
    stub, controllers = running
    with pytest.raises(grpc.RpcError) as error:
        stub.FixedDuty(fixed_duty_pb2.FixedDutyRequest())
    assert error.value.code() == grpc.StatusCode.UNIMPLEMENTED
    controllers["sim-1"].error = "USB disconnected"
    assert stub.Health(health_pb2.HealthRequest()).status == 2
    with pytest.raises(grpc.RpcError) as error:
        stub.Status(status_pb2.StatusRequest(device_id="sim-1"))
    assert error.value.code() == grpc.StatusCode.UNAVAILABLE


def test_media_validation_and_rotation(tmp_path):
    with pytest.raises(ValueError):
        decode_media(b"not an image")
    outside = tmp_path / "outside"
    outside.write_bytes(b"test")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        read_media(str(allowed / "link"), [allowed])
    im = Image.new("RGB", (400, 400), "black")
    im.paste("red", (0, 0, 200, 200))
    data = BytesIO()
    im.save(data, "PNG")
    rotated = Image.open(BytesIO(decode_media(data.getvalue(), 90)[0].jpeg))
    assert rotated.getpixel((300, 100))[0] > 240
    assert rotated.getpixel((100, 100))[0] < 10
