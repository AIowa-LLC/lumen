# Lumen

A native screen recorder and presentation editor built for **Omarchy / Hyprland on Wayland**. Lumen uses GTK 4 and libadwaita for its interface, GPU Screen Recorder for capture, and FFmpeg for export. It is an early local application, not a benchmarked replacement for Recordly.

## Run

From this directory, inside your Hyprland session:

```sh
./scripts/lumen
```

Lumen opens a compact floating **recording HUD**. Drag its header to move it, choose **Record** to begin, or select **Open Studio** for the library, full capture settings, and editor. Opening Lumen again raises the current view; it does not start recording. Use the Studio header's **HUD** button to return to the compact controls.

The launcher uses the system Python so Arch's PyGObject and multimedia packages are available. No Python package download, virtual environment, browser runtime, or cloud account is needed.

To add Lumen to your application menu:

```sh
./scripts/install.sh
```

This copies the application to `~/.local/share/lumen`, installs `~/.local/bin/lumen`, and adds a desktop entry and icon. Run it again after updating this checkout. `XDG_DATA_HOME` and `LUMEN_INSTALL_BIN` can override the installation directories. The installer does not install system dependencies.

The same application can be controlled from a terminal or an existing Hyprland keybinding:

```sh
./scripts/lumen --hud          # Show the compact recording controls
./scripts/lumen --studio       # Show the full studio and editor
./scripts/lumen --record       # Start using the studio's capture settings
./scripts/lumen --pause        # Toggle pause/resume
./scripts/lumen --stop         # Save a recording, or discard an unsaved replay buffer
./scripts/lumen --replay       # Start a replay buffer; defaults to 30 seconds
./scripts/lumen --save-replay  # Save the recent buffer and keep buffering
./scripts/lumen --diagnostics  # Print local devices, tools, and capabilities as JSON
./scripts/lumen --help
```

Commands reach the running Lumen instance. `--record` and `--replay` open Lumen if needed; control commands report when no instance is running. Inside the application, use **Ctrl+R** to record, **Ctrl+Shift+R** to stop, **Ctrl+P** to pause, **Ctrl+Shift+S** to save a replay, **Ctrl+O** to import, and **Ctrl+E** to export. **Ctrl+Shift+H** opens the HUD, **Ctrl+Shift+O** opens Studio, and **Ctrl+Q** requests quit. These shortcuts work when Lumen has focus. The application installer does not change Hyprland keybindings.

For global controls when Lumen is hidden, the separate shortcut helper previews its changes before applying them:

```sh
./scripts/install-shortcuts.sh          # Inspect the proposed bindings
./scripts/install-shortcuts.sh --apply  # Back up, append, reload, and validate
```

It adds **Super+Alt+R** to record, **Super+Alt+Shift+R** to stop, **Super+Alt+P** to pause, and **Super+Alt+V** to save a replay. It refuses conflicts, keeps a backup of `bindings.lua`, and restores the original if validation fails. Super+Alt+S remains Omarchy's scratchpad shortcut.

## Capture and editing

The HUD shows the selected source, audio mode, elapsed time, and recording state. During a take, its controls become **Pause** / **Resume** and **Stop & save**; stopping opens the saved take in Studio. Its replay button starts buffering explicitly, then changes to a stop-buffer control while **Save replay** saves recent footage and keeps buffering.

The HUD has separate **Mic muted/on** and **Camera off/on** buttons. Before recording, they choose which inputs start with the take. During native recording or replay, they control the current session; a pending indicator stays visible until the change succeeds. Successful changes also become the choices for your next take. Muting the microphone leaves desktop audio enabled when selected.

Open the HUD's gear menu for **Capture**, **Display** or **Window**, **Audio**, **Microphone**, **Webcam**, **Countdown**, **Replay duration**, and **Hide controls during capture**. Device choices use saved identities, so unplugging a selected device never silently selects a different one. **More settings in Studio** opens the full Record page, including **Refresh devices** after connecting hardware. Both views share the same settings and active recording; switching views does not start or stop capture.

