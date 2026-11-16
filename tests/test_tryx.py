from io import BytesIO
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import threading
import time

from PIL import Image
import pytest

from coolercontrol.device_service.v1 import lcd_pb2, list_devices_pb2
from lianli_lcd.discovery import Screen, discover, simulated_screens
from lianli_lcd.service import DeviceService
from lianli_lcd.tryx_controller import TryxController
from lianli_lcd.tryx_media import encode_media
from lianli_lcd.tryx_protocol import decode_response, encode_command
from lianli_lcd.tryx_transport import SimulatedTryxTransport, TryxTransport


def response(body=None, literal=False, status=200, length=None):
    body = json.dumps(body, ensure_ascii=False).encode() if body is not None else b""
    separator = b"\\r\\n" if literal else b"\r\n"
    headers = separator.join([f"1 {status}".encode(), b"AckNumber=0",
                              f"ContentLength={len(body) if length is None else length}".encode(),
                              b"ContentType=json"])
    message = headers + b"\r\n\r\n" + body
    raw = (len(message) + 5).to_bytes(2, "big") + message
    raw += bytes([sum(raw) % 256])
    # Independent escaping implementation.
    return b"Z" + b"".join({90: b"[\x01", 91: b"[\x02"}.get(v, bytes([v]))
                            for v in raw) + b"Z"


@pytest.mark.parametrize("literal", [False, True])
def test_protocol_real_header_variants_and_unicode(literal):
    assert decode_response(response({"value": "Ж[Z"}, literal)) == {"value": "Ж[Z"}
    packet = encode_command("brightness", {"value": "Ж[Z"})
    raw = packet[1:-1].replace(b"[\x01", b"Z").replace(b"[\x02", b"[")
    assert int.from_bytes(raw[:2], "big") == len(raw) + 2
    assert sum(raw[:-1]) % 256 == raw[-1]
    header, body = raw[2:-1].split(b"\r\n\r\n")
    assert f"ContentLength={len(body)}".encode() in header
    assert json.loads(body) == {"value": "Ж[Z"}


def test_protocol_rejects_corruption_and_non_display_commands():
    packet = bytearray(response({"ok": True}))
    packet[4] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        decode_response(bytes(packet))
    for frame in (b"bad", b"Z[\x03Z", response({}, length=99), response([1])):
        with pytest.raises(ValueError):
            decode_response(frame)
    with pytest.raises(OSError, match="403"):
        decode_response(response({}, status=403))
    with pytest.raises(ValueError):
        encode_command("reboot")


def test_discovery_does_not_claim_android_phones(tmp_path):
    usb, hid, tty = [tmp_path / name for name in ("usb", "hid", "tty")]
    for p in (usb, hid, tty):
        p.mkdir()
    for index, model in enumerate(("phone", "cm01_se", "cm01_se")):
        device = usb / f"3-{index}"
        device.mkdir()
        for key, value in {"idVendor": "18d1", "idProduct": "2d04", "manufacturer": "rockchip",
                           "product": model, "serial": f"test{index}",
                           "busnum": "3", "devnum": "6"}.items():
            (device / key).write_text(value)
        if index < 2:
            interface = device / "interface"
            interface.mkdir()
            port = tty / f"ttyACM{index}"
            port.mkdir()
            (port / "device").symlink_to(interface)
    screens = discover(usb, hid, tty)
    assert len(screens) == 1
    assert screens[0].id == "tryx-panorama-3-1"
    assert screens[0].tty == "/dev/ttyACM1"
    assert (screens[0].width, screens[0].height) == (2240, 1080)


def png(size=(400, 400), color="red"):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


