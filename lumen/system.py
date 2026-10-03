"""Read-only desktop discovery and Wayland capture selection."""

from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess


class DesktopError(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 8) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopError(f"Cannot run {args[0]}: {exc}") from exc
    if result.returncode:
        raise DesktopError(
            (result.stderr or result.stdout).strip() or f"{args[0]} failed"
        )
    return result.stdout.strip()


def get_monitors() -> list[dict]:
    """Return Hyprland monitors, retaining logical origin and native dimensions."""
    try:
        monitors = json.loads(_run(["hyprctl", "monitors", "-j"]))
    except (DesktopError, ValueError):
        return []
    return [
        dict(
            m,
            logical_width=round(
                m["height" if m.get("transform", 0) % 2 else "width"]
                / m.get("scale", 1)
            ),
            logical_height=round(
                m["width" if m.get("transform", 0) % 2 else "height"]
                / m.get("scale", 1)
            ),
        )
        for m in monitors
        if not m.get("disabled")
    ]


def get_windows() -> list[dict]:
    """Window bounds are a fixed screen region, not isolated window capture."""
    try:
        clients = json.loads(_run(["hyprctl", "clients", "-j"]))
    except (DesktopError, ValueError):
        return []
    visible_workspaces = set()
    for monitor in get_monitors():
        for key in ("activeWorkspace", "specialWorkspace"):
            workspace_id = monitor.get(key, {}).get("id")
            if workspace_id:
                visible_workspaces.add(workspace_id)
    windows = []
    for client in clients:
        if not client.get("mapped", True) or client.get("hidden"):
            continue
        if (
            visible_workspaces
            and not client.get("pinned")
            and client.get("workspace", {}).get("id") not in visible_workspaces
        ):
            continue
        x, y = client.get("at", [0, 0])
        w, h = client.get("size", [0, 0])
        if w > 0 and h > 0:
            windows.append(
                {
                    **client,
                    "geometry": f"{x},{y} {w}x{h}",
                    "label": client.get("title") or client.get("class") or "Window",
                }
            )
    return windows


def get_audio_sources() -> list[dict]:
    try:
        sources = json.loads(_run(["pactl", "-f", "json", "list", "sources"]))
    except (DesktopError, ValueError):
        return []
    return [
        {
            "name": s["name"],
            "description": s.get("description", s["name"]),
            "is_monitor": s["name"].endswith(".monitor"),
            "state": s.get("state", ""),
        }
        for s in sources
    ]


_VIDEO_CAPABILITY = struct.Struct("=16s32s32s6I")
_VIDIOC_QUERYCAP = 0x80685600  # _IOR('V', 0, struct v4l2_capability), Linux
_CAP_CAPTURE = 0x00000001 | 0x00001000
_CAP_M2M = 0x00008000 | 0x00004000
_CAP_IO = 0x01000000 | 0x04000000
_CAP_DEVICE_CAPS = 0x80000000


