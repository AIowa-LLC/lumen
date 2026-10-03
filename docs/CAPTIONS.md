# Captions and timed layers

Open a recording in Studio and select **Captions & layers**. All layer times refer to the original source recording. Trimming and playback speed automatically adjust rendered layers and exported subtitle files.

## Captions

Choose **Captions**, then **Add** to create a cue at the playhead. Edit its text and start/end times, or use **Start here** and **End here**. Use **Apply layer** to commit the change. The list and timeline let you select cues; copy, delete, enable/disable, undo, and redo remain available.

**Import SRT / VTT** appends UTF-8 subtitles, retaining Unicode and multiple text lines. Overlapping cues are allowed. Subtitle markup is converted to plain text; imported font, position, and WebVTT styling rules are not retained. Imported cues receive new IDs so another import cannot replace existing captions accidentally.

**Export SRT** and **Export VTT** clip captions to the current trim and adjust their times for playback speed. Disabled and fully trimmed-out cues are omitted. Choose a new destination filename; an existing file is preserved. Sidecar times are written to millisecond precision.

Caption size, color, top/bottom position, and dark background are controlled together for the project. **Render captions** controls whether captions appear in edited preview and final video output. Sidecar export remains a separate action.

## Local automatic captions

Choose **Mix all tracks**, **Desktop only**, or **Microphone only**, and an explicit language or auto-detection. **Generate captions** extracts the selected tracks into a private temporary mono WAV and starts a local speech process. Selecting a missing audio track fails explicitly; Lumen does not substitute another track.

Generated captions append to the project and can be undone as a group. The worker uses aligned word timestamps to group text into approximately 42-character or three-second cues, preferring punctuation boundaries. It retains an oversized word intact and uses original segment timing when alignment is unavailable. Progress advances with processed audio and returned speech timestamps; model loading is shown as a status rather than a fabricated percentage. **Cancel transcription** terminates the owned process and removes temporary audio.

Review the generated words and timing before sharing. Speech recognition can miss or mishear words, especially names, overlapping speech, accents, and noisy audio. There is no speaker diarization or translation in this version.

### Prepared offline runtime

This desktop already had compatible package archives, a Python 3.11 runtime, and a complete faster-whisper base model. The optional helper can reuse those files:

```sh
./scripts/setup-speech.py --from-cache
./scripts/install.sh
```

The helper copies cached dependencies into a private `.speech/` directory beside the application, verifies that the local model loads, and writes its runtime configuration. It never downloads packages or models, modifies the system Python, or overwrites an existing `.speech/` directory. Its default destination derives from the script location, not the current working directory. The application installer copies a prepared runtime when one exists.

The prepared dependencies occupy approximately 392 MiB; the existing model is read from the user's Hugging Face cache. Keep that model and its configured Python interpreter available. `.speech/` is excluded from version control. Manual captions, subtitle import, recording, and editing do not require these dependencies.

The configured faster-whisper worker uses CPU INT8, a local model-directory path, and offline-only loading. Those modes are supported by the [upstream faster-whisper documentation](https://github.com/SYSTRAN/faster-whisper#usage). Lumen additionally sets offline and telemetry-disable environment options for the child process.

The reused model is [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base), a CTranslate2 conversion of OpenAI's Whisper base model whose model card lists the MIT license. The copied runtime retains each distribution's `.dist-info` metadata and included license/notice files. Its `runtime.json` records package versions, the interpreter, and the model path; third-party dependencies keep their own licenses independently of Lumen's license.

### Use another existing runtime

An existing faster-whisper environment and complete CTranslate2-format model directory can be selected explicitly:

```sh
LUMEN_WHISPER_PYTHON=/path/to/environment/bin/python \
LUMEN_WHISPER_MODEL=/path/to/local/model-directory \
./scripts/lumen
```

The directory must include `model.bin`, `config.json`, and `tokenizer.json`. Model names that would trigger a download are not accepted. For a different machine without cached packages, prepare a separate environment following the upstream installation instructions and point Lumen at it.

Lumen also accepts an existing whisper.cpp CLI and ggml model file:

```sh
LUMEN_WHISPER_CLI=/path/to/whisper-cli \
LUMEN_WHISPER_MODEL=/path/to/ggml-base.bin \
./scripts/lumen
```

The adapter uses the CLI's local file/model inputs and SRT output flags, documented in the [upstream CLI implementation](https://github.com/ggml-org/whisper.cpp/blob/master/examples/cli/cli.cpp). This adapter is optional; the live transcription check on this desktop used faster-whisper.

## Annotations and cursor clicks

**Annotations** offers text labels, arrows, outline boxes, and highlights. Choose a color and size, enter normalized positions as percentages, or use **Place on video** and **Place endpoint**. Placement switches to the original source so coordinates remain stable while edit framing or zoom changes. Annotation and click geometry is rendered with the source before zoom/framing, while captions remain positioned on the final canvas.

**Cursor clicks** offers editable click rings with left/right/middle button labels, duration, position, color, and size. Normal recordings can save plain, unmodified left/right/middle clicks when the compositor capability is available, up to **10,000 clicks per take**. Modifier-key clicks are not collected. Collection does not record keystrokes or replace existing conflicting mouse bindings. Collected clicks are metadata, so timing and appearance can be changed later. You can add rings manually to imported videos too. A ring does not alter or remove the cursor already visible in a source video.

Layer visibility and timing apply to **Preview edits** and final exports. Render a new preview after edits; the previous draft is a snapshot of the earlier recipe.

### Zoom to clicks

In Studio, open **Style & export → Follow the action** and enable **Zoom to clicks**. Adjust **Click zoom amount**, **Hold after click · seconds**, and **Transition · seconds**. The **Zoom to clicks** button under **Cursor clicks** enables and saves the option, then opens these controls. It starts disabled; the defaults are **1.8×** zoom, **1.2 seconds** hold, and **0.35 seconds** transition. Manual zoom controls are disabled while following clicks.

Enabled clicks supply the camera's timing and focus position. Edit a click and choose **Apply layer** to change the resulting motion. Nearby clicks smoothly pan the focus and keep the zoom active instead of repeatedly zooming out and back in. Disabling an individual click removes it from the motion. **Render click effects** controls the visible rings independently, so you can follow clicks with no rings on the video.

Hold and transition values use seconds in the original source. Playback speed retimes them with the footage: at 2× speed, a 1.2-second hold lasts 0.6 seconds in the output. Preview and export use the current trim, speed, and click edits. Render a new **Preview edits** draft to see changes.

Without click metadata, there is nothing for the camera to follow. Add clicks manually for imported videos, replay clips, or recordings made without click collection. Click-driven zoom uses these events; it does not infer clicks from the image or an embedded cursor.