def test_media_preserves_aspect_and_rotation():
    media, extension = encode_media(png((200, 100)), 90)
    assert extension == "jpg"
    image = Image.open(BytesIO(media))
    assert image.size == (2240, 1080)
    assert image.getpixel((1120, 100))[0] > 240
    assert image.getpixel((700, 540)) == (0, 0, 0)
    for source in (b"bad", b"x" * (20 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            encode_media(source)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_gif_converts_to_h264_with_native_dimensions(tmp_path):
    data = BytesIO()
    Image.new("RGB", (48, 48), "red").save(
        data, "GIF", save_all=True, append_images=[Image.new("RGB", (48, 48), "blue")],
        duration=200, loop=0)
    media, extension = encode_media(data.getvalue())
    assert extension == "mp4"
    video = tmp_path / "video.mp4"
    video.write_bytes(media)
    result = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(video)],
                            capture_output=True, check=True, timeout=10)
    stream = json.loads(result.stdout)["streams"][0]
    assert (stream["codec_name"], stream["width"], stream["height"]) == ("h264", 2240, 1080)
    assert 0.3 <= float(stream["duration"]) <= 0.6


def test_tryx_native_contract_off_resume_and_source_validation(tmp_path):
    screen = simulated_screens(True)[-1]
    transport = SimulatedTryxTransport(screen)
    controller = TryxController(transport, [tmp_path])
    service = DeviceService({screen.id: controller}, threading.Event())
    device = service.ListDevices(list_devices_pb2.ListDevicesRequest(), None).devices[0]
    assert device.name.startswith("TRYX PANORAMA 360")
    lcd = device.info.channels["lcd"].lcd_info
    assert (lcd.screen_width, lcd.screen_height) == (2240, 1080)
    controller.initialize()
    assert transport.frames == 0  # discovery leaves current artwork alone
    with pytest.raises(ValueError, match="image_path"):
        controller.apply(lcd_pb2.LcdSetting(mode="image"))
    source = tmp_path / "image.png"
    source.write_bytes(png())
    controller.apply(lcd_pb2.LcdSetting(mode="image", image_path=str(source), brightness=72))
    assert transport.brightness == 72
    assert Image.open(BytesIO(transport.last_frame)).size == (2240, 1080)
    controller.apply(lcd_pb2.LcdSetting(mode="none"))
    assert transport.brightness == 0
    controller.initialize()
    assert transport.brightness == 0
    controller.apply(lcd_pb2.LcdSetting(mode="image", orientation=90))
    assert transport.brightness == 72 and controller.orientation == 90
    controller.initialize()
    assert transport.brightness == 72
    source.write_bytes(png(color="blue"))
    controller.apply(lcd_pb2.LcdSetting(mode="image", image_path=str(source)))
    assert Image.open(BytesIO(transport.last_frame)).getpixel((1120, 540))[2] > 240
    controller.close()


def test_serial_fragmented_responses_empty_ack_and_exclusive_access(tmp_path, monkeypatch):
    master, slave = pty.openpty()
    screen = Screen("tryx-test", "tryx", "test", 1, 1, "test", tty=os.ttyname(slave),
                    width=2240, height=1080)
    transport = TryxTransport(screen, tmp_path)
    competitor = TryxTransport(screen, tmp_path)
    monkeypatch.setattr(transport, "_adb", lambda *a, **k: b"cm01_se\n")
    stopped = threading.Event()
    errors = []

    def firmware():
        buffer = b""
        try:
            while not stopped.is_set():
                if not select.select([master], [], [], .05)[0]:
                    continue
                buffer += os.read(master, 4096)
                if len(buffer) < 2 or not buffer.endswith(b"Z"):
                    continue
                is_conn = b"POST conn" in buffer
                buffer = b""
                reply = response({"productId": "cm01", "OS": "Android"}, literal=True)
                packet = response() + response({"value": 50}) + reply if is_conn else response()
                for start in range(0, len(packet), 17):
                    os.write(master, packet[start:start + 17])
                    time.sleep(.001)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=firmware)
    worker.start()
    try:
        transport.open()
        with pytest.raises(BlockingIOError):
            competitor.open()
        transport.settings(0)
        assert transport.fd is not None
        transport.close()
        assert transport.fd is None and transport.worker is None
    finally:
        stopped.set()
        worker.join(2)
        transport.close()
        competitor.close()
        os.close(master)
        os.close(slave)
    assert not errors


