"""Agent operations on the native application's GTK thread and workers.

No MCP dependency belongs here. The app is the sole owner of capture and editor
state; the stdio server reaches it through GApplication's same-user IPC.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import threading
import uuid

from . import __version__
from .capture import CaptureOptions
from .editor import ExportCancelled, ExportOptions, _validate, probe, thumbnail
from .overlays import validate_project_overlays
from .project import create_project, library_root, list_projects, load_project, save_project
from .wallpapers import BUILTIN_WALLPAPERS, custom_wallpapers, import_wallpaper, wallpaper_path

MAX_REQUEST_BYTES = 64 * 1024
OPERATIONS = {
    "status", "sources", "projects", "project", "open_project", "import_video",
    "update_edits", "set_layers", "wallpapers", "add_wallpaper", "start_recording",
    "stop_recording", "set_paused", "start_replay", "save_replay", "render",
    "frame", "get_job", "cancel_job",
}


def summary(project):
    return {
        "id": Path(project["path"]).name,
        **{key: project.get(key) for key in ("name", "status", "duration", "width", "height", "fps", "created_at")},
    }


def edit_options(project, changes=None):
    """Use editor validation plus strict JSON types that the native form accepts."""
    defaults = asdict(ExportOptions())
    saved = project.get("edits", {})
    if not isinstance(saved, dict):
        saved = {}
    changes = changes or {}
    if not isinstance(changes, dict) or changes.keys() - defaults.keys():
        raise ValueError("Unknown edit option. Use the documented edit recipe fields.")
    for key, value in changes.items():
        default = defaults[key]
        if value is None and key in {"trim_end", "zoom_start", "zoom_end", "output_width", "fps"}:
            continue
        if isinstance(default, bool):
            valid = isinstance(value, bool)
        elif isinstance(default, str):
            valid = isinstance(value, str)
        elif isinstance(default, int) or key == "fps":
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        if not valid:
            raise ValueError(f"Invalid type for edit option {key}.")
    options = ExportOptions(**{**defaults, **{k: v for k, v in saved.items() if k in defaults}, **changes})
    _validate(options, {"duration": project["duration"]})
    if options.background == "wallpaper" and options.padding:
        wallpaper_path(project, options.wallpaper)
    return options


class AgentController:
    def __init__(self, studio):
        self.studio = studio
        self.jobs = {}
        self.lock = threading.RLock()

    def request(self, raw):
        if len(raw.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Agent request exceeds 64 KiB.")
        request = json.loads(raw)
        if not isinstance(request, dict) or set(request) - {"operation", "arguments", "library"}:
            raise ValueError("Invalid agent request.")
        if request.get("library") != str(library_root()):
            raise ValueError("The running Lumen app uses a different library. Connect using that library or close the idle app first.")
        operation = request.get("operation")
        if operation not in OPERATIONS:
            raise ValueError("Unknown agent operation.")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("Agent arguments must be an object.")
        method = getattr(self, operation)
        inspect.signature(method).bind(**arguments)
        return method(**arguments)

    def _load(self, project_id, *, sync=False):
        if not isinstance(project_id, str) or not project_id or project_id in {".", ".."} or Path(project_id).name != project_id:
            raise ValueError("Use a project ID from lumen_list_projects, not a path.")
        root = library_root()
        folder = (root / project_id).resolve()
        if folder.parent != root:
            raise ValueError("Project must be inside the Lumen library.")
        studio = self.studio
        if sync and getattr(studio, "opening_project", None) == str(folder):
            raise ValueError("Wait for this take to finish opening in Studio before changing it.")
        if sync and studio.project and Path(studio.project["path"]).resolve() == folder:
            if studio.export_busy or studio.layers.transcribing:
                raise ValueError("Wait for the open take's render or transcription before changing it.")
            studio._save_edits()  # Reject invalid pending fields; never discard them.
        project = load_project(folder)
        source = (folder / project["source"]).resolve()
        if not source.is_relative_to(folder):
            raise ValueError("This project's source must be inside its recording folder.")
        if project["status"] not in {"ready", "imported"}:
            raise ValueError("This take is not ready for editing. Finish recording first.")
        return project

    def _saved(self, project):
        save_project(project)
        studio = self.studio
        if studio.project and studio.project["path"] == project["path"]:
            studio.project.update(deepcopy(project))
            studio.project_name.set_text(project["name"])
            studio.load_edits(project.get("edits", {}), project["duration"])
            studio.layers.load(project)
            studio.show_original()
        return summary(project)

    def _job(self, kind, work, *, done=None, failed=None, cancel=None):
        with self.lock:
            if len(self.jobs) >= 100:
                for identifier, job in list(self.jobs.items()):
                    if job["state"] != "running":
                        del self.jobs[identifier]
                        break
                else:
                    raise ValueError("Too many active jobs.")
            identifier = uuid.uuid4().hex
            job = {"id": identifier, "kind": kind, "state": "running", "progress": 0.0,
                   "created_at": datetime.now(timezone.utc).isoformat(), "_cancel": cancel}
            self.jobs[identifier] = job

        def finish(value):
            try:
                result = done(value) if done else value
                with self.lock:
                    job.update(state="succeeded", progress=1.0, result=result)
            except Exception as exc:
                fail(exc)

        def fail(exc):
            try:
                if failed:
                    failed(exc)
            finally:
                with self.lock:
                    job.update(state="cancelled" if isinstance(exc, ExportCancelled) else "failed", error=str(exc))

        self.studio.worker(work, finish, fail)
        return self.get_job(identifier)

    def status(self):
        s = self.studio
        opening = getattr(s, "opening_project", None)
        return {"version": __version__, "pid": os.getpid(), "library": str(library_root()), "view": s.get_application().view,
                "opening_project_id": Path(opening).name if isinstance(opening, str) else None,
                "devices_ready": s.devices_ready, "capture_busy": s.capture_busy,
                "recording": {"active": s.recorder.is_running, "state": s.recorder.status,
                              "elapsed": s.recorder.elapsed, "project_id": Path(s.recorder.project["path"]).name if s.recorder.project else None},
                "replay": {"active": s.replay.is_running, "state": s.replay.status, "elapsed": s.replay.elapsed},
                "export_busy": s.export_busy, "project": summary(s.project) if s.project else None,
                "jobs": [self.get_job(j)["id"] for j in self.jobs if self.jobs[j]["state"] == "running"]}

    def sources(self, refresh=False):
        s = self.studio
        if not isinstance(refresh, bool):
            raise ValueError("refresh must be a boolean.")
        if refresh:
            s.devices_ready = False
            s.refresh_devices()
        return deepcopy({"ready": s.devices_ready, "monitors": s.monitors, "windows": s.windows,
                         "microphones": s.microphones, "cameras": s.camera_devices})

    def projects(self, limit=50, offset=0):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Use limit 1–100 and a nonnegative offset.")
        projects = list_projects()
        return {"projects": [summary(p) for p in projects[offset:offset+limit]], "total": len(projects), "next_offset": offset+limit if offset+limit < len(projects) else None}

    def project(self, project_id):
        return self._load(project_id)

    def open_project(self, project_id):
        if self.studio.export_busy or self.studio.layers.transcribing:
            raise ValueError("Finish or cancel the render/transcription before opening another take.")
        if self.studio.project:
            self.studio._save_edits()
        project = self._load(project_id, sync=True)
        self.studio.open_project(project)
        self.studio.get_application().show_studio("editor")
        return {"opening": project_id}

    def import_video(self, source, name=None):
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise ValueError("Video file not found.")
        if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > 200):
            raise ValueError("Project name must have 1–200 characters.")

        def work():
            media = probe(source)
            project = create_project()
            project.update(name=name or source.stem, source="source" + source.suffix.lower(), status="ready")
            shutil.copy2(source, Path(project["path"]) / project["source"])
            project.update({key: media[key] for key in ("duration", "width", "height", "fps")})
            save_project(project)
            return summary(project)

        return self._job("import", work)

    def update_edits(self, project_id, changes, name=None):
        project = self._load(project_id, sync=True)
        options = edit_options(project, changes)
        # Native controls must round-trip the persistent recipe exactly.
        if options.speed not in {0.5, 0.75, 1, 1.5, 2, 3} or options.output_width not in {1920, 1280, 960, None} or options.padding > 300:
            raise ValueError("Use Studio's speed/output-size choices and padding at most 300 pixels.")
        if name is not None:
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ValueError("Project name must have 1–200 characters.")
            project["name"] = name.strip()
        project["edits"] = {**project.get("edits", {}), **asdict(options)}
        self._saved(project)
        return {"project": summary(project), "edits": project["edits"]}

    def set_layers(self, project_id, kind, items, append=True, caption_style=None):
        if kind not in {"captions", "annotations", "clicks"} or not isinstance(items, list) or not isinstance(append, bool):
            raise ValueError("Choose captions, annotations, or clicks with a list of layers.")
        project = self._load(project_id, sync=True)
        incoming = []
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each layer must be an object.")
            incoming.append({"id": uuid.uuid4().hex, **item})
        project[kind] = (project.get(kind, []) if append else []) + incoming
        if caption_style is not None:
            if kind != "captions" or not isinstance(caption_style, dict):
                raise ValueError("Caption style must be an object supplied with captions.")
            project["caption_style"] = caption_style
        validate_project_overlays(project, project["duration"])
        self._saved(project)
        return {"project_id": project_id, "kind": kind, "layers": project[kind]}

    def wallpapers(self, project_id=None):
        result = {"builtins": [{"id": "builtin:" + key, "name": name} for key, name in BUILTIN_WALLPAPERS.items()]}
        if project_id:
            result["custom"] = custom_wallpapers(self._load(project_id))
        return result

    def add_wallpaper(self, project_id, source):
        project = self._load(project_id, sync=True)

        def done(item):
            latest = self._load(project_id, sync=True)
            gallery = custom_wallpapers(latest)
            if item["path"] not in {p["path"] for p in gallery}:
                gallery.append(item)
            latest["wallpapers"] = gallery
            self._saved(latest)
            return item

        return self._job("wallpaper", lambda: import_wallpaper(project, source), done=done)

    def _capture_ready(self):
        s = self.studio
        if s.shutting_down or s.capture_busy:
            raise ValueError("A capture transition is already in progress.")
        if not s.devices_ready:
            raise ValueError("Device discovery is still running. Retry shortly.")

    def _start(self, options, seconds=None, hide_controls=True):
        self._capture_ready()
        s = self.studio
        if s.recorder.is_running or s.replay.is_running:
            raise ValueError("Recording or replay is already active. Stop it first.")
        if not isinstance(options, dict) or options.keys() - {f.name for f in fields(CaptureOptions)}:
            raise ValueError("Unknown capture option.")
        options = CaptureOptions(**{"live_inputs": True, **options})
        options.validate()
        if options.mode in {"region", "window"} and not options.geometry:
            raise ValueError("Agent capture requires explicit geometry for a region/window; list sources first.")
        if not isinstance(hide_controls, bool):
            raise ValueError("hide_controls must be a boolean.")
        s.get_application().show_hud()
        s.capture_busy = True
        if hide_controls:
            s.get_application().visible_window().set_visible(False)

        def done(project):
            if seconds is None:
                s.capture_started(project)
                return summary(project)
            s.replay_started(None)
            return {"buffering": True, "seconds": seconds}

        def failed(exc):
            s.capture_busy = False
            s.reset_capture_controls()
            s.get_application().show_hud()
            s.toast(str(exc))

        return self._job("replay-start" if seconds else "record-start", lambda: s.start_backend(options, seconds), done=done, failed=failed)

    def start_recording(self, options=None, hide_controls=True):
        return self._start(options or {}, hide_controls=hide_controls)

    def start_replay(self, options=None, seconds=30, hide_controls=True):
        if isinstance(seconds, bool) or seconds not in {15, 30, 60}:
            raise ValueError("Replay duration must be 15, 30, or 60 seconds.")
        return self._start(options or {}, seconds, hide_controls)

    def stop_recording(self):
        self._capture_ready()
        s = self.studio
        backend = s.recorder if s.recorder.is_running else s.replay if s.replay.is_running else None
        if backend is None:
            raise ValueError("No recording or replay buffer is active.")
        s.capture_busy = True

        def reset(_):
            s.capture_busy = False
            s.reset_capture_controls()
            s.get_application().show_hud()

        def done(project):
            reset(None)
            if project:
                return summary(project)
            return {"buffer_stopped": True}

        return self._job("capture-stop", backend.stop, done=done, failed=reset)

    def set_paused(self, paused):
        self._capture_ready()
        s = self.studio
        if not isinstance(paused, bool) or not s.recorder.is_running:
            raise ValueError("Pause/resume requires an active recording and a boolean paused value.")
        if (s.recorder.status == "paused") == paused:
            return {"paused": paused}
        s.capture_busy = True

        def reset(_):
            s.capture_busy = False

        def done(_):
            reset(None)
            s.pause_button.set_label("Resume" if paused else "Pause")
            s.session_status.set_text("Paused" if paused else "Recording")
            return {"paused": paused}

        return self._job("pause" if paused else "resume", s.recorder.pause if paused else s.recorder.resume, done=done, failed=reset)

    def save_replay(self):
        self._capture_ready()
        s = self.studio
        if not s.replay.is_running:
            raise ValueError("No replay buffer is running.")
        s.capture_busy = True

        def reset(_):
            s.capture_busy = False

        def done(project):
            reset(None)
            return summary(project)

        return self._job("replay-save", s.replay.save, done=done, failed=reset)

    def render(self, project_id, preview=False, filename=None):
        s = self.studio
        if s.export_busy:
            raise ValueError("An export is already running. Wait or cancel it first.")
        project = self._load(project_id, sync=True)
        options = edit_options(project)
        if not isinstance(preview, bool):
            raise ValueError("preview must be a boolean.")
        if preview:
            requested = options.output_width or project["width"]
            width = min(960, requested)
            options = replace(options, output_width=width, padding=round(options.padding * width / requested), format="mp4", fps=30, quality=27)
        if filename is None:
            filename = f"{'preview' if preview else 'export'}-{uuid.uuid4().hex[:12]}.{options.format}"
        if not isinstance(filename, str) or Path(filename).name != filename or Path(filename).suffix != "." + options.format:
            raise ValueError("Use a new filename with the selected .mp4 or .gif extension, without directory components.")
        folder = Path(project["path"]) / ("previews" if preview else "exports")
        destination = folder / filename
        if not folder.resolve().is_relative_to(Path(project["path"]).resolve()):
            raise ValueError("Output folder must remain inside the recording folder.")
        identifier = uuid.uuid4().hex
        # render_export retains the existing UI's progress, cancellation, player,
        # source protection, and atomic output publication.
        with self.lock:
            if len(self.jobs) >= 100:
                completed = next((key for key, value in self.jobs.items() if value["state"] != "running"), None)
                if completed is None:
                    raise ValueError("Too many active jobs.")
                del self.jobs[completed]
            self.jobs[identifier] = {"id": identifier, "kind": "preview" if preview else "export", "state": "running", "progress": 0.0,
                                    "created_at": datetime.now(timezone.utc).isoformat(), "_cancel": s.cancel_export}

        def finished(path, error=None):
            with self.lock:
                job = self.jobs[identifier]
                if error:
                    job.update(state="cancelled" if isinstance(error, ExportCancelled) else "failed", error=str(error))
                else:
                    job.update(state="succeeded", progress=1.0, result={"path": str(path), "uri": Path(path).as_uri(), "project_id": project_id})

        try:
            s.render_export(options, destination, project, preview, completed=finished)
        except Exception as exc:
            finished(None, exc)
        return self.get_job(identifier)

    def frame(self, project_id, seconds=0, width=960):
        project = self._load(project_id)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 <= seconds < project["duration"]:
            raise ValueError("Frame time must be within the original recording.")
        if isinstance(width, bool) or not isinstance(width, int) or not 128 <= width <= 1920:
            raise ValueError("Frame width must be 128–1920 pixels.")
        destination = Path(project["path"]) / "previews" / (uuid.uuid4().hex + ".jpg")
        if not destination.parent.resolve().is_relative_to(Path(project["path"]).resolve()):
            raise ValueError("Preview folder must remain inside the recording folder.")
        return self._job("frame", lambda: str(thumbnail(Path(project["path"]) / project["source"], destination, time=seconds, width=width)))

    def get_job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise ValueError("Unknown or expired job ID. Jobs belong to the running app session.")
            job = self.jobs[job_id]
            result = deepcopy({key: value for key, value in job.items() if not key.startswith("_")})
            if job["kind"] in {"preview", "export"} and job["state"] == "running":
                result["progress"] = self.studio.export_progress.get_fraction()
            result["cancellable"] = job["state"] == "running" and job.get("_cancel") is not None
            return result

    def cancel_job(self, job_id):
        with self.lock:
            self.get_job(job_id)
            job = self.jobs[job_id]
            if job["state"] != "running":
                return self.get_job(job_id)
            if job.get("_cancel") is None:
                raise ValueError("This operation cannot be cancelled. Stop capture using lumen_stop_recording.")
            job["_cancel"]()
            job["cancel_requested"] = True
            return self.get_job(job_id)
