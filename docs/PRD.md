# Meeting Companion PRD

Nov 9, 2026 · @Bryce

## Overview
 Huddle Helper is a Windows desktop app that records your microphone and the meeting audio playing on your PC, transcribes and labels speakers entirely on-device, and streams timestamped transcripts as JSONL files into a local folder. A separate application reads those files to analyze the meeting and act on it.

**Problem.** Meeting notes tools usually send audio to the cloud and bundle transcription with summaries. You want capture and transcription to stay local, and the analysis to live in a different app that only needs a clean, stable file contract.

**v1 scope.** Windows only, English only, fully local transcription on the CPU, speaker diarization, live JSONL streaming, manual Start and Stop. Summaries, action items and any other analysis are out of scope: they belong to the downstream app.

## Goals and non-goals

**Goals**

- Capture microphone and system playback audio as two separate streams, with no meeting bot and no virtual audio driver.
- Transcribe and diarize on the user's machine; no audio or text leaves the device at runtime.
- Write timestamped transcript lines to disk as they are produced, so another app can follow a meeting live.
- Keep the output format stable and documented, so the downstream app can depend on it.

**Non-goals for v1**

- Summaries, action items or any other analysis (the downstream app does this).
- macOS, Linux or mobile support, and languages other than English.
- Cloud transcription, cloud sync or multi-user sharing.
- Joining calls as a participant, recording video, or detecting meetings automatically (recording starts by hand).
- A transcript editor; the app is a recorder, not a notes workspace.

## Users and key use cases

Two consumers: the person running meetings (starts and stops recording, owns the files) and the downstream application (reads the files, never writes to them).

1. **Record a meeting.** The user clicks Start in the tray app before or during a call, and Stop at the end.
2. **Follow live.** The downstream app watches the meeting folder and reads new JSONL lines as they are appended.
3. **Process afterwards.** When the meeting ends, the app marks it complete and writes a final transcript with refined speaker labels; the downstream app picks that up.
4. **Find it later.** Each meeting lives in its own dated folder with a small metadata file, so past meetings are easy to locate and re-process.

## Functional requirements

P0 items define v1; P1 items follow once the core pipeline is stable.

| ID | Requirement | Priority |
| --- | --- | --- |
| F1 | Capture the default microphone as its own stream | P0 |
| F2 | Capture system playback (what the meeting plays through speakers or headphones) as its own stream | P0 |
| F3 | Manual Start / Stop from a tray icon, with a clear recording indicator | P0 |
| F4 | Transcribe both streams on-device in near real time | P0 |
| F5 | Label speakers: mic stream is "You" by default; remote speakers are separated by diarization; a setting runs diarization on the mic stream too for in-room meetings | P0 |
| F6 | Append each transcript segment to a JSONL file as soon as it is final, with start and end timestamps | P0 |
| F7 | One folder per meeting, with a metadata file and a completion marker | P0 |
| F8 | After Stop, write a refined final transcript with improved speaker labels | P0 |
| F9 | Let the user rename speakers (for example "Speaker 2" to "Dana") and store the mapping in the meeting folder | P1 |
| F10 | Buffer both streams as audio during the meeting, for catch-up and the after-Stop pass; delete after the final transcript unless Keep audio is on | P0 |
| F11 | Choose input and output devices and the output folder in settings | P1 |
| F12 | Detect likely meeting start (call app audio active) and prompt to record; v1 stays manual | Later |
| F13 | Languages other than English; v1 uses English-only models | Later |
| F14 | Optional auto-purge of kept audio older than a user-set number of days, off by default | P1 |

## Output spec

This is the contract with the downstream app. Each meeting gets its own folder; the live file is append-only, and the final file replaces it as the source of truth once the meeting ends.

```text
%USERPROFILE%\MeetingCompanion\meetings\
  2026-11-09_1100_weekly-sync\
    meta.json               # start time, devices, models, status
    transcript.jsonl        # live, append-only, one JSON object per line
    transcript.final.jsonl  # written after Stop, refined speaker labels
    speakers.json           # optional: speaker id to display name
    DONE                    # empty marker, created last
    audio\                  # buffered while recording; kept only if Keep audio is on
```

Every line of `transcript.jsonl` is one event. Line 1 is `meeting_start`, the last line is `meeting_end`, and everything between is `segment`:

```json
{"type":"meeting_start","meeting_id":"2026-11-09_1100_weekly-sync","started_at":"2026-11-09T11:00:03-06:00","schema":1}
{"type":"segment","seq":1,"start_ms":1200,"end_ms":4850,"source":"mic","speaker":"You","text":"Let's start with the launch checklist.","confidence":0.93}
{"type":"segment","seq":2,"start_ms":5100,"end_ms":9020,"source":"system","speaker":"SPEAKER_01","text":"Legal still needs one more day.","confidence":0.88}
{"type":"meeting_end","ended_at":"2026-11-09T11:42:30-06:00","segments":212}
```

- **Timestamps** are milliseconds from the start of the meeting, so segments from both streams share one clock. Wall-clock time is in the start and end events.
- **Speaker labels** in the live file are provisional. `transcript.final.jsonl` uses the same schema with refined labels, and the app creates `DONE` only after it is fully written.
- **Crash safety.** Each line is flushed on write. If the app dies, the live file is still valid up to the last complete line, and `meta.json` says the meeting did not finish cleanly.

**Detecting new meetings.** The downstream app runs on the same PC and watches the `meetings` folder. The recorder creates each meeting folder with `meta.json` before writing any transcript line, and writes `transcript.final.jsonl` under a temporary name, renaming it into place when complete. A watcher therefore never sees a half-written final file: new folder means a meeting started, `DONE` means it is safe to process.

