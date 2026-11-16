"""Display-only transport for the cm01 Android/CDC TRYX Panorama revision."""

import fcntl
import hashlib
import os
from pathlib import Path
import re
import select
import subprocess
import termios
import tempfile
import threading
import time
import tty
import uuid

from .tryx_renderer import TryxRenderer
from .tryx_protocol import MAX_FRAME, decode_response, encode_command, screen_config

MEDIA_DIR = "/sdcard/pcMedia"
KEEPALIVE_SECONDS = 10


class TryxTransport:
    def __init__(self, screen, lock_dir=Path("/run/cc-lianli-lcd/devices")):
        self.screen = screen
        self.lock_dir = lock_dir
        self.lock = threading.RLock()
        self.fd = None
        self.file_lock = None
        self.old_termios = None
        self.sequence = 0
        self.error = None
        self.stop = threading.Event()
        self.worker = None
        self.last_digest = None
        self.filename = None
        self.prefix = "manitou_cc_" + hashlib.sha256(screen.id.encode()).hexdigest()[:12] + "_"
        self.owned_pattern = re.compile(re.escape(self.prefix) + r"[0-9a-f]{32}\.(jpg|mp4)")
        self.previous = None
        self.renderer = TryxRenderer(self)
        self.last_brightness = None

    def adb_command(self, *args):
        # Never use -d/default-device: another Android device may be plugged in.
        if not self.screen.serial or not re.fullmatch(r"[A-Za-z0-9._:-]+", self.screen.serial):
            raise ValueError("TRYX requires a valid USB serial for targeted ADB access")
        return ["adb", "-s", self.screen.serial, *args]

    def _adb(self, *args, timeout=10):
        try:
            result = subprocess.run(self.adb_command(*args),
                                    capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise OSError("TRYX screen control requires adb (sudo apt install adb)") from exc
        if result.returncode:
            raise OSError("TRYX ADB operation failed; check USB access and close other screen apps")
        return result.stdout

    def _command(self, command, content=None):
        # Caller holds lock; this firmware always responds with AckNumber=0.
        # Commands must therefore be strictly synchronous, with no queued replies.
        self.sequence += 1
        termios.tcflush(self.fd, termios.TCIFLUSH)
        packet = memoryview(encode_command(command, content, self.sequence))
        deadline = time.monotonic() + 3
        while packet:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                raise TimeoutError("TRYX serial write timed out")
            try:
                packet = packet[os.write(self.fd, packet):]
            except BlockingIOError:
                continue
        data = bytearray()
        while time.monotonic() < deadline:
            if not select.select([self.fd], [], [], max(0, deadline - time.monotonic()))[0]:
                break
            chunk = os.read(self.fd, 4096)
            if not chunk:
                raise OSError("TRYX serial device disconnected")
            data.extend(chunk)
            if len(data) > MAX_FRAME * 2:
                raise ValueError("TRYX response exceeds frame limit")
            while True:
                start = data.find(b"\x5a")
                if start < 0:
                    break
                end = data.find(b"\x5a", start + 1)
                if end < 0:
                    break
                frame = bytes(data[start:end + 1])
                del data[:end + 1]
                response = decode_response(frame)
                # Firmware can send unrelated acknowledgements before conn info.
                if command == "conn" and "productId" not in response:
                    continue
                return response
        raise TimeoutError("TRYX serial response timed out")

    def _release(self):
        if self.fd is not None:
            try:
                if self.old_termios is not None:
                    termios.tcsetattr(self.fd, termios.TCSANOW, self.old_termios)
            except OSError:
                pass  # unplugged
            finally:
                os.close(self.fd)
                self.fd = None
                self.old_termios = None
        if self.file_lock is not None:
            self.file_lock.close()
            self.file_lock = None
        self.last_digest = None
        self.last_brightness = None
        self.renderer.close()

    def open(self):
        with self.lock:
            if self.fd is not None:
                return
            try:
                if not self.screen.tty:
                    raise OSError("TRYX CDC serial port is missing")
                self.lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.file_lock = (self.lock_dir / (self.prefix + ".lock")).open("a")
                fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fd = os.open(self.screen.tty, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.old_termios = termios.tcgetattr(self.fd)
                tty.setraw(self.fd)
                attrs = termios.tcgetattr(self.fd)
                attrs[4] = attrs[5] = termios.B115200
                attrs[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
                attrs[2] |= termios.CLOCAL | termios.CREAD
                termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
                info = self._command("conn")
                if info.get("productId") != "cm01" or info.get("OS") != "Android":
                    raise ValueError("Unsupported TRYX revision (expected Android cm01)")
                if self._adb("shell", "getprop", "ro.product.model").strip() != b"cm01_se":
                    raise ValueError("TRYX ADB model does not match discovered USB device")
                self.error = None
                if self.worker is None or not self.worker.is_alive():
                    self.stop.clear()
                    self.worker = threading.Thread(target=self._heartbeat, daemon=True,
                                                   name="tryx-keepalive")
                    self.worker.start()
            except Exception:
                self._release()
                raise

    def _heartbeat(self):
        while not self.stop.wait(KEEPALIVE_SECONDS):
            with self.lock:
                if self.fd is None:
                    continue
                try:
                    self._command("conn")
                    self.renderer.ping()
                    self.error = None
                except Exception as exc:
                    self.error = str(exc)
                    self._release()

    def settings(self, brightness):
        with self.lock:
            self.open()
            if brightness != self.last_brightness:
                self._command("brightness", {"value": brightness})
                self.last_brightness = brightness

    def display(self, media, extension, brightness):
        if extension not in ("jpg", "mp4"):
            raise ValueError("Unsupported TRYX media type")
        digest = hashlib.sha256(media).digest()
        with self.lock:
            self.open()
            if extension == "jpg":
                if digest != self.last_digest:
                    self.renderer.frame(media)
                    self.last_digest = digest
                self.settings(brightness)
                self.error = None
                return
            if digest != self.last_digest:
                filename = self.prefix + uuid.uuid4().hex + "." + extension
                with tempfile.TemporaryDirectory(prefix="manitou-tryx-") as folder:
                    local = Path(folder) / filename
                    local.write_bytes(media)
                    self._adb("shell", "mkdir", "-p", MEDIA_DIR)
                    self._adb("push", str(local), f"{MEDIA_DIR}/{filename}", timeout=20)
                config = screen_config(filename)
                self._command("waterBlockScreenId", config)
                self.previous, self.filename = self.filename, filename
                self.last_digest = digest
                self._cleanup()
            self.renderer.hide()
            self.settings(brightness)
            self.error = None

    def _cleanup(self):
        # Bounded cache, including files from previous processes. Only our exact
        # device-specific generated names qualify; Kanali/user media is preserved.
        listing = self._adb("shell", "ls", "-1", MEDIA_DIR).decode(errors="replace")
        for name in listing.splitlines():
            if self.owned_pattern.fullmatch(name) and name not in (self.filename, self.previous):
                self._adb("shell", "rm", "-f", f"{MEDIA_DIR}/{name}")

    def close(self):
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=15)
            if self.worker.is_alive():
                raise TimeoutError("TRYX keepalive did not stop")
            self.worker = None
        with self.lock:
            self._release()


class SimulatedTryxTransport:
    def __init__(self, screen):
        self.screen = screen
        self.error = None
        self.last_frame = None
        self.extension = None
        self.brightness = 50
        self.frames = 0

    def open(self):
        pass

    def settings(self, brightness):
        self.brightness = brightness

    def display(self, media, extension, brightness):
        self.last_frame, self.extension, self.brightness = media, extension, brightness
        self.frames += 1

    def close(self):
        pass
