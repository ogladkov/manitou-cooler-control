"""Per-screen playback lifecycle; the first frame is acknowledged synchronously."""

import logging
import threading
import time

from .media import decode_media, read_media

LOG = logging.getLogger(__name__)
# Wired 0x46 is a live stream, including when the source is a static PNG.
# Keep that stream alive while CoolerControl skips unchanged sensor values.
WIRED_REFRESH_SECONDS = 1.0


class Controller:
    def __init__(self, transport, image_roots):
        self.transport = transport
        self.image_roots = image_roots
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.brightness = 50
        self.orientation = 0
        self.source = None
        self.frames = []
        self.error = None
        self.off = False

    def _stop(self):
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(timeout=10)
            if self.worker.is_alive():
                raise TimeoutError("LCD worker did not stop")
            self.worker = None
        self.stop_event.clear()

    def _animate(self):
        index = 0
        animated = len(self.frames) > 1
        wired = self.transport.screen.kind == "wired"
        deadline = time.monotonic() + self.frames[0].duration if animated else float("inf")
        try:
            while True:
                now = time.monotonic()
                wake = min(deadline, now + WIRED_REFRESH_SECONDS) if wired else deadline
                if self.stop_event.wait(max(0, wake - now)):
                    break
                now = time.monotonic()
                if animated and now >= deadline:
                    index = (index + 1) % len(self.frames)
                    deadline = max(deadline + self.frames[index].duration, now)
                self.transport.frame(self.frames[index].jpeg)
        except Exception as exc:
            self.error = str(exc)
            LOG.exception("LCD playback failed for %s", self.transport.screen.id)
            self.transport.close()

    def _start(self):
        if self.frames and (len(self.frames) > 1 or self.transport.screen.kind == "wired"):
            self.worker = threading.Thread(target=self._animate, daemon=True,
                                           name=self.transport.screen.id)
            self.worker.start()

    def apply(self, setting):
        if setting.mode not in ("image", "none"):
            raise ValueError("Expected image or none; CoolerControl renders temp/carousel to image")
        if setting.HasField("brightness") and setting.brightness > 100:
            raise ValueError("Brightness must be 0..100")
        if setting.HasField("orientation") and setting.orientation not in (0, 90, 180, 270):
            raise ValueError("Orientation must be 0, 90, 180 or 270")
        with self.lock:
            orientation = setting.orientation if setting.HasField("orientation") else self.orientation
            brightness = setting.brightness if setting.HasField("brightness") else self.brightness
            source = self.source
            if setting.HasField("image_path"):
                # Read immediately: CoolerControl overwrites single_temp.png on the next update.
                source = read_media(setting.image_path, self.image_roots)
            frames = decode_media(source, orientation) if source is not None else []
            if setting.mode == "image" and not frames:
                raise ValueError("image_path is required for the first image")
            self._stop()
            try:
                if setting.mode == "none":
                    self.transport.settings(0)
                else:
                    if setting.HasField("brightness") or not self.frames or self.error or self.off:
                        self.transport.settings(brightness)
                    self.transport.frame(frames[0].jpeg)
                self.brightness, self.orientation = brightness, orientation
                self.source, self.frames = source, frames
                self.error = None
                self.off = setting.mode == "none"
                if setting.mode == "image":
                    self._start()
            except Exception as exc:
                self.error = str(exc)
                self.transport.close()
                raise

    def initialize(self):
        with self.lock:
            self._stop()
            self.transport.close()
            try:
                self.transport.settings(0 if self.off else self.brightness)
                if self.frames and not self.off:
                    self.transport.frame(self.frames[0].jpeg)
                    self._start()
                self.error = None
            except Exception as exc:
                self.error = str(exc)
                self.transport.close()
                raise

    def close(self):
        with self.lock:
            self._stop()
            self.transport.close()