## Technical approach (Windows)

These are proposed defaults for the first build; each can change after a spike.

&#91;embedded content: audio pipeline · 2 streams, 1 writer\]

Mic and system audio stay separate until the writer, so every line knows whether it came from you or from the call.

- **Capture.** Use WASAPI from Python (PyAudioWPatch or soundcard are the likely libraries). The microphone is a normal capture stream; meeting audio is WASAPI loopback on the default playback device, which needs no virtual driver and works with any call app. Keeping the two as separate streams gives the "You" versus "others" split for free.
- **Segmentation.** Run voice activity detection on each stream and cut speech into utterances, so transcription works on short chunks and lines can be written as soon as an utterance ends.
- **Transcription.** faster-whisper on the CPU with 8-bit weights and an English-only model (base.en or small.en). Milestone 2 picks the size by measuring speed on the target laptop.
- **Audio buffer.** Both streams are written to compressed audio files in the meeting folder while recording. If live transcription falls behind, it keeps working through the backlog without dropping speech, and the after-Stop pass always has the full audio. By default the files are deleted once `transcript.final.jsonl` is written. With Keep audio on, they stay until the user deletes them, or until the optional auto-purge (F14) removes them.
- **Diarization.** Run only on the system stream by default, since the mic stream is usually just you. A setting switches diarization onto the mic stream, or both streams, for meetings with several people in your room. A light live pass (voice fingerprints grouped by similarity) gives provisional speaker ids; a second, more accurate pass after Stop runs on the buffered audio and produces the refined labels in `transcript.final.jsonl`.
- **Writer.** A single component merges segments from both streams by time, assigns sequence numbers and appends JSONL lines with a flush per line.
- **Echo.** Without headphones, the mic also hears the speakers, which would duplicate remote speech as "You". Start with a dedupe step that drops mic segments matching system text at the same time, and revisit echo cancellation if that is not enough.
- **App shell.** A tray application with Start/Stop, a recording indicator and a settings window. Built in Python, using pystray for the tray icon, and packaged as a Windows app with PyInstaller.

## Non-functional requirements

- **Privacy.** After models are installed, the app makes no network calls. Audio is kept on disk only until the final transcript is written, unless the user turns on Keep audio. Transcripts stay in the user's chosen folder.
- **Recording transparency.** A visible recording indicator is always on while capturing. Laws on recording calls vary by place; the app should remind users to tell participants, and it is the user's responsibility to comply.
- **Latency (proposed).** A finished utterance appears in `transcript.jsonl` within about 5 seconds at the 90th percentile on the reference laptop (a recent CPU, no dedicated GPU; exact model to be chosen).
- **Throughput.** Transcription keeps up with real time on average on the reference laptop. If it falls behind, live lines arrive late but none are lost, and the backlog clears after the meeting.
- **Reliability.** A meeting of at least 2 hours records without dropped audio, and a crash loses at most the utterance in progress.
- **Footprint.** Idle CPU use is negligible when not recording; disk use is text only by default.
- **Compatibility.** Works alongside common call apps without interfering with their audio devices.

## Milestones

Each milestone ends with something you can run and check. Dates are not set yet.

1. **Capture spike.** Record mic and loopback to separate WAV files for 30 minutes on a real call; confirm sync and no dropouts.
2. **Single-stream transcription.** Transcribe one stream with VAD chunking; measure speed against real time on a CPU-only laptop, comparing base.en and small.en.
3. **Two streams to JSONL.** Merge both streams, write the folder layout and `transcript.jsonl`, and share a sample with the downstream app.
4. **Diarization.** Add live speaker labels on the system stream, plus the refined pass and `transcript.final.jsonl`.
5. **Tray app.** Start/Stop, indicator, settings, device selection, Keep audio, and a PyInstaller build.
6. **Hardening.** Long-meeting soak tests, crash recovery, echo handling, and the P1 items you choose to keep.

## Risks and decisions

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Local diarization is less accurate than cloud services, especially with overlapping speech | Wrong speaker labels downstream | Two-pass labels; keep speaker names editable; mark confidence |
| Transcription falls behind real time on weaker laptops | Late or missing live lines | Smaller model fallback; audio buffer so the backlog clears without loss; visible lag in the tray |
| Mic picks up speaker audio and duplicates remote speech | Noisy transcript, wrong "You" lines | Time-aligned dedupe first; echo cancellation if needed |
| Some call apps or devices change the default audio device mid-meeting | Capture silently stops | Watch for device changes and rebind; log gaps in `meta.json` |
| Recording consent rules differ by place | Legal exposure for the user | Visible indicator, reminder at start, no hidden mode |
| CPU-only diarization is slow, so refined labels arrive well after Stop | Downstream app waits on DONE | Measure in milestone 4; show progress in the tray; live file stays usable meanwhile |
| Python runtime plus models make a large install | Slow download and setup | Ship one model; download others on demand |

**Decisions**

| Question | Decision |
| --- | --- |
| Target hardware | Laptop with no dedicated GPU; transcription and diarization run on the CPU |
| Languages | English only |
| How the downstream app finds meetings | It watches the meetings folder |
| Auto-detect meetings (F12) | Manual Start and Stop for v1 |
| Stack | Python |
| Raw audio | Buffered during every meeting so a lagging transcriber can catch up; a Keep audio setting saves it afterwards |
| Refined labels after Stop | Arriving several minutes after Stop on a CPU-only laptop is acceptable |
| Kept audio retention | Stays until the user deletes it; optional auto-purge removes audio older than a user-set number of days |
| Where the downstream app runs | Always on the same PC as the recorder |
