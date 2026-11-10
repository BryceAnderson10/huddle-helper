# Huddle Helper

A Windows tray app that records your microphone and the meeting audio playing on your
PC, transcribes and labels speakers **entirely on-device**, and streams timestamped
transcripts as JSONL into a local folder for a separate app to analyze.

* Two separate streams: your mic, and WASAPI loopback of the playback device. No meeting
  bot, no virtual audio driver, works with any call app.
* Silero VAD cuts speech into utterances; faster-whisper (English, int8, CPU) transcribes them.
* Live speaker labels from voice fingerprints; a refined pass after Stop (pyannote
  segmentation + WeSpeaker embeddings via sherpa-onnx) writes `transcript.final.jsonl`.
* Lines are flushed as they're final; a single writer merges both streams in time order.
* Mic lines that echo the call (no headphones) are dropped.
* After models are installed the app makes no network calls.

The file contract for downstream apps is in [docs/OUTPUT_SPEC.md](docs/OUTPUT_SPEC.md).
The product requirements are in [docs/PRD.md](docs/PRD.md).

## Using it

1. Run `HuddleHelper.exe` (or `python -m huddle_helper` from a dev setup). A grey
   microphone icon appears in the tray.
2. Click it → **Start recording…**, name the meeting, and start. The icon turns red with a
   dot while recording. Tell participants you're recording; laws vary by place.
3. **Stop recording** when done. The icon turns amber while speaker labels are refined
   (a few minutes for a long meeting on a laptop CPU), then you get a notification.
4. **Meetings…** lists every meeting (status, length, speakers, bookmarks, audio size) to open,
   export, show in Explorer, retry a failed final pass, or delete (to the Recycle Bin, or
   permanently; kept audio can be deleted on its own). **Rename speakers…** maps `SPEAKER_02` to
   a name (saved as `speakers.json`); **Settings…** picks devices, folder, model,
   diarization, echo handling, Keep audio and auto-purge.

Files and folders:

| What | Where |
| --- | --- |
| Meetings | `%USERPROFILE%\MeetingCompanion\meetings` (changeable) |
| Settings | `%APPDATA%\HuddleHelper\settings.json` |
| Downloaded models, logs | `%LOCALAPPDATA%\HuddleHelper\models`, `...\logs` |

## Development

Requires Windows and Python 3.12 or 3.14.

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\huddle-helper download-models      # one-time: base.en + diarization (~180 MB)
.\.venv\Scripts\python -m huddle_helper             # tray app
.\.venv\Scripts\python -m pytest                    # tests (the real-model e2e test needs the models)
```

Command line (useful for the milestone checks):

```powershell
huddle-helper record --title "Weekly sync"          # record from live devices, Enter to stop
huddle-helper capture-spike --minutes 30            # M1: mic + loopback to WAVs, reports gaps
huddle-helper bench --audio call.wav --models base.en,small.en   # M2: speed vs real time
python tools\make_sample_meeting.py --out sample --echo          # synthetic two-voice meeting (Windows TTS)
huddle-helper simulate --mic sample\mic.wav --system sample\system.wav --realtime
huddle-helper finalize <meeting folder>             # re-run the after-Stop pass
huddle-helper recover                               # finish meetings left by a crash
huddle-helper tray --replay <meeting folder> --out <dir> [--keep-audio]  # dev: run a kept meeting's audio through the tray app (add --auto to run unattended)
huddle-helper devices
```

### Packaging

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build.ps1            # bundles base.en + diarization models
powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -NoModels  # smaller; downloads on first run
```

