"""Native TRYX media playback with the same LCD contract as Lian Li screens."""

import threading

from .media import read_media
from .tryx_media import encode_media


class TryxController:
    def __init__(self, transport, image_roots):
        self.transport = transport
        self.image_roots = image_roots
        self.lock = threading.RLock()
        self.brightness = 50
        self.orientation = 0
        self.source = None
        self.encoded = None
        self.off = False
        self._error = None

    @property
    def error(self):
        return self._error or self.transport.error

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
            source = (read_media(setting.image_path, self.image_roots)
                      if setting.HasField("image_path") else self.source)
            if setting.mode == "image" and source is None:
                raise ValueError("image_path is required for the first image")
            encoded = self.encoded
            if source is not None and (source != self.source or orientation != self.orientation):
                encoded = encode_media(source, orientation)
            try:
                if setting.mode == "none":
                    self.transport.settings(0)
                else:
                    self.transport.display(*encoded, brightness)
                self.source, self.encoded = source, encoded
                self.orientation, self.brightness = orientation, brightness
                self.off = setting.mode == "none"
                self._error = None
            except Exception as exc:
                self._error = str(exc)
                self.transport.close()
                raise

    def initialize(self):
        with self.lock:
            self.transport.close()
            try:
                self.transport.open()
                if self.off:
                    self.transport.settings(0)
                elif self.encoded:
                    self.transport.display(*self.encoded, self.brightness)
                self._error = None
            except Exception as exc:
                self._error = str(exc)
                self.transport.close()
                raise

    def close(self):
        with self.lock:
            self.transport.close()
