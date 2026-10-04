# Lumen 0.5.0 verification record

Verified on the development Omarchy / Hyprland Wayland desktop through **2026-10-03**. Version 0.5.0 adds live microphone and webcam controls to the HUD, recording, and replay workflows below. These checks establish the tested paths; they are not a Recordly comparison or a general hardware certification.

## Completed

- **Wallpaper workflow passed** via `scripts/verify_wallpapers.py`, using synthetic footage and a generated custom image. Built-in tile selection, the visible import button, custom-image decoding, gallery persistence after save/reopen and original-image deletion, native edited-preview playback, and captioned **960 × 610 H.264/AAC** export passed. The 0.2–2.8-second trim at 1.5× speed produced a **1.733333-second** output that fully decoded, with the source SHA-256 unchanged. The file chooser callback received the fixture programmatically; choosing files through the real system dialog remains a manual QA case. Own-window screenshots and results: `/tmp/lumen-wallpapers-ui-5z5cgqda`. **288 unit/integration tests passed in 17.262 seconds**, and Ruff passed for `lumen`, `tests`, and `scripts`. New coverage includes PNG/JPEG/WebP imports, private files, duplicate imports, malformed galleries, missing images, portability after moving a take, stale dialog/import completions, all bundled backdrops in MP4, GIF, wide/portrait cover crops, and zero-padding/no-frame behavior. The temporary installer and wheel both include all three wallpaper JPEGs and the new modules.
- **Native HUD input toggles passed** via `scripts/verify_inputs.py`. A private tone source and generated camera image were toggled through the actual HUD while recording only the test application's pattern window. The saved **1200 × 676 H.264/AAC** take lasted **8.789 seconds**. Both microphone-on intervals contained the 997 Hz tone (RMS approximately 0.0883); every tested muted interval decoded to exactly zero. The camera's sampled pixels changed from the scene to green and back. Pending states, confirmed labels, persisted choices, private-source removal, and unchanged audio defaults passed. Artifacts: `/tmp/lumen-inputs-ui-hrt7hscv`. No physical input was opened and no tone played through speakers. Simulated-state window checks confirmed the new floating HUD at **540 × 180 logical pixels**.
- **Native replay inputs passed** via `scripts/verify_replay_inputs.py`. A **2.346-second** clip saved while buffering continued with the generated camera visible. Its reserved microphone track decoded to exact silence. Camera off exited its player, and stopping replay removed the owned audio modules and temporary compositor rules. Artifacts: `/tmp/lumen-replay-inputs-5fqph9c8`.
- **Microphone routing and crash cleanup passed.** A separate synthetic audio check verified on/off signal, unchanged default devices and existing mute/volume levels, and no leftover private modules: `/tmp/lumen-mic-test-haf1xi8x`. Killing a disposable parent with an active synthetic microphone caused the guardian to release its loopback and silent sink within **0.114 seconds** in that run: `/tmp/lumen-mic-crash-4tujazx0`. This validates application-parent death handling, not recovery from an unresponsive audio server or a killed guardian.
- **Native quit and recovery passed** via `scripts/verify_quit.py`, using a private library and synthetic silent footage. An invalid pending click duration of `24.14` did not block exit. The valid name and 0.25–2.75-second trim saved, the committed 0.6-second click stayed intact, and exactly one private recovery file retained the raw field through quit and shutdown. A second launch offered **Restore draft**, restored the exact value, and saved a corrected 0.8-second duration with the banner cleared. The source SHA-256 remained unchanged. Artifacts: `/tmp/lumen-quit-ui-v8s0az4o`. A native widget check also preserved the recorded 8.1449-second click time and independent 0.6-second duration: `/tmp/lumen-draft-widget-zkbtcv_f/result.json`.
- **273 unit and integration tests passed in 14.043 seconds; Ruff passed for `lumen`, `tests`, and `scripts`.** Coverage includes capture arguments and validation, project persistence and failure handling, UI state, real FFmpeg rendering, source preservation, cancellation, audio selection, replay lifecycle, timed layers, caption parsing/retiming, aligned speech-cue grouping, and transcription process cleanup. Thirty-seven HUD tests cover controls, shared state, view routing, quit guards, and hidden-preview playback. Recovery regressions cover invalid pending fields, durable backup failure, explicit discard, active-work guards, exact form restoration, backup cache damage, and rollback after a failed restoration. Click-camera tests verify decoded zoom/focus geometry, smooth pans, edge clamping, trim and speed, fixed caption placement, unchanged manual zoom, and an actual render with 10,000 click events. Crash cleanup was verified using a disposable test process; recorder children receive a finalizing signal when their parent exits.
- **Native capture workflow passed.** GPU H.264 recording, pause/resume, stop, project reload, saved edit recipe, cursor-based zoom suggestion, styled MP4 export, and full FFmpeg decode were exercised. The original file's SHA-256 remained unchanged and the owned recording process stopped.
- **Scaled region capture passed.** On DP-2 at 1.25× scale, a 512 × 288 logical region produced the expected 640 × 360 native recording at 30 fps, with a desktop-audio track. Its edited output was 960 × 568 and decoded successfully.
- **Live GTK workflow passed** via `scripts/verify_ui.py`: a silent capture of the application's own window, pause/resume, source playback, rendered edited-preview playback, 1920px final MP4 export, replay start/save, continued buffering after save, and replay stop. Screenshots and media were kept in the test's private temporary directory.
- **Native HUD workflow passed** via `scripts/verify_hud.py`: default compact launch, native floating behavior, single-instance view routing, HUD/Studio navigation, retained capture settings, countdown cancellation, pause/resume, stop-and-save, and a quit guard that kept the take running when closing was declined. The silent capture of the test application's own Studio window produced a **998 × 720 H.264** recording at **60 fps**, lasting **1.617 seconds**. No permanent Hyprland configuration was changed. Artifacts and result metadata are in `/tmp/lumen-hud-ui-f2_fd_4n`. A subsequent sizing check confirmed the final floating HUD at **540 × 150 logical pixels** (GTK content: 538 × 148), with a screenshot at `/tmp/lumen-hud-size-fwp26y4k/hud.png`. That sizing check did not repeat recording; later quit-validation and hidden-preview fixes are covered by the HUD regression tests.
- **Native timed-layer authoring and UI transcription passed** via `scripts/verify_layers.py --speech-fixture`. Four generated captions appeared in the GTK editor; a draft typed during transcription was preserved and saved. A composition containing a caption, two annotations, and a click effect rendered and exported at 960 × 596 for 2.333 seconds, with the source SHA-256 unchanged. Artifacts are in `/tmp/lumen-layer-ui-ryq3w334`. Sidecar tests verified SRT/WebVTT timing after trim and speed changes, literal Unicode/markup preservation, overlaps, and existing-file protection.
- **Native click-zoom workflow passed** via `scripts/verify_click_zoom.py`. The Cursor clicks shortcut enabled follow mode, and a **2.1×** zoom, **1.35-second** hold, and **0.4-second** transition survived save/reload. Toggling follow preserved the existing manual 1.6× zoom and 0.7–4.9-second interval, with the correct controls enabled in each mode. A 0.2–5.8-second trim at 1.5× speed produced a playable, fully decoded **960 × 540 H.264** export lasting **3.733333 seconds**. Zoom remained visible with click rings hidden, the disabled third click did not move the camera, and the view returned to its original framing. The source SHA-256 remained unchanged. Synthetic footage, screenshot, export, and results are in `/tmp/lumen-click-zoom-ui-e_agem9u`.
- **Real offline transcription passed.** The existing faster-whisper base model transcribed the public speech fixture from upstream whisper.cpp into four readable cues using actual word-alignment timestamps. Expected spoken content was recognized, source tracks were selected explicitly, and cancellation of the real speech worker was verified. Audio-frequency integration tests also confirmed that desktop-only and microphone-only extraction selects the requested track. The fixture, transcript, and result metadata are in `/tmp/lumen-speech-verification`.
- **Large editable projects passed.** A manifest containing 3,000 clicks and 1,000 captions, exceeding the former 1 MiB limit, saved and reloaded intact. The metadata limit is now 16 MiB.
- **Packaging and shortcut helper checks passed.** Temporary local installation worked with spaces in paths and included the stylesheet and documentation. The shortcut helper's preview confirmed unused proposed keys; simulated application, repeated installation, conflict rejection, and failed-validation rollback passed.

