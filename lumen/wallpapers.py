"""Bundled backdrops and portable, normalized custom wallpaper imports."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


BUILTIN_WALLPAPERS = {"aurora": "Aurora", "dusk": "Dusk", "glacier": "Glacier"}
DEFAULT_WALLPAPER = "builtin:aurora"
BACKGROUND_STYLES = ["midnight", "violet", "sand", "none", "wallpaper"]
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def wallpaper_path(project: dict, selection: str) -> Path:
    """Resolve a bundled ID or project-relative image; never fall back silently."""
    if not isinstance(selection, str) or not selection:
        raise ValueError("Choose a wallpaper or add an image first.")
    if selection.startswith("builtin:"):
        name = selection.removeprefix("builtin:")
        if name not in BUILTIN_WALLPAPERS:
            raise ValueError("Unknown built-in wallpaper. Choose another wallpaper.")
        path = Path(__file__).parent / "wallpapers" / f"{name}.jpg"
    else:
        folder = Path(project["path"]).resolve()
        relative = Path(selection)
        path = (folder / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(folder / "wallpapers"):
            raise ValueError("Custom wallpapers must be stored in this project's wallpapers folder.")
    if not path.is_file():
        raise ValueError("Wallpaper image is missing. Add it again or choose another wallpaper.")
    return path


def custom_wallpapers(project: dict) -> list[dict]:
    """Read gallery metadata defensively, retaining missing entries for repair."""
    entries = project.get("wallpapers", [])
    if not isinstance(entries, list):
        return []
    result = []
    seen = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        path, name = item.get("path"), item.get("name")
        if (
            not isinstance(path, str) or not path.startswith("wallpapers/")
            or Path(path).is_absolute() or ".." in Path(path).parts
            or path in seen
        ):
            continue
        seen.add(path)
        result.append({"path": path, "name": name if isinstance(name, str) and name.strip() else "Custom image"})
    return result


def import_wallpaper(project: dict, source: str | Path) -> dict:
    """Decode PNG/JPEG/WebP off-thread and copy a still JPEG into the project.

    Content-addressed files are immutable, so replacing a selection cannot alter
    an export already in progress. The caller publishes the gallery metadata.
    """
    source = Path(source).expanduser().resolve()
    if not source.is_file():
        raise ValueError("Wallpaper image not found.")
    if source.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError("Choose a wallpaper smaller than 50 MiB.")
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(source)],
            check=True, capture_output=True, text=True, timeout=15,
        )
        streams = json.loads(result.stdout).get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        if video.get("codec_name") not in {"png", "mjpeg", "webp"}:
            raise ValueError("Choose a PNG, JPEG, or WebP image.")
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("Choose a wallpaper with at most 40 megapixels.")
        folder = Path(project["path"]) / "wallpapers"
        folder.mkdir(mode=0o700, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=folder) as temporary:
            image = Path(temporary) / "wallpaper.jpg"
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-nostdin", "-i", str(source),
                    "-map", "0:v:0", "-frames:v", "1", "-vf",
                    "scale=w='min(3840,iw)':h='min(3840,ih)':force_original_aspect_ratio=decrease",
                    "-pix_fmt", "yuvj444p", "-q:v", "2", "-update", "1", str(image),
                ],
                check=True, capture_output=True, timeout=30,
            )
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            destination = folder / f"{digest}.jpg"
            image.chmod(0o600)
            # A repeat import is the same immutable asset, not a new gallery tile.
            if not destination.exists():
                image.replace(destination)
    except FileNotFoundError as exc:
        raise ValueError("FFmpeg and ffprobe are required to add wallpapers.") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError("This image could not be read. Choose a valid PNG, JPEG, or WebP image.") from exc
    return {"path": destination.relative_to(Path(project["path"])).as_posix(), "name": source.stem}