The webcam appears as a live bubble inside the recorded area and is included in the saved video. Turning it off closes Lumen's camera stream. Keep that bubble visible: another window covering it also covers it in the recording. Microphone muting is local to Lumen and releases Lumen's microphone stream; it does not change system mute, device volume, or other apps. Native sessions keep a silent microphone track ready so you can enable the mic after recording has begun.

Controls inside the captured area appear in the recording. Move the HUD outside that area or enable **Hide controls during capture**. Reopen Lumen or use `--hud` to show the controls again. Closing Studio returns to the HUD; the HUD's close button requests quit, with a guard for active recording, replay, export, or transcription. The HUD floats on the tested Omarchy desktop without permanent Hyprland configuration changes.

Unfinished layer fields never block returning to the HUD or quitting. Lumen saves valid edits and keeps an invalid draft separately in the recording folder; reopening that take offers **Restore draft**. If neither the project nor a recovery copy can be written, the quit dialog also offers **Quit without saving edits**. Cursor clicks have a separate **Duration** control (0.01–10 seconds), so moving a click's start time preserves its duration.

The full Studio's **Record** page includes:

- An actual display layout, monitor selection, a region picker, and a picker for visible window areas.
- 30, 60, or 120 fps; H.264, HEVC, or AV1; automatic, GPU-only, or CPU encoding; and three quality presets.
- Original resolution, 1920 × 1080, or 1280 × 720 capture, subject to backend support.
- Silent, desktop, microphone, or desktop plus microphone audio, with microphone device selection. The main backend stores desktop and microphone in separate tracks.
- An optional webcam overlay in the bottom-right corner, captured cursor visibility, and optional cursor-path telemetry.
- A 0, 3, or 5 second countdown, countdown cancellation, hide-on-record, elapsed time, pause/resume, and stop-and-save. Pause is available with GPU Screen Recorder.
- An explicit **Start replay buffer** control with 15, 30, or 60 second history, plus **Save replay** in the header while buffering.

Replay uses GPU Screen Recorder's compressed RAM buffer. Saving creates a normal editable project and buffering continues. Stopping discards the unsaved buffer; save a clip first to keep it. Replay never starts automatically and does not save cursor-path telemetry. At the default 30 seconds and “Very high” 18 Mbps setting, the compressed video payload is approximately 64 MiB; audio, encoder state, GPU memory, and application overhead are additional. This estimate is not a cap on total memory use.

The **Library** lists local recordings and can import MP4, MKV, WebM, MOV, and AVI files by copying the source into a new project. Original screen captures are saved as Matroska (`source.mkv`), alongside project metadata and a capture log.

The **Studio** includes source playback, project naming, trim in/out points (including marks at the playhead), 0.5× to 3× playback speed, gradient backgrounds, wallpapers or no frame, padding, a fixed or animated zoom with adjustable focus, and a cursor-based zoom suggestion. **Preview edits** renders a temporary MP4 draft with the actual trim, speed, zoom, frame, and audio choices at up to 960 pixels wide and 30 fps. **Show original** returns to the source. Render another preview after changing the recipe.

Under **Style & export → Composition**, choose **Aurora**, **Dusk**, or **Glacier** from the wallpaper gallery, or use **Add your own wallpaper** for a PNG, JPEG, or WebP image. Selecting a tile switches the background to **Wallpaper**. Images fill the area behind the recording with a centered crop; increase **Frame padding** to show more. Custom images are copied into the take's `wallpapers/` folder and remain available in its gallery after save/reopen, deleting the original image, or moving the complete take folder. Images must be at most 50 MiB and 40 megapixels; imports are normalized to a still JPEG with a maximum 3840-pixel edge. Wallpaper backgrounds work in edited previews, MP4, and GIF exports. **None** or zero padding hides the backdrop.

Enable **Zoom to clicks** under **Style & export → Follow the action**, or use the button under **Captions & layers → Cursor clicks**, to focus smoothly on enabled clicks. Adjust the zoom amount, hold, and transition times; nearby clicks pan the focus and extend the zoom. Editing a click's time or position changes the camera motion, while **Render click effects** independently controls the visible rings. Projects without click metadata need manually added clicks to use this feature.