## Scoped measurements

Native `Recorder.start()` returned after first-frame readiness in **0.208–0.309 seconds** during short capture checks. This measures recorder startup, not application-window startup.

One separate, roughly four-second **800 × 450 silent recording at 60 fps** showed approximately **3% of one CPU core and 108 MiB resident memory for the GPU Screen Recorder process**. This excludes the Lumen interface, cursor telemetry, GPU memory, and other desktop processes. The tiny region and brief duration do not represent full-display workloads or establish an advantage over another recorder.

The default replay video-buffer estimate of approximately 64 MiB is calculated from 30 seconds × 18 Mbps, not a measurement of total application memory.

The optional speech runtime occupies approximately 392 MiB of isolated package files. Existing cached packages were copied locally and the existing model was reused; no package or model download was required. The configured Python interpreter and model remain external local dependencies. This is an installation-size observation, not a speech-process RAM measurement.

The click-zoom check compared frames from a static synthetic scene, scaled to 240 × 135 RGB pixels. With rings hidden, the mean absolute channel difference between the initial and zoomed frames was **25.260** on the 0–255 channel scale; after the zoom ended it was **0.271**. These comparisons confirm visible motion and return to the original framing in that fixture; they are not visual-quality or performance scores.

## Still needs broader testing

- Real microphone and webcam capture, and desktop/microphone balance or synchronization by listening to representative recordings.
- Automatic caption accuracy across languages, accents, noise, overlapping speakers, and long recordings. The public English fixture verifies a working local pipeline, not general transcription accuracy.
- Long recordings and replay sessions, sustained memory usage, dropped-frame behavior, disk exhaustion, and device disconnections.
- Full-resolution performance across the user's complete workloads, different GPU drivers/codecs, 120 fps capture, and the compatibility backend on additional hardware.
- Every monitor arrangement, rotation, and fractional scale; the successful scaled-region check covers one configuration.
- A matched Recordly comparison using the same scene, dimensions, encoder, frame rate, duration, and audio sources.

