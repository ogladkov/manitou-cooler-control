"""Persistent, acknowledged frame stream to TRYX's Android compositor."""

import hashlib
from importlib.resources import files
import os
import select
import struct
import subprocess
import time


class TryxRenderer:
    def __init__(self, transport):
        self.transport = transport
        self.process = None

    def _read(self, count, deadline):
        result = bytearray()
        while len(result) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("TRYX frame renderer response timed out")
            part = os.read(self.process.stdout.fileno(), count - len(result))
            if not part:
                raise OSError("TRYX frame renderer stopped unexpectedly")
            result.extend(part)
        return bytes(result)

    def _write(self, packet, deadline):
        packet = memoryview(packet)
        while packet:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.process.stdin], [], remaining)[1]:
                raise TimeoutError("TRYX frame transfer timed out")
            try:
                packet = packet[os.write(self.process.stdin.fileno(), packet):]
            except BlockingIOError:
                continue

    def _exchange(self, length, data=b""):
        deadline = time.monotonic() + 5
        self._write(struct.pack(">i", length) + data, deadline)
        if self._read(1, deadline) != b"\x01":
            raise OSError("TRYX frame renderer rejected the frame")

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        artifact = files("lianli_lcd").joinpath("data/tryx-frame-server.dex")
        if not artifact.is_file():
            raise OSError("TRYX renderer missing; run scripts/build-tryx-renderer.sh and rebuild wheels")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()[:16]
        remote = f"/data/local/tmp/manitou-tryx-{digest}.dex"
        # Installed wheels are unpacked; this path belongs to our package only.
        self.transport._adb("push", str(artifact), remote)
        command = self.transport.adb_command(
            "shell", "-T", "CLASSPATH=" + remote, "app_process", "/system/bin",
            "com.manitou.tryx.FrameServer")
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)
        try:
            if self._read(4, time.monotonic() + 5) != b"MTX1":
                raise OSError("Unsupported TRYX frame renderer protocol")
        except Exception:
            self.close()
            raise

    def frame(self, jpeg):
        self._start()
        self._exchange(len(jpeg), jpeg)

    def ping(self):
        if self.process is not None:
            self._exchange(0)

    def hide(self):
        if self.process is not None:
            self._exchange(-1)

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        # Closing stdin lets the Android process release its surface on EOF.
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        finally:
            process.stdout.close()
