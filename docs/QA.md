# Manual QA

Run in the target Omarchy / Hyprland session. Record the application revision, GPU/driver, monitor resolution and scale, capture settings, and any failed case. These are checks to perform, not claims of completed verification.

## Startup and controls

- [ ] `./scripts/lumen --help` prints the flags and shortcuts; `--diagnostics` prints JSON without opening the studio.
- [ ] Launch Lumen normally: a compact floating HUD appears and no recording starts. Drag the header without activating nearby buttons. Choose **Open Studio**, inspect all four pages, and resize Studio; devices match the desktop.
- [ ] Launch a second copy: the existing view is raised. Use `--hud`, `--studio`, and both view shortcuts; only one recording controller remains, with no automatic capture or lost edits.
- [ ] In the HUD gear menu, change capture source, audio, microphone, countdown, and replay duration. Switch to Studio and back; the controls agree. Open **More settings in Studio** and check the full Record page.
- [ ] Use HUD Mic and Camera buttons before recording. They change the next take's choices without opening either hardware stream. Reopen Lumen and verify saved device identities.
- [ ] Start muted with the camera off. Turn each on, off, and on during the same take; verify actual audio/video changes in the saved recording, pending indicators, unchanged desktop audio, and no interruption of the screen capture. Repeat while paused and while replay is buffering.
- [ ] Turn camera off and microphone muted, then stop and start a new take. They remain off/muted. Confirm their device streams and Lumen's private audio resources close after stopping or quitting.
- [ ] Unplug a selected device, refresh discovery, and verify it is marked unavailable instead of being replaced silently. A failed live enable keeps the HUD's confirmed state and reports the failure.
- [ ] Enable **Hide controls during capture**, start a take, reopen Lumen, and stop. With hiding disabled, verify that controls inside the selected area are visibly included in the recording.
- [ ] Close Studio while idle, recording, exporting, and transcribing. It returns to the HUD while work continues. A preview finishing after that switch must not start playback in the hidden Studio.
- [ ] Use the HUD close button or Ctrl+Q during recording, replay, and countdown. Declining keeps work active; explicit stop/save or countdown cancellation completes before quit. Export/transcription guards remain visible and preserve running work.
- [ ] Use CLI `--record`, `--pause`, and `--stop` against the running instance. HUD **Stop & save** opens the saved take in Studio; saving a replay from HUD keeps buffering and retains the HUD view.
- [ ] If global shortcuts are installed, record, pause, stop, and save a replay while Lumen is minimized; existing Omarchy shortcuts still work.
- [ ] Cancel the countdown and press Escape in the region picker. Neither action leaves a recording running.

## Capture on real hardware

- [ ] Record 10 seconds of a changing screen at 60 fps / H.264 / automatic encoding. Stop, reopen the project, and verify picture, duration, and readable text in the source.
- [ ] Pause for 3 seconds and resume. Check that the saved timeline excludes the paused interval and audio stays in sync.
- [ ] Capture a region, then a fixed window area. Verify boundaries on each monitor, including any fractional scaling, rotation, or negative monitor coordinates in use.
- [ ] Try desktop audio, microphone, and both. For both, confirm two audio streams and listen to desktop-only, microphone-only, and mixed exports.
- [ ] Enable a webcam, disable the captured cursor, and record with cursor-path saving. Verify overlay placement and that a zoom suggestion is offered after pausing the pointer over an area.
- [ ] Try CPU compatibility, then each hardware codec and resolution actually needed. Inspect `capture.log` and the project `backend` field if capture falls back or fails.
- [ ] Use “Stop, save & close” during recording. Reopen Lumen and play the saved source. Check that no Lumen-owned capture process remains.

## Editing, export, and recovery