The [manual QA checklist](QA.md) covers these follow-up scenarios. Unit tests can run with `/usr/bin/python -m unittest discover -s tests -v`. `verify_hud.py`, `verify_ui.py`, and `verify_workflow.py` intentionally record desktop content inside the target Wayland session; the HUD and UI checks restrict capture to their own application windows. `verify_layers.py` and `verify_click_zoom.py` instead generate synthetic footage and take only their own window screenshots. The layer check can also use an explicitly supplied local/public speech fixture.

## MCP integration — 2026-10-04

Lumen 0.6.0 was checked with the official Python MCP SDK 2.3.0 in the Omarchy
Hyprland session. Seven protocol tests passed for discovery, current/legacy
connections, strict recipe/capture schemas, structured outputs, resources, prompt
content, and actionable native errors. The desktop proof used
`scripts/verify_mcp.py`, synthetic footage, and only its owned floating test window
with audio, webcam, click hooks, and cursor telemetry disabled.

The stdio client discovered all 20 tools, started capture, paused/resumed, finalized
a take, started/saved/stopped replay, imported footage and a custom wallpaper,
changed trim/speed, added captions and an annotation, received a frame image, and
rendered a preview and MP4. Render cancellation completed without publishing a
partial output. A legacy client reconnected to retrieve and finish an existing
job. The imported source's SHA-256 remained unchanged. The final 960px export's
duration matched a 0.2–2.8-second trim at 1.5× speed. Artifacts are in
`/tmp/lumen-mcp-verify-lg4cf1j6`. This verifies short synthetic workflows rather
than sustained capture or physical input hardware.

Cold-start verification passed with Lumen initially closed, both from source and
from an isolated installation. The first status response arrived, capture stayed
inactive, and a second legacy client reached the same app PID after the first
disconnected. Only the isolated test instance was terminated. The copied SDK
runtime/launcher worked, and the 0.6.0 wheel built with the optional MCP extra and
bundled wallpaper assets.