Produces `dist\HuddleHelper\` with `HuddleHelper.exe` (tray) and `huddle-helper-cli.exe`.
Run the exe from inside that folder (or a shortcut to it): it needs the `_internal\` and
`models\` folders next to it. To install or share it, copy or zip the whole
`dist\HuddleHelper\` folder, not just the exe.

## Code map

| Module | Role |
| --- | --- |
| `audio/capture.py` | WASAPI mic + loopback, resampling, default-device change detection and rebind |
| `audio/aligner.py` | Keeps both streams on one clock (fills loopback silence and dropouts) |
| `audio/buffer.py` | FLAC buffer per stream |
| `vad.py` | Streaming Silero VAD and utterance segmentation |
| `transcribe.py` | faster-whisper wrapper |
| `vocab.py` | Custom vocabulary: parse terms, token budget, prompt sentence |
| `diarize.py` | Live clustering and offline diarization |
| `writer.py`, `echo.py` | Time-ordered merge, echo dedupe, JSONL writing |
| `recorder.py` | Threads and lifecycle of one recording |
| `finalize.py` | After-Stop pass: refined labels, final file, cleanup, DONE |
| `housekeeping.py` | Crash recovery, audio auto-purge |
| `library.py` | Meetings list: `MeetingInfo`, cached `scan` (sizes measured on a worker thread), status mapping, filter and search, `delete_meeting` / `delete_audio` (never while busy; only real folders inside the meetings folder) |
| `shell.py` | Recycle Bin delete through `SHFileOperationW` (`recycle`), `has_recycle_bin` |
| `tail.py` | `JsonlTail`: reads only the complete lines appended to a growing JSONL file |
| `transcript_model.py` | UI-free model of one meeting: lines, names, bookmarks and gaps as ordered rows and change ops; live-to-final swap |
| `marks.py` | Bookmarks: `marks.jsonl` events, the reducer shared with the model, `MarkLog` (ids, the 2 s double-press rule, edits) |
| `hotkeys.py` | Global hotkeys: combo parsing, the AltGr check, `HotkeyThread` (`RegisterHotKey` message loop, registered only while recording) |
| `audio/gate.py` | `MicGate`: replaces the mic with zeros while muted (250 ms tail), counts samples so the muted period matches `mic.flac` exactly |
| `audio/ring.py` | `RecentAudio`: the last 10 minutes of one stream in memory (16-bit), written by the stream worker after the mic gate, so muted speech can never be played back |
| `playback.py` | Click to hear: padded clips of a line from the kept FLAC or the ring (quiet clips raised, ends moved to quiet points), bookmark and "Hear this voice" choices, the playback windows and drop rule, the headphone check, the reasons a play control is disabled, and `Player` (one clip at a time on a worker thread, resampled to the output device's rate, output `gain`) |
| `export.py` | Meeting to Markdown / plain text (`huddle-helper export`) |
| `session.py` | `MeetingSession`: the Transcript window's polling of one meeting (tails, meta, `DONE`, live-to-final swap), no Tk |
| `replay.py` | `tray --replay <folder>` dev aid: feeds a kept meeting's audio through the recorder; unattended-run logic |
| `gui/window.py`, `gui/transcript_view.py`, `gui/dialogs.py` | Main window: header (mute, Keep this meeting's audio, Export), notice bar, status bar, read-only transcript text with a play control on each line and bookmark, Export, bookmark and delete-confirmation dialogs (thin Tk); the window swaps between the Transcript and Meetings pages |
| `gui/speaker_bar.py` | The speaker bar: one chip per person under the notice bar; a click opens the chip menu ("Hear this voice") (thin Tk) |
| `gui/meetings_view.py` | Meetings page: list, filter, search, multi-select, Open, Export…, Show in Explorer, Retry final pass, Delete audio, Delete meeting (thin Tk; scan and delete run on worker threads) |
| `gui/meetings_logic.py`, `gui/confirm.py` | What the Meetings page shows and which buttons work; the exact wording of the delete confirmation (what goes, can it come back, 24-hour warning, permanently), as pure functions |
| `gui/logic.py`, `gui/theme.py` | What the window says (header, status, time left, default meeting, mute bar, Bookmark menu, hotkey rows, tray tooltip) as pure functions; colours, fonts, DPI awareness |
| `app.py`, `tray.py`, `ui.py` | Controller (after-Stop progress and time-left estimate, bookmarks, mute, `is_busy`, `retry_final`, `delete_meetings`, `play_clip` / `stop_playback`, `set_keep_audio`), tray icon and menu (Meetings…, Bookmark submenu, mute, `muted`/`flag` icons, hotkeys), dialogs (Settings has Hotkeys) |

## Status against the PRD milestones

| Milestone | State |
| --- | --- |
| 1. Capture spike | Built (`capture-spike`). Verified briefly on real devices; **the 30-minute real-call run is still to do.** |
| 2. Single-stream transcription | Built (`bench`). On the dev PC, base.en ran ~4× real time over both streams. Pick the default model on the target laptop. |
| 3. Two streams to JSONL | Done; verified with synthetic meetings (median 0.9 s, p90 1.5 s from end of speech to line on disk). |
| 4. Diarization | Done (live + refined). Verified on two synthetic voices; needs tuning on real calls. |
| 5. Tray app | Done, including settings, device selection, Keep audio and a PyInstaller build. |
| 6. Hardening | Crash recovery, echo dedupe, device rebind, F9 rename and F14 purge are in. **Long soak tests (2 h+) are still to do.** |

Known limitations:

* The refined pass loads a whole stream into memory (~230 MB per hour of audio per stream).
* Device-change rebinding is implemented but hasn't been exercised by switching devices mid-call.
* Echo handling is text-based dedupe only; there is no acoustic echo cancellation.