Exports can mix audio tracks, keep desktop or microphone only, or mute audio, with adjustable volume. Choose MP4 or a looping GIF, and 1920, 1280, 960, or original output width. Exports show progress and can be cancelled. **Save edits** preserves the edit recipe; previewing and exporting also save it.

The Studio's **Captions & layers** inspector adds a timed layer timeline with editable **Captions**, **Annotations**, and **Cursor clicks**. Add, copy, delete, enable/disable, undo, and redo layers; set their source-time intervals or mark the current playhead. Caption tools include SRT/WebVTT import and export, font size/color, top/bottom placement, and an optional dark background. Annotations include text labels, arrows, outline boxes, and highlights, with position picking directly on the original video. Click rings have editable position, timing, button, size, and color. New captures can collect actual mouse clicks; effects can also be added manually to existing videos. Preview edits and final export render the layers.

**Generate captions** transcribes the selected desktop, microphone, or mixed audio locally when the optional speech runtime is available. It uses real word alignment to produce short editable cues and supports cancellation. This desktop's runtime was prepared from existing cached packages and a model without downloading either. See [captions and timed layers](docs/CAPTIONS.md) for setup and behavior.

The **System** page reports the capture tools, devices, session, and GPU capabilities. It can run checks without starting a recording.

The capture backend and export pipeline are separate. Capture can use the GPU while presentation exports use FFmpeg's software filters and H.264 encoder. Performance depends on the GPU, display resolution, encoder, audio sources, and selected effects; no performance advantage over Recordly has been established.

## Dependencies

Lumen targets Python 3.11 or newer and a running Hyprland Wayland session. The development desktop already has these packages:

