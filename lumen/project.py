"""Private recording folders and small, atomically written JSON manifests.

This module uses only the standard library so recordings remain discoverable
even when the desktop or capture dependencies are unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any
import uuid


MANIFEST = "project.json"
MAX_JSON_BYTES = 16 * 1024 * 1024


def library_root(root: str | os.PathLike[str] | None = None) -> Path:
    """Find the library without creating it or depending on GTK."""
    selected = root if root is not None else os.environ.get("LUMEN_LIBRARY")
    if selected is not None:
        return Path(selected).expanduser().resolve()
    videos = Path.home() / "Videos"
    try:
        result = subprocess.run(
            ["xdg-user-dir", "VIDEOS"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        candidate = Path(result.stdout.strip()).expanduser()
        if result.returncode == 0 and result.stdout.strip() and candidate.is_absolute():
            videos = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
    return (videos / "Lumen").resolve()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"JSON file is too large: {path}")
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return result


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    # Serialize before touching the existing manifest, including rejection of
    # NaN/Infinity, which are not JSON and often signal corrupt media metadata.
    data = (
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("JSON metadata exceeds the 16 MiB size limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        # Persist the directory entry as well as the file contents on Linux.
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def create_project(root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Create a unique private recording folder and its initial manifest."""
    library = library_root(root)
    library.mkdir(mode=0o700, parents=True, exist_ok=True)
    now = datetime.now().astimezone()
    # A random suffix avoids both same-second recordings and concurrent creators.
    while True:
        folder = library / f"{now:%Y-%m-%d_%H-%M-%S}-{uuid.uuid4().hex[:8]}"
        try:
            folder.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    project: dict[str, Any] = {
        "schema_version": 1,
        "path": str(folder),
        "source": "source.mkv",
        "name": now.strftime("Recording · %b %d, %Y at %H:%M"),
        "created_at": now.isoformat(timespec="seconds"),
        "status": "new",
        "duration": 0,
        "width": 0,
        "height": 0,
        "fps": 60,
    }
    try:
        save_project(project)
    except Exception:
        # Only remove an empty directory created by this call.
        try:
            folder.rmdir()
        except OSError:
            pass
        raise
    return project


def save_project(project: dict[str, Any]) -> None:
    """Save all supplied metadata; replacing a file never truncates it in place."""
    if not isinstance(project, dict) or not isinstance(
        project.get("path"), (str, os.PathLike)
    ):
        raise ValueError("A project needs a path to its recording folder")
    folder = Path(project["path"]).expanduser().resolve()
    stored = dict(project)
    stored["path"] = str(folder)
    stored["updated_at"] = _now()
    _atomic_json(folder / MANIFEST, stored)
    project["path"] = stored["path"]
    project["updated_at"] = stored["updated_at"]


def _number(value: Any, default: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return int(default) if integer else default
    try:
        valid = math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        return int(default) if integer else default
    return int(value) if integer else value


def load_project(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a folder or project.json, retaining extension/editor metadata.

    Invalid JSON raises ValueError; missing/unreadable files raise OSError.
    The library scanner skips those files. Known display fields with malformed
    types are normalized, and stale absolute paths from moved projects are fixed.
    """
    location = Path(path).expanduser().resolve()
    manifest = location / MANIFEST if location.is_dir() else location
    project = _read_json(manifest)
    project["path"] = str(manifest.parent)
    for key, fallback in (
        ("source", "source.mkv"),
        ("name", manifest.parent.name),
        ("status", "new"),
    ):
        if not isinstance(project.get(key), str) or not project[key].strip():
            project[key] = fallback
    if not isinstance(project.get("created_at"), str):
        project["created_at"] = datetime.fromtimestamp(
            manifest.stat().st_mtime, timezone.utc
        ).isoformat()
    project["duration"] = _number(project.get("duration"), 0)
    project["width"] = _number(project.get("width"), 0, integer=True)
    project["height"] = _number(project.get("height"), 0, integer=True)
    project["fps"] = _number(project.get("fps"), 60)
    return project


def _created_key(project: dict[str, Any]) -> tuple[float, str]:
    try:
        stamp = datetime.fromisoformat(project["created_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        timestamp = stamp.timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        timestamp = 0.0
    return timestamp, project["path"]


def list_projects(root: str | os.PathLike[str] | None = None) -> list[dict[str, Any]]:
    """Newest first; a damaged manifest cannot hide other recordings."""
    library = library_root(root)
    try:
        children = list(library.iterdir())
    except OSError:
        return []
    projects = []
    for child in children:
        if not child.is_dir():
            continue
        try:
            projects.append(load_project(child))
        except (OSError, ValueError, UnicodeError, RecursionError):
            continue
    return sorted(projects, key=_created_key, reverse=True)


def _settings_path() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    config = Path(configured).expanduser() if configured else Path.home() / ".config"
    if not config.is_absolute():
        config = Path.home() / ".config"
    return config / "lumen" / "settings.json"


def load_settings() -> dict[str, Any]:
    try:
        return _read_json(_settings_path())
    except (OSError, ValueError, UnicodeError, RecursionError):
        return {}


def save_settings(settings: dict[str, Any]) -> None:
    if not isinstance(settings, dict):
        raise ValueError("Settings must be a dictionary")
    _atomic_json(_settings_path(), settings)
