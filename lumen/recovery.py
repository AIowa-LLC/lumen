"""Durable, private snapshots of unfinished edits that cannot be committed."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any
import uuid

from .project import MAX_JSON_BYTES


_RECOVERY_NAME = re.compile(r"draft-recovery-\d{8}T\d{12}Z-[a-f0-9]{32}\.json")


def _project_folder(project: dict[str, Any]) -> Path:
    if not isinstance(project, dict):
        raise ValueError("A recovery draft needs its recording folder")
    location = project.get("path")
    if not isinstance(location, (str, os.PathLike)) or not str(location).strip():
        raise ValueError("A recovery draft needs its recording folder")
    folder = Path(location).expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise NotADirectoryError(f"Recovery location is not a recording folder: {folder}")
    return folder


def write_recovery(project: dict[str, Any], draft: dict[str, Any]) -> Path:
    """Preserve an unvalidated draft without changing its project or media.

    ``project`` is the caller's last valid project snapshot. ``draft`` may hold
    raw form text, candidate edits, and a validation error; it must be JSON data.
    Neither dictionary is modified or subjected to editor-layer validation.

    The existing project directory receives a uniquely named, mode-0600 JSON
    file. Publication never replaces an existing path, including a symlink.
    Returning means both file contents and the directory entry were fsynced.
    Serialization, size, and filesystem errors propagate to the caller. A
    complete recovery file may remain if directory fsync fails after publication;
    in that case this function raises rather than reporting a durable save.
    """
    if not isinstance(project, dict) or not isinstance(draft, dict):
        raise ValueError("Project and recovery draft must be JSON objects")
    folder = _project_folder(project)

    now = datetime.now(timezone.utc)
    document = {
        "schema_version": 1,
        "created_at": now.isoformat(timespec="microseconds"),
        "project": dict(project, path=str(folder)),
        "draft": draft,
    }
    data = (
        json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError("Recovery metadata exceeds the 16 MiB size limit")

    descriptor, temporary = tempfile.mkstemp(
        prefix=".draft-recovery-", suffix=".tmp", dir=folder
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

        for _ in range(32):
            destination = folder / (
                f"draft-recovery-{now:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex}.json"
            )
            try:
                # Unlike replace/rename, link fails when a destination exists.
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError("Could not choose an unused recovery filename")

        os.unlink(temporary)
        temporary = None
        directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return destination
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Recovery contains an invalid JSON number: {value}")


def read_recovery(path: str | os.PathLike[str], project: dict[str, Any]) -> dict[str, Any]:
    """Read a bounded recovery snapshot belonging to this recording folder.

    This checks the envelope, not the uncommitted editor values: those values
    deliberately may be invalid. Symlinks, special files, files outside the
    project, unsupported schemas, and mismatched saved project paths are refused.
    The returned dictionary contains ``project`` and ``draft`` snapshots; applying
    either to the live editor is a separate, explicit action for the caller.
    """
    folder = _project_folder(project)
    candidate = Path(path).expanduser().absolute()
    if (
        candidate.parent.resolve(strict=True) != folder
        or _RECOVERY_NAME.fullmatch(candidate.name) is None
    ):
        raise ValueError("Recovery file must belong to this recording folder")

    directory_fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        descriptor = os.open(
            candidate.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Recovery must be a regular file")
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Recovery metadata exceeds the 16 MiB size limit")
    document = json.loads(raw, parse_constant=_invalid_constant)
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or not isinstance(document.get("project"), dict)
        or not isinstance(document.get("draft"), dict)
        or not isinstance(document.get("created_at"), str)
    ):
        raise ValueError("Unsupported or malformed recovery snapshot")
    if document["project"].get("path") != str(folder):
        raise ValueError("Recovery snapshot belongs to a different recording folder")
    created = datetime.fromisoformat(document["created_at"])
    if created.tzinfo is None:
        raise ValueError("Recovery timestamp must include a time zone")
    return document


def list_recoveries(project: dict[str, Any]) -> list[Path]:
    """Return valid snapshots newest first, skipping unreadable or damaged files."""
    try:
        folder = _project_folder(project)
        candidates = list(folder.iterdir())
    except (ValueError, OSError):
        return []
    recoveries = []
    for candidate in candidates:
        if _RECOVERY_NAME.fullmatch(candidate.name) is None:
            continue
        try:
            document = read_recovery(candidate, project)
            created = datetime.fromisoformat(document["created_at"])
        except (ValueError, OSError, UnicodeError, RecursionError):
            continue
        recoveries.append((created, candidate.name, candidate))
    recoveries.sort(reverse=True)
    return [candidate for _, _, candidate in recoveries]
