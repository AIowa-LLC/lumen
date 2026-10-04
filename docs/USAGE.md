# Using Lumen

[Back to the README](../README.md) · [Captions & layers](CAPTIONS.md) · [Manual QA](QA.md)

## HUD and Studio

Lumen opens a compact floating **recording HUD**. Drag its header to move it. The HUD shows the source, audio mode, elapsed time, and recording state. **Record** starts a take; during recording the controls become **Pause** / **Resume** and **Stop & save**. Stopping opens the saved take in Studio.

Choose **Open Studio** for the library, full capture settings, and editor. The Studio header's **HUD** button returns to the compact controls. Both views share settings and the active recording. Switching views never starts or stops capture; opening Lumen again raises its current view.

The HUD's gear menu includes **Capture**, **Display** or **Window**, **Audio**, **Microphone**, **Webcam**, **Countdown**, **Replay duration**, and **Hide controls during capture**. **More settings in Studio** opens the Record page, where **Refresh devices** updates discovery after connecting hardware. Saved device identities prevent a disconnected selection from silently switching to another device.

Controls inside the capture area appear in the video. Move the HUD outside it or enable **Hide controls during capture**. Reopen Lumen or use `--hud` to bring the controls back. Closing Studio returns to the HUD. Closing the HUD or requesting quit shows a guard when recording, replay, export, or transcription is active. The HUD floats on the tested Omarchy desktop without permanent Hyprland configuration changes.

## Commands and shortcuts

Run these from the checkout, or replace `./scripts/lumen` with your installed launcher:

```sh
./scripts/lumen --hud          # Show compact recording controls
./scripts/lumen --studio       # Show the full studio and editor
./scripts/lumen --record       # Start using the current/saved capture settings
./scripts/lumen --pause        # Toggle pause/resume for a normal recording
./scripts/lumen --stop         # Save a recording, or discard unsaved replay history
./scripts/lumen --replay       # Start a replay buffer; defaults to 30 seconds
./scripts/lumen --save-replay  # Save recent footage and keep buffering
./scripts/lumen --diagnostics  # Print local devices, tools, and capabilities as JSON
./scripts/lumen --help
```

Commands reach the running Lumen instance. `--record` and `--replay` open Lumen if needed; control commands report when no instance is running.

These shortcuts work while Lumen has focus:

| Action | Shortcut |
| --- | --- |
| Record | **Ctrl+R** |
| Stop | **Ctrl+Shift+R** |
| Pause / resume | **Ctrl+P** |
| Save replay | **Ctrl+Shift+S** |
| Import | **Ctrl+O** |
| Export | **Ctrl+E** |
| Open HUD | **Ctrl+Shift+H** |
| Open Studio | **Ctrl+Shift+O** |
| Request quit | **Ctrl+Q** |

For global controls while Lumen is hidden, install the application first, then preview the optional shortcut helper:

```sh
./scripts/install-shortcuts.sh
./scripts/install-shortcuts.sh --apply
```

The helper targets `${XDG_CONFIG_HOME:-$HOME/.config}/hypr/bindings.lua` and checks that `hyprland.lua` loads `hypr.bindings`. It adds **Super+Alt+R** (record), **Super+Alt+Shift+R** (stop), **Super+Alt+P** (pause/resume), and **Super+Alt+V** (save replay). **Super+Alt+S** remains Omarchy's scratchpad shortcut.

It previews by default, refuses conflicts, backs up the bindings before applying, reloads Hyprland, and validates the result. If validation fails, it restores its own changes unless another edit arrived in the meantime; in that case it leaves the file untouched and reports the backup. It will not rewrite an unfamiliar configuration. Existing Hyprland bindings can also call the same CLI flags directly.

## Capture settings

Studio's **Record** page offers:

