#!/usr/bin/python
"""Prepare optional offline speech recognition from an existing uv cache.

This helper never installs into the system Python or downloads packages/models.
It copies compatible cached distributions into a private project-local runtime.
"""
from __future__ import annotations

import argparse
from email import message_from_string
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


PACKAGES = {
    "faster-whisper", "ctranslate2", "huggingface-hub", "tokenizers", "onnxruntime", "av",
    "tqdm", "numpy", "pyyaml", "flatbuffers", "packaging", "protobuf", "filelock", "fsspec",
    "hf-xet", "httpx", "typing-extensions", "click", "anyio", "certifi", "httpcore", "idna", "h11",
}


def version_key(value):
    return tuple(int(part) for part in re.findall(r"\d+", value))


def cached_distributions(cache, abi):
    found = {}
    for directory in cache.iterdir():
        for metadata in directory.glob("*.dist-info/METADATA"):
            information = message_from_string(metadata.read_text())
            name = information.get("Name", "").lower().replace("_", "-")
            if name not in PACKAGES:
                continue
            wheel = metadata.with_name("WHEEL")
            if not wheel.is_file():
                continue
            tags = message_from_string(wheel.read_text()).get_all("Tag", [])
            compatible = any(tag.startswith("py3-none-") or tag.startswith(abi + "-" ) or
                             ("-abi3-" in tag and int(tag.split("-", 1)[0][2:]) <= int(abi[2:])) for tag in tags)
            if not compatible:
                continue
            version = information.get("Version", "0")
            if name not in found or version_key(version) > version_key(found[name][0]):
                found[name] = (version, directory)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-cache", action="store_true", help="Explicitly select the offline mode (the only mode)")
    parser.add_argument("--python", type=Path, help="Existing CPython 3.11 interpreter with matching cached native wheels")
    parser.add_argument("--model", type=Path, help="Existing faster-whisper model directory")
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / ".speech")
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache/uv"))) / "archive-v0")
    args = parser.parse_args()
    destination = args.destination.expanduser().absolute()
    if destination.exists():
        raise RuntimeError(f"{destination} already exists. Keep it, or choose another --destination; no files changed")
    python = args.python
    if python is None:
        candidates = sorted((Path.home() / ".local/share/uv/python").glob("cpython-3.11.*-linux-*/bin/python3.11"))
        python = candidates[-1] if candidates else None
    if not python or not python.is_file():
        raise RuntimeError("No existing Python 3.11 runtime found. Supply --python from a compatible local environment")
    information = json.loads(subprocess.check_output([str(python), "-c", "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor}))"], text=True))
    if information != {"major": 3, "minor": 11}:
        raise RuntimeError("This offline setup helper currently supports cached CPython 3.11 wheels")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lumen.speech import _cached_model
    model = args.model.expanduser().resolve() if args.model else _cached_model()
    if not model or not all((model / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json")):
        raise RuntimeError("No complete cached base model found. Supply --model pointing to a local CTranslate2 Whisper model")
    if not args.cache.is_dir():
        raise RuntimeError(f"No uv package cache at {args.cache}")
    distributions = cached_distributions(args.cache, "cp311")
    missing = sorted(PACKAGES - distributions.keys())
    if missing:
        raise RuntimeError("Required compatible packages are not cached: " + ", ".join(missing))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".speech-build-", dir=destination.parent))
    try:
        packages = staging / "site-packages"
        packages.mkdir()
        for name, (version, source) in sorted(distributions.items()):
            print(f"Copying cached {name} {version}", flush=True)
            for entry in source.iterdir():
                if entry.name in ("__pycache__", ".git", ".gitignore"):
                    continue
                target = packages / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                elif entry.is_file():
                    shutil.copy2(entry, target)
        env = dict(os.environ, PYTHONPATH=str(packages), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_HUB_DISABLE_TELEMETRY="1", DO_NOT_TRACK="1")
        check = "from faster_whisper import WhisperModel; import sys; model=WhisperModel(sys.argv[1], device='cpu', compute_type='int8', cpu_threads=2, local_files_only=True); print('Offline speech model loaded successfully')"
        subprocess.run([str(python), "-c", check, str(model)], env=env, check=True, timeout=90)
        configuration = {"backend": "faster-whisper", "python": str(python.resolve()), "packages": "site-packages",
                         "model": str(model.resolve()), "versions": {name: value[0] for name, value in distributions.items()}}
        (staging / "runtime.json").write_text(json.dumps(configuration, indent=2) + "\n")
        os.rename(staging, destination)
        print(f"Ready: {destination}\nNo packages or models were downloaded. Restart Lumen to use automatic captions.")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Speech setup: {exc}", file=sys.stderr)
        raise SystemExit(1)
