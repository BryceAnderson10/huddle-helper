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
4. **Open meetings folder** shows the output; **Rename speakers…** maps `SPEAKER_02` to
   a name (saved as `speakers.json`); **Settings…** picks devices, folder, model,
   diarization, echo handling, Keep audio and auto-purge.

Files and folders:

| What | Where |
| --- | --- |
| Meetings | `%USERPROFILE%\MeetingCompanion\meetings` (changeable) |
| Settings | `%APPDATA%\HuddleHelper\settings.json` |
| Downloaded models, logs | `%LOCALAPPDATA%\HuddleHelper\models`, `...\logs` |

## Development

Requires Windows and Python 3.12.

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
| `diarize.py` | Live clustering and offline diarization |
| `writer.py`, `echo.py` | Time-ordered merge, echo dedupe, JSONL writing |
| `recorder.py` | Threads and lifecycle of one recording |
| `finalize.py` | After-Stop pass: refined labels, final file, cleanup, DONE |
| `housekeeping.py` | Crash recovery, audio auto-purge |
| `app.py`, `tray.py`, `ui.py` | Controller, tray icon, dialogs |

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
