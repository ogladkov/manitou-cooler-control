"""Bounded image/GIF decoding and 400x400 baseline JPEG encoding."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import stat
import time

from PIL import Image, ImageOps, UnidentifiedImageError

from .protocol import MAX_JPEG

MAX_INPUT = 20 * 1024 * 1024
MAX_FRAMES = 600
MAX_ENCODED = 32 * 1024 * 1024
Image.MAX_IMAGE_PIXELS = 16_000_000


@dataclass(frozen=True)
class Frame:
    jpeg: bytes
    duration: float


def jpeg_frame(image: Image.Image, orientation: int) -> bytes:
    image = ImageOps.fit(image.convert("RGB"), (400, 400), method=Image.Resampling.LANCZOS)
    if orientation:
        image = image.rotate(-orientation)
    for quality in (90, 80, 70, 60, 50, 40):
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, progressive=False, subsampling=2)
        data = output.getvalue()
        if len(data) <= MAX_JPEG:
            return data
    raise ValueError("Image cannot fit LCD JPEG payload limit")


def read_media(path: str, roots: list[Path]) -> bytes:
    resolved = Path(path).resolve(strict=True)
    if not any(resolved.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("Image path is outside configured image roots")
    if not stat.S_ISREG(resolved.stat().st_mode):
        raise ValueError("Image must be a regular file")
    with resolved.open("rb") as stream:
        data = stream.read(MAX_INPUT + 1)
    if len(data) > MAX_INPUT:
        raise ValueError("Image exceeds 20 MiB input limit")
    return data


def decode_media(data: bytes, orientation: int = 0) -> list[Frame]:
    deadline = time.monotonic() + 3
    if orientation not in (0, 90, 180, 270):
        raise ValueError("Orientation must be 0, 90, 180 or 270")
    if len(data) > MAX_INPUT:
        raise ValueError("Image exceeds 20 MiB input limit")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in ("PNG", "JPEG", "GIF", "WEBP"):
                raise ValueError("Supported formats: PNG, JPEG, GIF, WebP")
            if image.width * image.height > Image.MAX_IMAGE_PIXELS:
                raise ValueError("Image exceeds 16 megapixels")
            count = getattr(image, "n_frames", 1)
            if count > MAX_FRAMES:
                raise ValueError("Animation exceeds 600 frames")
            frames, size = [], 0
            for index in range(count):
                if time.monotonic() > deadline:
                    raise ValueError("Animation is too complex; use a shorter or smaller GIF")
                image.seek(index)
                jpeg = jpeg_frame(image, orientation)
                size += len(jpeg)
                if size > MAX_ENCODED:
                    raise ValueError("Decoded animation exceeds 32 MiB")
                # Cap playback at 15 fps to keep seven screens affordable over USB.
                duration = max(1 / 15, min(60, image.info.get("duration", 100) / 1000))
                frames.append(Frame(jpeg, duration))
            return frames
    except (UnidentifiedImageError, Image.DecompressionBombError, EOFError, OSError) as exc:
        raise ValueError(f"Invalid image: {exc}") from exc
