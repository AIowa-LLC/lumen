"""JSON-lines bridge run by an optional isolated faster-whisper interpreter."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def segment_captions(segment, *, max_chars=42, max_seconds=3.0):
    """Group actual aligned words into readable cues without making up timing.

    A single oversized word stays intact. Missing or invalid alignment falls
    back to the model's original segment boundaries and text.
    """
    fallback = [{"start": segment.start, "end": segment.end, "text": segment.text.strip()}]
    words = getattr(segment, "words", None)
    if not words:
        return fallback
    parts = []
    try:
        for word in words:
            if not isinstance(word.word, str) or not word.word:
                return fallback
            start, end = float(word.start), float(word.end)
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                return fallback
            if parts and start < parts[-1]["start"]:
                return fallback
            parts.append({"start": start, "end": end, "text": word.word})
    except (AttributeError, TypeError, ValueError, OverflowError):
        return fallback
    chunks = []
    group = []
    def publish():
        if group:
            chunks.append({"start": group[0]["start"], "end": group[-1]["end"],
                           "text": "".join(part["text"] for part in group).strip()})
            group.clear()
    for part in parts:
        combined = "".join(p["text"] for p in group) + part["text"]
        if group and (len(combined.strip()) > max_chars or part["end"] - group[0]["start"] > max_seconds):
            publish()
        group.append(part)
        current = "".join(p["text"] for p in group).strip()
        # Prefer a natural punctuation boundary once enough text is on screen.
        if (len(current) >= 20 or part["end"] - group[0]["start"] >= 1.2) and re.search(r'''[,.!?;:。！？、]["'”’）)\]]*$''', current):
            publish()
    publish()
    if any(c["end"] <= c["start"] or not c["text"] for c in chunks):
        return fallback
    return chunks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--language")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    if not Path(args.model).is_dir():
        raise ValueError("A local model directory is required; automatic model downloads are disabled")
    emit({"type": "status", "text": "Loading the local speech model on CPU…"})
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=max(1, min(8, args.threads)),
                         num_workers=1, local_files_only=True)
    emit({"type": "status", "text": "Detecting speech and transcribing locally…"})
    segments, info = model.transcribe(args.audio, language=args.language, beam_size=5,
                                     vad_filter=True, condition_on_previous_text=False,
                                     word_timestamps=True)
    emit({"type": "status", "text": f"Transcribing {info.language} speech locally…"})
    for segment in segments:
        text = segment.text.strip()
        if text and segment.end > segment.start:
            for index, caption in enumerate(segment_captions(segment)):
                emit({"type": "caption", "caption": {"id": f"speech-{segment.id}-{index}", **caption}})
    emit({"type": "done"})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Local speech recognition failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