def test_adb_is_pinned_and_cleanup_only_owns_exact_names(tmp_path, monkeypatch):
    transport = TryxTransport(simulated_screens(True)[-1], tmp_path)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(subprocess, "run", run)
    transport._adb("get-state")
    assert calls[0][:3] == ["adb", "-s", "simulation"]
    current = transport.prefix + "a" * 32 + ".jpg"
    previous = transport.prefix + "b" * 32 + ".jpg"
    old = transport.prefix + "c" * 32 + ".mp4"
    transport.filename, transport.previous = current, previous
    deleted = []

    def adb(*args, **kwargs):
        if args[1] == "ls":
            return "\n".join([current, previous, old, "kanali.mp4", "../other.jpg",
                              "manitou_cc_other_" + "c" * 32 + ".jpg"]).encode()
        deleted.append(args[-1])
        return b""

    monkeypatch.setattr(transport, "_adb", adb)
    transport._cleanup()
    assert deleted == ["/sdcard/pcMedia/" + old]


def test_display_deduplicates_and_keeps_two_files(tmp_path, monkeypatch):
    transport = TryxTransport(simulated_screens(True)[-1], tmp_path)
    monkeypatch.setattr(transport, "open", lambda: None)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    files, commands = {}, []

    def adb(*args, **kwargs):
        if args[0] == "push":
            files[Path(args[2]).name] = Path(args[1]).read_bytes()
        elif args[1] == "ls":
            return "\n".join(files).encode()
        elif args[1] == "rm":
            del files[Path(args[-1]).name]
        return b""

    monkeypatch.setattr(transport, "_adb", adb)
    monkeypatch.setattr(transport, "_command", lambda *a: commands.append(a))
    for content in (b"one", b"two", b"three", b"three"):
        transport.display(content, "mp4", 70)
    assert len(files) == 2 and set(files.values()) == {b"two", b"three"}
    assert len([c for c in commands if c[0] == "waterBlockScreenId"]) == 3
    assert [c for c in commands if c[0] == "brightness"] == [("brightness", {"value": 70})]
    assert commands[-1][1]["playMode"] == "single"


def test_heartbeat_failure_is_visible_to_service(tmp_path, monkeypatch):
    import lianli_lcd.tryx_transport as module
    transport = TryxTransport(simulated_screens(True)[-1], tmp_path)
    transport.fd = 123
    monkeypatch.setattr(module, "KEEPALIVE_SECONDS", .01)
    def failed(*args):
        raise OSError("USB disconnected")
    monkeypatch.setattr(transport, "_command", failed)
    monkeypatch.setattr(transport, "_release", lambda: setattr(transport, "fd", None))
    transport.worker = threading.Thread(target=transport._heartbeat)
    transport.worker.start()
    controller = TryxController(transport, [tmp_path])
    try:
        deadline = time.monotonic() + 1
        while controller.error is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert controller.error == "USB disconnected"
    finally:
        controller.close()


def test_static_frames_never_reset_vendor_layout_or_write_media(tmp_path, monkeypatch):
    transport = TryxTransport(simulated_screens(True)[-1], tmp_path)
    monkeypatch.setattr(transport, "open", lambda: None)
    commands, frames = [], []
    monkeypatch.setattr(transport, "_command", lambda *a: commands.append(a))
    monkeypatch.setattr(transport.renderer, "frame", frames.append)
    def no_upload(*args, **kwargs):
        pytest.fail("Static frames must not touch the Android media directory")
    monkeypatch.setattr(transport, "_adb", no_upload)
    for content in (b"one", b"two", b"three", b"three"):
        transport.display(content, "jpg", 70)
    assert frames == [b"one", b"two", b"three"]
    assert commands == [("brightness", {"value": 70})]
    transport.settings(0)
    transport.display(b"three", "jpg", 70)
    assert frames == [b"one", b"two", b"three"]
    assert commands[-2:] == [("brightness", {"value": 0}), ("brightness", {"value": 70})]
    transport._release()
    transport.display(b"three", "jpg", 70)
    assert frames[-2:] == [b"three", b"three"]  # reconnect redraws the saved frame