def _query_camera(path: Path) -> dict:
    """Read V4L2 capabilities only; never negotiate a format or start a stream."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISCHR(metadata.st_mode):
            raise ValueError("A camera must be a character device")
        data = bytearray(_VIDEO_CAPABILITY.size)
        fcntl.ioctl(fd, _VIDIOC_QUERYCAP, data, True)
    finally:
        os.close(fd)
    driver, card, bus, version, capabilities, device_caps, *_ = _VIDEO_CAPABILITY.unpack(data)

    def text(value):
        return value.split(b"\0", 1)[0].decode("utf-8", "replace").strip()

    return {
        "name": text(card),
        "driver": text(driver),
        "bus_info": text(bus),
        "capabilities": device_caps if capabilities & _CAP_DEVICE_CAPS else capabilities,
        "device_number": metadata.st_rdev,
    }


def _camera_aliases(dev_root: Path) -> dict[Path, Path]:
    aliases = {}
    # Prefer serial-based identity, then physical connection identity. Several
    # by-path aliases may point to one node; choose a deterministic spelling.
    for directory in ("by-id", "by-path"):
        for alias in sorted((dev_root / "v4l" / directory).glob("*")):
            try:
                target = alias.resolve(strict=True)
            except OSError:
                continue
            aliases.setdefault(target, alias)
    return aliases


def get_cameras(
    *, sys_root: str | Path = "/sys/class/video4linux", dev_root: str | Path = "/dev"
) -> list[dict]:
    """Discover usable capture nodes without opening a camera stream.

    Each entry includes ``id`` for saved selection, canonical ``path`` for the
    capture backend, and human-readable ``name``/``label``. Persistent by-id or
    by-path aliases take precedence over driver/bus/sysfs-index identities.
    ``identity_stable`` is false only when identity falls back to a device node.

    Metadata, output-only, and memory-to-memory codec nodes are excluded using
    per-node capabilities, not the union advertised by the physical device.
    Aliases to one node are deduplicated; distinct capture endpoints are kept so
    RGB/IR and other multi-sensor devices do not silently lose a usable stream.
    An inaccessible or disconnected node is skipped rather than guessed usable.
    """
    sys_root, dev_root = Path(sys_root), Path(dev_root)
    aliases = _camera_aliases(dev_root)
    nodes = sorted(
        (p for p in dev_root.glob("video*") if re.fullmatch(r"video\d+", p.name)),
        key=lambda p: int(p.name[5:]),
    )
    cameras, seen_paths, seen_devices = [], set(), set()
    for node in nodes:
        try:
            path = node.resolve(strict=True)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            info = _query_camera(path)
            caps = info["capabilities"]
            if not caps & _CAP_CAPTURE or not caps & _CAP_IO or caps & _CAP_M2M:
                continue
            number = info["device_number"]
            if number in seen_devices:
                continue
            seen_devices.add(number)
        except (OSError, ValueError):
            continue

        alias = aliases.get(path)
        try:
            index = int((sys_root / node.name / "index").read_text().strip())
        except (OSError, ValueError):
            index = None
        bus = info["bus_info"]
        stable = bool(alias or (bus and index is not None))
        identity = (
            str(alias) if alias else
            f"v4l2:{info['driver']}:{bus}:index:{index}" if stable else str(path)
        )
        name = info["name"] or node.name
        cameras.append({
            **info,
            "id": identity,
            "identity_stable": stable,
            "path": str(path),
            "stable_path": str(alias) if alias else None,
            "name": name,
            "label": f"{name} · {node.name}",
        })
    return cameras


def select_region() -> str | None:
    """Let the user select logical desktop coordinates; Escape returns None."""
    try:
        result = subprocess.run(
            ["slurp", "-f", "%x,%y %wx%h"], capture_output=True, text=True, timeout=120
        )
    except OSError as exc:
        raise DesktopError(f"Cannot open region selection with slurp: {exc}") from exc
    except subprocess.TimeoutExpired:
        return None
    if result.returncode:
        return None
    region = result.stdout.strip()
    parse_geometry(region)
    return region


def parse_geometry(geometry: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"(-?\d+),(-?\d+)\s+(\d+)x(\d+)", geometry.strip())
    if not match:
        raise ValueError("Region must use logical coordinates: x,y WIDTHxHEIGHT")
    x, y, width, height = map(int, match.groups())
    if width < 2 or height < 2:
        raise ValueError("Recording region must be at least 2 × 2 pixels.")
    return x, y, width, height


def diagnostics() -> dict:
    binaries = {
        name: shutil.which(name)
        for name in (
            "gpu-screen-recorder",
            "wf-recorder",
            "ffmpeg",
            "ffprobe",
            "slurp",
            "hyprctl",
            "pactl",
            "mpv",
        )
    }
    report = {
        "session": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        "binaries": binaries,
        "monitors": get_monitors(),
        "audio_sources": get_audio_sources(),
        "cameras": get_cameras(),
        "render_devices": [str(p) for p in Path("/dev/dri").glob("renderD*")],
        "issues": [],
    }
    if report["session"] != "wayland":
        report["issues"].append("Lumen is designed for a Wayland session.")
    if not binaries["gpu-screen-recorder"] and not binaries["wf-recorder"]:
        report["issues"].append(
            "Install gpu-screen-recorder or wf-recorder to capture."
        )
    if not report["monitors"]:
        report["issues"].append(
            "Cannot read Hyprland monitors. Launch Lumen inside your desktop session."
        )
    if not binaries["ffmpeg"] or not binaries["ffprobe"]:
        report["issues"].append(
            "FFmpeg and ffprobe are required for playback metadata and export."
        )
    if binaries["gpu-screen-recorder"]:
        try:
            report["gpu_info"] = _run(["gpu-screen-recorder", "--info"], timeout=12)
        except DesktopError as exc:
            report["gpu_info"] = str(exc)
    return report


def probe_media(path: str | Path) -> dict:
    try:
        data = json.loads(
            _run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path),
                ],
                timeout=20,
            )
        )
    except (DesktopError, ValueError) as exc:
        raise DesktopError(f"Cannot read recording metadata: {exc}") from exc
    video = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if not video:
        raise DesktopError(
            "The recording has no readable video stream. Its raw file was preserved."
        )
    try:
        duration = float(
            data.get("format", {}).get("duration") or video.get("duration") or 0
        )
        width, height = int(video["width"]), int(video["height"])
    except (ValueError, KeyError, TypeError) as exc:
        raise DesktopError(
            "The recording has incomplete video metadata. Its raw file was preserved."
        ) from exc
    if not math.isfinite(duration) or duration <= 0 or width <= 0 or height <= 0:
        raise DesktopError(
            "The recording has no usable duration or frame dimensions. Its raw file was preserved."
        )
    audio_count = sum(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return {
        "duration": duration,
        "width": width,
        "height": height,
        "video_codec": video.get("codec_name"),
        "has_audio": audio_count > 0,
        "audio_stream_count": audio_count,
        "size_bytes": int(data.get("format", {}).get("size") or 0),
    }
