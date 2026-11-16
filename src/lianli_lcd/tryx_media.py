"""Fit images to the panoramic display; encode GIFs for its native video player."""

from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from PIL import Image, ImageOps, UnidentifiedImageError

SIZE = (2240, 1080)
MAX_BYTES = 20 * 1024 * 1024


def encode_media(source, orientation=0):
    if orientation not in (0, 90, 180, 270):
        raise ValueError("Orientation must be 0, 90, 180 or 270")
    if len(source) > MAX_BYTES:
        raise ValueError("TRYX media exceeds 20 MiB")
    deadline = time.monotonic() + 3
    try:
        with Image.open(BytesIO(source)) as image:
            if image.format not in ("PNG", "JPEG", "GIF"):
                raise ValueError("TRYX accepts PNG, JPEG or GIF")
            if image.width * image.height > 16_000_000:
                raise ValueError("TRYX source exceeds 16 megapixels")
            animated = getattr(image, "n_frames", 1) > 1
            if animated:
                if image.format != "GIF":
                    raise ValueError("Use GIF for animated TRYX media")
                if image.n_frames > 600:
                    raise ValueError("TRYX GIF exceeds 600 frames")
                duration = 0
                for index in range(image.n_frames):
                    if time.monotonic() > deadline:
                        raise ValueError("TRYX GIF is too complex; use a smaller animation")
                    image.seek(index)
                    duration += image.info.get("duration", 100)
                if duration > 60_000:
                    raise ValueError("TRYX GIF exceeds 60 seconds")
            else:
                rgba = ImageOps.exif_transpose(image).convert("RGBA")
                if orientation:
                    rgba = rgba.rotate(-orientation, expand=True)
                fitted = ImageOps.contain(rgba, SIZE, Image.Resampling.LANCZOS)
                canvas = Image.new("RGB", SIZE, "black")
                canvas.paste(fitted, ((SIZE[0] - fitted.width) // 2,
                                     (SIZE[1] - fitted.height) // 2), fitted)
                output = BytesIO()
                canvas.save(output, "JPEG", quality=90)
                return output.getvalue(), "jpg"
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid TRYX image") from exc
    if not shutil.which("ffmpeg"):
        raise OSError("TRYX GIF playback requires ffmpeg (sudo apt install ffmpeg)")
    rotations = {0: "", 90: "transpose=1,", 180: "hflip,vflip,", 270: "transpose=2,"}
    filters = (rotations[orientation] + "scale=2240:1080:force_original_aspect_ratio=decrease:"
               "force_divisible_by=2,pad=2240:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=15")
    with tempfile.TemporaryDirectory(prefix="manitou-tryx-video-") as folder:
        source_path, target = Path(folder) / "input.gif", Path(folder) / "output.mp4"
        source_path.write_bytes(source)
        try:
            result = subprocess.run([
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-ignore_loop", "1", "-i", str(source_path), "-vf", filters, "-an",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "2",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-t", "60",
                "-fs", str(MAX_BYTES), str(target),
            ], capture_output=True, timeout=5, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("TRYX GIF conversion timed out; use a smaller animation") from exc
        if result.returncode or not target.exists():
            raise ValueError("TRYX GIF conversion failed")
        if target.stat().st_size >= MAX_BYTES:
            raise ValueError("Converted TRYX video exceeds 20 MiB; use a shorter GIF")
        return target.read_bytes(), "mp4"