| Component | Arch / Omarchy package or command |
| --- | --- |
| Native interface | `python`, `python-gobject`, `gtk4`, `libadwaita` |
| Video playback | `gstreamer`, `gst-plugins-base`, `gst-plugins-good`, `gst-libav` |
| Main capture backend | `gpu-screen-recorder` |
| Compatibility capture | `wf-recorder` |
| Encoding and metadata | `ffmpeg`, `ffprobe` (the desktop's `ffmpeg-obs` also provides these) |
| Region selection | `slurp` |
| Monitor and window discovery | `hyprctl` |
| Audio discovery and live microphone control | `pactl` with the desktop's PipeWire/PulseAudio compatibility service |
| Live webcam bubble | `mpv` with V4L2 input support |
| Optional automatic captions | A separate local faster-whisper runtime and model, or a configured whisper.cpp CLI/model |

There are no PyPI runtime dependencies. Installing the Python package alone cannot supply GTK, GStreamer, or capture binaries; use the source launcher or local installer on Omarchy.

## Files and privacy

The default library is `$(xdg-user-dir VIDEOS)/Lumen`, falling back to `~/Videos/Lumen`. Set `LUMEN_LIBRARY` to use another directory:

```sh
LUMEN_LIBRARY="$HOME/Videos/Product demos" ./scripts/lumen
```

Each recording has its own private directory:

```text
2026-10-02_12-30-00-a1b2c3d4/
  project.json
  source.mkv
  capture.log
```

Additional files may hold thumbnails, cursor telemetry, and exports. Project JSON writes are atomic, recording directory names are collision safe, and unreadable manifests do not prevent the rest of the library from opening. Moving a complete recording directory preserves its project metadata. User settings live in `${XDG_CONFIG_HOME:-~/.config}/lumen/settings.json`. Monitor, frame rate, codec, encoder, quality, audio mode, and cursor preferences persist after starting a recording; the other setup controls return to their defaults when Lumen restarts.

Processing stays on this computer. Cursor telemetry, when enabled, stores cursor positions used to suggest an editing zoom. Optional click capture records mouse-button events and positions while a normal recording is active; it does not record keystrokes. Captured audio and whatever is visible within the selected screen area become part of the recording. Automatic caption generation extracts only the selected audio tracks into a private temporary file and uses an offline local model.

## Current boundaries

- This release targets Hyprland. Other Wayland compositors and X11 have not been validated.
- Window mode records a fixed rectangle on the desktop. Moving, covering, or minimizing that window changes what is captured.
- The compatibility backend uses software H.264 at native resolution and supports one audio source. Live input toggles, webcam composition, hiding the captured cursor, and other codecs are unavailable there. A basic automatic fallback reports this limitation and keeps the HUD's input indicators accurate.
- Hardware codecs depend on the capture backend and GPU support. An explicit GPU request must fail visibly if that path cannot run.
- Edited preview is a rendered draft, not an immediate live filter. It uses lower resolution and encoding quality than final export. Original source playback uses the first audio track; edited preview and export can mix separate tracks.
- Replay requires GPU Screen Recorder and cannot run alongside a normal Lumen recording. A newly started buffer can save only the footage captured so far. Replay does not support pause or cursor-based zoom suggestions.
- Audio selection uses the project track mapping or imported track titles. Untagged imports use the first track for desktop and second for microphone; selecting a track that is absent produces silent output. Already mixed audio cannot be separated.
- The current Studio export controls cap MP4 at 60 fps and GIF at 24 fps, even when capture is set to 120 fps.
- Webcam discovery filters metadata and output-only devices. Availability and input formats still depend on the camera; a device already in use by another application can fail to open.
- Zoom suggestions are based on cursor activity, not semantic understanding of the application being recorded.
- Editable click rings are separate overlays. They cannot remove or change a cursor already embedded in a source recording. Replay does not collect click or cursor metadata; imported and replay clips can use manually added clicks for click-driven zoom.
- Automatic speech recognition needs review; names, accents, overlapping speech, and noise can produce errors. It does not identify speakers or translate audio. Subtitle imports preserve plain text and timing, not external fonts or positioning rules.
- There is no cloud sharing, collaboration service, or multi-clip video editing in this version.

## Development and checks

```sh
/usr/bin/python -m unittest discover -s tests -v
```

The project store tests cover unique recording folders, private file permissions, malformed manifests, moves, metadata preservation, atomic write failures, time-zone-aware ordering, and settings recovery. Capture, replay, UI-state, and FFmpeg export tests exercise the corresponding modules. The [verification record](docs/VERIFICATION.md) describes checks completed on this Omarchy desktop and their limits. Use the [manual QA checklist](docs/QA.md) for broader hardware and workflow coverage.

Two opt-in live scripts exercise the actual desktop: `scripts/verify_ui.py` records a short silent take of its own studio window and checks playback, edited preview, export, and replay; `scripts/verify_workflow.py` records a small region around the pointer with desktop audio and checks pause/resume, edit persistence, rendering, and source integrity. Both keep artifacts in private `/tmp/lumen-*` directories. Run them inside the Hyprland session only when the visible test content is suitable for capture.

`scripts/verify_hud.py` checks default HUD launch, floating behavior, navigation, shared settings, countdown cancellation, and guarded close while capturing a short silent take of its own Studio window. It uses an isolated application instance and temporary library.

`scripts/verify_inputs.py` records only its own generated pattern window, uses a private audio test tone and a generated camera image, and toggles both inputs through the HUD. It checks decoded sound and pixels, saved input choices, and cleanup. It never opens a physical microphone or camera, or plays the test tone through speakers.

For caption/layer authoring, `scripts/verify_layers.py` generates synthetic footage and exercises the GTK controls, edited preview, sidecars, and final export. It takes screenshots only of its own window and does not record the desktop. The optional `--speech-fixture /path/to/test-speech.wav` argument adds a local transcription check using an explicitly selected public or local test fixture; no fixture is downloaded automatically.

`scripts/verify_wallpapers.py` uses synthetic footage and an image fixture to check native wallpaper selection, custom import, saved recipes, edited-preview playback, and captioned export. Its file chooser callback receives the generated image, and screenshots include only its own window. Wallpaper artwork and generation prompts are documented in [docs/WALLPAPERS.md](docs/WALLPAPERS.md).

The implementation separates project storage, desktop discovery, capture, replay, caption/sidecar processing, offline speech workers, and FFmpeg layer rendering. The native interface authors timed layers independently of the source file. Media and capture subprocesses use explicit argument lists; the application owns and signals its own capture and transcription processes.

Licensed under the MIT License.