- A display layout, monitor selection, region picker, and picker for visible window areas.
- **30, 60, or 120 fps**, **H.264, HEVC, or AV1**, automatic/GPU-only/CPU encoding, and High/Very high/Ultra quality.
- Original resolution, **1920 × 1080**, or **1280 × 720**, subject to backend support.
- Silent, desktop, microphone, or desktop-plus-microphone audio with microphone selection. The main backend keeps desktop and microphone in separate tracks.
- Optional webcam overlay, captured cursor visibility, cursor-path telemetry, and actual click collection where supported.
- A **0-, 3-, or 5-second countdown**, cancellation, hide-on-record, elapsed time, and stop-and-save. GPU Screen Recorder supports pause/resume for normal recording.

Window mode records a fixed rectangle on the desktop. Moving, covering, or minimizing the selected window changes what is captured. GPU codec support depends on the backend, driver, and hardware. An explicit GPU-only request must fail visibly if that path cannot run.

The compatibility backend uses software H.264 at native resolution with one audio source. Native live input toggles, webcam composition, hiding the captured cursor, and other codecs are unavailable. Automatic fallback reports its limitations and updates the HUD's input indicators accordingly.

### Live microphone and camera controls

The HUD's **Mic muted/on** and **Camera off/on** buttons choose which inputs start with the next take. During native recording or replay they control the current session, with a pending indicator until the change succeeds. Successful changes also become the choices for the next take.

Muting is local to Lumen: it releases Lumen's microphone stream without changing system mute, device volume, or another application's input. Desktop audio remains enabled when selected. Native sessions keep a silent microphone track ready so the mic can be enabled after capture begins.

The webcam appears as a live bubble in the bottom-right of the recorded area. Keep it visible: another window covering it also covers it in the saved video. Turning it off closes Lumen's camera stream. Camera discovery filters metadata and output-only devices, but usable formats depend on the camera. A camera already in use by another application can fail to open.

## Replay buffer

Choose **Start replay buffer** on the Record page, the HUD replay button, or `--replay`. Select **15, 30, or 60 seconds** of history. Replay starts only when requested and cannot run alongside a normal Lumen recording.

**Save replay** creates a normal editable project and keeps buffering. The HUD replay button becomes a stop-buffer control while buffering. **Stop** discards unsaved history, so save a clip first if you want to keep it. A newly started buffer can save only footage collected so far.

Replay uses GPU Screen Recorder's compressed RAM buffer. It does not support pause and does not collect cursor-path or click metadata. Add clicks manually to a replay clip if you want click-driven zoom.

At 30 seconds with the default Very high 18 Mbps setting, compressed video alone is approximately **64 MiB**. Audio, encoder state, GPU memory, and application overhead are additional; this estimate is not a cap on total memory.

## Editing and export

The **Library** lists local takes. Importing MP4, MKV, WebM, MOV, or AVI copies the source into a new project.

Studio includes source playback, project naming, trim in/out points and playhead marks, **0.5×–3×** playback speed, padding, gradient/wallpaper/no-frame backgrounds, fixed or animated zoom with adjustable focus, and a cursor-based zoom suggestion. Cursor suggestions reflect pointer activity rather than semantic understanding of the application being recorded.

**Preview edits** renders a temporary MP4 using the actual trim, speed, zoom, frame, audio, and enabled layers at up to **960px width and 30 fps**. It uses lower encoding quality than final export. **Show original** returns to the source. After changing the recipe, render another preview to see the changes.

**Save edits** preserves the recipe; previewing and exporting also save it. Exports show progress and can be cancelled. Choose H.264 MP4 or looping GIF with **1920, 1280, 960, or original output width**. Studio caps MP4 at 60 fps and GIF at 24 fps, including for a 120 fps source. GIF has no audio.

Capture and presentation rendering use separate pipelines. GPU capture does not mean GPU-accelerated editing: presentation exports use FFmpeg software filters and its H.264 encoder. Performance depends on the source, selected effects, GPU, encoder, resolution, and audio inputs.

### Wallpapers

Under **Style & export → Composition**, choose **Aurora**, **Dusk**, or **Glacier**, or use **Add your own wallpaper** for PNG, JPEG, or WebP. Selecting a tile switches the background to **Wallpaper**. Images fill the space behind the recording with a centered crop. Increase **Frame padding** to show more; **None** or zero padding hides the backdrop.

