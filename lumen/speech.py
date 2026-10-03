"""Optional, local-only subtitle generation isolated from GTK's Python runtime."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading

from ._process import spawn_owned


class TranscriptionError(RuntimeError):
    pass


class TranscriptionCancelled(TranscriptionError):
    pass


def _cached_model() -> Path | None:
    cache = Path(os.environ.get("HF_HUB_CACHE", str(Path.home() / ".cache/huggingface/hub")))
    model = cache / "models--Systran--faster-whisper-base"
    try:
        revision = (model / "refs/main").read_text().strip()
    except OSError:
        return None
    if not re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
        return None
    snapshot = model / "snapshots" / revision
    return snapshot if all((snapshot / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")) else None


def discover_transcriber() -> dict:
    """Describe an explicitly configured or prepared offline speech runtime.

    No subprocess, model download, or audio access occurs during discovery.
    LUMEN_WHISPER_PYTHON can point to an existing faster-whisper environment.
    LUMEN_WHISPER_CLI + a ggml model file select whisper.cpp instead.
    """
    root = Path(__file__).resolve().parent.parent
    runtime = root / ".speech/runtime.json"
    config = {}
    if runtime.is_file():
        try:
            config = json.loads(runtime.read_text())
            if not isinstance(config, dict):
                config = {}
        except (OSError, ValueError):
            config = {}
    python = os.environ.get("LUMEN_WHISPER_PYTHON") or config.get("python")
    configured_model = os.environ.get("LUMEN_WHISPER_MODEL") or config.get("model")
    model = Path(configured_model).expanduser() if isinstance(configured_model, str) else _cached_model()
    explicit_cli = os.environ.get("LUMEN_WHISPER_CLI")
    if explicit_cli:
        executable = shutil.which(explicit_cli)
        if executable and model and model.is_file():
            return {"available": True, "backend": "whisper.cpp", "binary": executable, "model": str(model.resolve())}
        return {"available": False, "reason": "Whisper.cpp needs an executable LUMEN_WHISPER_CLI and an existing ggml model file in LUMEN_WHISPER_MODEL."}
    if python:
        executable = shutil.which(str(python))
        if not executable:
            return {"available": False, "reason": "The configured speech Python is missing. Run scripts/setup-speech.py again."}
        if not model or not all((model / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")):
            return {"available": False, "reason": "A complete local faster-whisper model is missing. Set LUMEN_WHISPER_MODEL to its folder."}
        packages = None
        if not os.environ.get("LUMEN_WHISPER_PYTHON") and config.get("packages"):
            if not isinstance(config["packages"], str):
                return {"available": False, "reason": "The optional speech runtime configuration is malformed."}
            packages = (runtime.parent / config["packages"]).resolve()
            if not (packages / "faster_whisper").is_dir():
                return {"available": False, "reason": "The optional speech packages are missing. Run scripts/setup-speech.py again."}
        return {"available": True, "backend": "faster-whisper", "python": executable,
                "model": str(model.resolve()), "packages": str(packages) if packages else None,
                "device": "cpu", "compute_type": "int8"}
    cli = os.environ.get("LUMEN_WHISPER_CLI") or shutil.which("whisper-cli")
    if cli and model and model.is_file():
        executable = shutil.which(str(cli))
        if executable:
            return {"available": True, "backend": "whisper.cpp", "binary": executable, "model": str(model.resolve())}
    return {"available": False, "reason": "Automatic captions need an optional local speech runtime. Run scripts/setup-speech.py to reuse cached packages and a model offline, or configure LUMEN_WHISPER_PYTHON and LUMEN_WHISPER_MODEL. Manual captions and subtitle import remain available."}


def build_audio_command(project: dict, destination: Path, audio_mode: str) -> tuple[list[str], float]:
    """Extract only the selected source tracks, retaining source timeline origin."""
    from .editor import _audio_indices, probe
    if audio_mode not in {"mix", "desktop", "mic"}:
        raise TranscriptionError("Choose desktop, microphone, or mixed audio for transcription")
    source = Path(project.get("source", "source.mkv"))
    if not source.is_absolute():
        source = Path(project["path"]) / source
    media = probe(source)
    indices = _audio_indices(project, media, audio_mode)
    if not indices:
        raise TranscriptionError(f"This recording has no {audio_mode} audio track to transcribe")
    graph = []
    for index, track in enumerate(indices):
        graph.append(f"[0:a:{track}]aresample=16000:async=1:first_pts=0[a{index}]")
    if len(indices) > 1:
        graph.append("".join(f"[a{i}]" for i in range(len(indices))) +
                     f"amix=inputs={len(indices)}:duration=longest:normalize=1[out]")
        mapping = "[out]"
    else:
        mapping = "[a0]"
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
               "-filter_complex", ";".join(graph), "-map", mapping, "-vn", "-ac", "1", "-ar", "16000",
               "-c:a", "pcm_s16le", "-progress", "pipe:1", "-nostats", str(destination)]
    return command, media["duration"]


class Transcriber:
    """Run on a worker thread; cancel() returns promptly on the GUI thread."""

    def __init__(self, runtime: dict | None = None):
        self.runtime = runtime
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._active = False

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                return
            def ensure_exit():
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            threading.Thread(target=ensure_exit, daemon=True).start()

    def _check_cancelled(self):
        if self._cancelled.is_set():
            raise TranscriptionCancelled("Caption generation cancelled")

    def _run(self, command: list[str], *, env: dict | None = None, on_line=None) -> None:
        self._check_cancelled()
        with tempfile.TemporaryFile() as errors:
            with self._lock:
                self._check_cancelled()
                process = spawn_owned(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                      stderr=errors, text=True, encoding="utf-8", errors="replace",
                                      env=env, start_new_session=True)
                self._process = process
            try:
                for line in process.stdout:
                    self._check_cancelled()
                    if on_line:
                        on_line(line.rstrip("\r\n"))
                code = process.wait()
                self._check_cancelled()
                if code:
                    errors.seek(0, 2)
                    errors.seek(max(0, errors.tell() - 4000))
                    details = errors.read().decode("utf-8", errors="replace").strip()
                    raise TranscriptionError(details or f"{Path(command[0]).name} exited with status {code}")
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
                with self._lock:
                    self._process = None

    def transcribe(self, project: dict, *, audio_mode: str = "mix", language: str | None = None,
                   on_progress=None, on_status=None) -> list[dict]:
        from .captions import normalize_captions, load_captions
        with self._lock:
            if self._active:
                raise TranscriptionError("Caption generation is already running")
            self._active = True
        try:
            self._check_cancelled()
            runtime = self.runtime or discover_transcriber()
            if not runtime.get("available"):
                raise TranscriptionError(runtime.get("reason", "No local speech runtime is available"))
            if language in ("", "auto"):
                language = None
            if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
                raise TranscriptionError("Language must be a lowercase language code such as en, es, or auto")
            def status(value):
                if on_status:
                    on_status(value)
            def progress(value):
                if on_progress:
                    on_progress(max(0., min(1., value)))
            progress(0)
            status(f"Preparing {audio_mode} audio locally…")
            with tempfile.TemporaryDirectory(prefix="lumen-transcribe-") as directory:
                folder = Path(directory)
                audio = folder / "selected-audio.wav"
                command, duration = build_audio_command(project, audio, audio_mode)
                def extracted(line):
                    if line.startswith("out_time_us=") and duration > 0:
                        try:
                            progress(min(.1, int(line.split("=", 1)[1]) / 1_000_000 / duration * .1))
                        except ValueError:
                            pass
                self._run(command, on_line=extracted)
                self._check_cancelled()
                env = dict(os.environ, HF_HUB_OFFLINE="1", HF_DATASETS_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                           HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1", TOKENIZERS_PARALLELISM="false")
                captions = []
                if runtime["backend"] == "faster-whisper":
                    if runtime.get("packages"):
                        env["PYTHONPATH"] = runtime["packages"]
                    worker = Path(__file__).with_name("speech_worker.py")
                    command = [runtime["python"], "-u", str(worker), "--model", runtime["model"],
                               "--audio", str(audio), "--threads", str(min(4, os.cpu_count() or 1))]
                    if language:
                        command += ["--language", language]
                    def decoded(line):
                        try:
                            event = json.loads(line)
                        except ValueError:
                            return
                        if event.get("type") == "status":
                            status(str(event["text"]))
                        elif event.get("type") == "caption":
                            captions.append(event["caption"])
                            if duration > 0:
                                progress(.1 + .9 * min(1, event["caption"]["end"] / duration))
                    self._run(command, env=env, on_line=decoded)
                elif runtime["backend"] == "whisper.cpp":
                    status("Transcribing with the local Whisper model…")
                    prefix = folder / "transcript"
                    command = [runtime["binary"], "-m", runtime["model"], "-f", str(audio), "-l", language or "auto",
                               "-t", str(min(4, os.cpu_count() or 1)), "-osrt", "-of", str(prefix), "-np"]
                    self._run(command, env=env)
                    captions = load_captions(prefix.with_suffix(".srt"))
                else:
                    raise TranscriptionError("Unknown speech backend")
                self._check_cancelled()
                result = normalize_captions(captions)
                progress(1)
                status(f"Generated {len(result)} captions locally. Review the text and timing." if result else "No speech was detected in the selected audio.")
                return result
        except (OSError, ValueError) as exc:
            raise TranscriptionError(str(exc)) from exc
        finally:
            with self._lock:
                self._active = False