- [ ] Select a cursor click, change its start, and confirm its independent 0.01–10 second duration stays unchanged. Verify both duration boundaries save without floating-point validation errors.
- [ ] Type an unfinished layer color, change the project name and trim, return to HUD, and close it. Reopen the take: valid edits persist, the original file is unchanged, and **Restore draft** brings back the exact unfinished field. Correct it and save; the recovery banner clears.
- [ ] In a disposable project with simulated write failures, confirm quitting offers **Keep working** and **Quit without saving edits**. Declining preserves the form; accepting still respects active recording/export guards.
- [ ] Import a video, rename it, set trim points, change speed, save edits, restart, and reopen it. The name and recipe persist.
- [ ] Use **Preview edits** with trim, speed, timed zoom, framing, and audio changes. The rendered draft plays correctly at up to 960px / 30 fps. **Show original** restores the source; render again after changing the recipe.
- [ ] Export an MP4 with a background, padding, volume change, and timed zoom. Verify duration, focus, easing, frame, audio selection, and synchronization in an external player.
- [ ] Export a looping GIF. Verify animation and the absence of audio. Check the actual output dimensions; padding is included within the selected width.
- [ ] Cancel an export. The source remains playable and no completed destination is published. Retry successfully with a new filename.
- [ ] Attempt export to an existing filename and to the original source. Both are rejected without modifying either file.
- [ ] In a temporary library, add malformed project JSON. Valid recordings remain visible. Move a complete project folder and reopen it.
- [ ] Test a missing capture binary, disconnected microphone, or unsupported codec where practical. Confirm the error is actionable and raw files/logs remain available.

## Replay

- [ ] Start buffering explicitly from Record or `--replay`. Test the 15, 30, and 60 second selections after waiting longer than each configured history.
- [ ] Save using the header, Ctrl+Shift+S, CLI `--save-replay`, and the optional global shortcut. Each action creates a playable project while buffering continues.
- [ ] Verify the saved clip covers recent activity, has the expected audio tracks, and includes no cursor telemetry. Edit, preview, and export it like a normal project.
- [ ] Stop the buffer using the button or `--stop`. Unsaved history is discarded, saved projects remain, and no Lumen replay process remains.
- [ ] Close and reopen Lumen. Replay must remain stopped until explicitly started. Test long-running memory and encoder load separately from short smoke tests.

## Click-driven zoom · 0.3.0

- [ ] Open an older project with manual or timed zoom. Click follow starts disabled and the old recipe still renders. Enable **Style & export → Follow the action → Zoom to clicks**, then disable it; the manual amount, focus, and interval are preserved.
- [ ] Edit a click's time or position, then use **Cursor clicks → Zoom to clicks** without first pressing Apply layer. The draft is committed, the option is saved, and Style & export opens. An invalid draft or failed save must not show a success message.
- [ ] Set a nondefault zoom amount, hold, and transition. Save, close, and reopen the project; all three values and the enabled state persist. Manual controls are disabled only while following clicks.
- [ ] Place two nearby enabled clicks at different positions. The camera zooms smoothly, pans between targets, extends the hold, and returns to the full source view. Move a target near each edge and check that framing stays within the source.
- [ ] Hide **Render click effects** and rerender. Camera motion remains while rings disappear. Disable an individual click; it no longer changes the camera motion. With no enabled clicks, no click-follow motion is produced.
- [ ] Change a click's position and timing, apply the layer, and render again. Preview and final export follow the updated event. Verify both trimmed footage and 0.5×/2× playback; hold and transition durations change with playback speed.
- [ ] On an imported video or replay with no click metadata, add clicks manually and follow them. Check MP4 and GIF exports, readable captions, attached annotations, and unchanged source contents.

## Captions, annotations, and editable clicks

- [ ] Add multiline Unicode captions, adjust start/end at the playhead, and test overlap. Import SRT/WebVTT containing escaped markup and export with trim/speed; verify timing and literal text in another subtitle player.
- [ ] Generate captions for a known spoken sample with desktop-only, microphone-only, and mixed audio as appropriate. Check that the selected track is honored, review words/timing, and cancel an in-progress transcription.
- [ ] Generate or import captions while existing cues are present. They append safely, one Undo restores the prior set, and original video/audio files remain unchanged.
- [ ] Add text, arrow, box, and highlight annotations. Place endpoints on the original video, change zoom/framing, and verify their final positions in rendered preview and export.
- [ ] Capture actual left/right/middle clicks inside the selected area. Check ordinary mouse behavior, pause exclusion, outside-region filtering, and that click collection stops with recording.
- [ ] Move, retime, recolor, disable, copy, and delete captured/manual click rings. Test Undo/Redo, save/reopen, and the layer timeline's seek controls.
- [ ] Change layers after rendering a preview, render again, and compare with final export. Hidden layers must be absent and captions must remain readable after zoom.

## Performance comparison

Use the same monitor, scene, duration, codec, frame rate, audio sources, and encoder class for each recorder. Measure process CPU, GPU/video-engine activity, memory, output size, frame timing, and perceived audio/video sync over a representative take. Keep warm-up and export measurements separate. A successful short recording is a smoke test, not a performance benchmark.