Custom images are copied into the take's `wallpapers/` folder and remain in its gallery after save/reopen, deletion of the original image, or a move of the complete take folder. Imports must be at most **50 MiB and 40 megapixels**. They are normalized to a still JPEG with a maximum 3840-pixel edge. Wallpapers appear in edited previews, MP4, and GIF exports. See [wallpaper artwork](WALLPAPERS.md) for bundled-asset details.

### Captions, layers, and zoom to clicks

**Captions & layers** provides a timed layer timeline for captions, annotations, and cursor clicks. Add, copy, delete, enable/disable, undo, redo, and mark source-time intervals at the playhead. Caption controls include SRT/WebVTT import/export, size, color, top/bottom placement, and an optional dark background. Annotations include labels, arrows, outline boxes, and highlights with direct positioning on the original video.

Click rings have editable position, button, size, color, start time, and independent **0.01–10-second duration**. Changing the start time preserves duration. Rings are overlays; they cannot remove or change a cursor embedded in the original recording.

Enable **Style & export → Follow the action → Zoom to clicks**, or use the button under **Captions & layers → Cursor clicks**. Adjust zoom amount, hold, and transition. Nearby enabled clicks pan the focus and extend the zoom. Editing a click's time or position changes the camera motion. **Render click effects** controls the rings independently. Projects without click metadata need manually added clicks.

**Generate captions** uses an optional local speech runtime and model to transcribe selected desktop, microphone, or mixed audio. Aligned words become short editable cues, and transcription can be cancelled. Review the result: names, accents, noise, and overlapping speech can cause errors. There is no speaker identification or translation; imported subtitles preserve plain text and timing rather than external font/position rules.

See [captions and timed layers](CAPTIONS.md) for complete authoring instructions, timing behavior, and offline runtime setup. Recording, manual captions, and subtitle import work without the speech runtime.

### Audio tracks

Exports can mix tracks, keep desktop or microphone only, or mute audio, with adjustable volume. Original playback uses the first audio track; edited preview and export can mix separate tracks.

Audio selection uses the project's track mapping or imported track titles. Untagged imports use the first track for desktop and second for microphone. Selecting an absent track for export produces silent output; caption generation instead reports a missing track explicitly. Already mixed audio cannot be separated.

## Storage, settings, and recovery

The default library is `$(xdg-user-dir VIDEOS)/Lumen`, falling back to `~/Videos/Lumen`. `LUMEN_LIBRARY` selects another directory. A typical captured take starts with:

```text
2026-10-02_12-30-00-a1b2c3d4/
  project.json
  source.mkv
  capture.log
```

Additional files may contain thumbnails, cursor telemetry, custom wallpapers, recovery drafts, and exports. Project JSON writes are atomic, take names are collision safe, and an unreadable manifest does not prevent other projects from opening. Move the complete folder to preserve its metadata and assets.

Settings live in `${XDG_CONFIG_HOME:-$HOME/.config}/lumen/settings.json`. Monitor, frame rate, codec, encoder, quality, audio mode, and cursor/click preferences persist after recording is started. Microphone and camera selections are also saved. Other setup controls return to their defaults after a restart.

An unfinished layer field does not block returning to the HUD or quitting. Lumen saves valid edits and keeps the invalid draft separately in the take folder. Reopening the take offers **Restore draft**. If neither the project nor a recovery copy can be written, the quit dialog also offers **Quit without saving edits**.

Cursor telemetry stores positions used for zoom suggestions. Optional click collection stores mouse-button events and positions during normal recording, not keystrokes. Captured audio and anything visible inside the capture area become part of the source. Automatic transcription extracts selected tracks into a private temporary file and uses an offline model.

The **System** page reports capture tools, devices, session, and GPU capabilities without starting a recording. Use it or `--diagnostics` when troubleshooting, and review logs before sharing them.
