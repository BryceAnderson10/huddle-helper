# Huddle Helper output contract (schema 1)

This is the stable interface between Huddle Helper (the recorder) and any downstream
app that analyzes meetings. The downstream app only reads these files; it never writes
to them (except optionally `speakers.json`, see below).

## Folder layout

```text
%USERPROFILE%\MeetingCompanion\meetings\        (configurable in Settings)
  2026-11-09_1100_weekly-sync\
    meta.json               # start time, devices, models, status
    transcript.jsonl        # live, append-only, one JSON object per line
    transcript.final.jsonl  # written after Stop, refined speaker labels
    speakers.json           # optional: speaker id -> display name
    DONE                    # empty marker, created last
    audio\                  # buffered while recording; kept only if Keep audio is on
```

Meeting ids are `YYYY-MM-DD_HHMM_<slug>` in local time. If two meetings start in the same
minute with the same name, the second gets a `-2` suffix.

## Detecting meetings

* **New folder = a meeting started.** The recorder builds each folder under a hidden
  temporary name (`.<meeting_id>.creating`) and renames it into place with `meta.json`
  already written, so a watcher never sees a meeting folder without `meta.json`.
  Ignore any entry whose name starts with `.`.
* **New lines in `transcript.jsonl` = live transcript.** Read appended lines as they
  arrive. Every line is flushed to disk on write.
* **`DONE` = safe to process.** `transcript.final.jsonl` is written under a temporary
  name (`transcript.final.jsonl.tmp`) and renamed into place when complete. Buffered
  audio is removed (unless kept), `meta.json` is set to `complete`, and `DONE` is created
  last.

## `transcript.jsonl` and `transcript.final.jsonl`

One JSON object per line, UTF-8, `\n` line endings. Line 1 is `meeting_start`, the last
line is `meeting_end`, everything between is `segment`.

```json
{"type":"meeting_start","meeting_id":"2026-11-09_1100_weekly-sync","started_at":"2026-11-09T11:00:03-06:00","schema":1}
{"type":"segment","seq":1,"start_ms":1200,"end_ms":4850,"source":"mic","speaker":"You","text":"Let's start with the launch checklist.","confidence":0.93}
{"type":"segment","seq":2,"start_ms":5100,"end_ms":9020,"source":"system","speaker":"SPEAKER_01","text":"Legal still needs one more day.","confidence":0.88}
{"type":"meeting_end","ended_at":"2026-11-09T11:42:30-06:00","segments":212}
```

| Field | Meaning |
| --- | --- |
| `seq` | 1-based, increasing by 1 within the file. The final file numbers its own segments. |
| `start_ms`, `end_ms` | Milliseconds since the meeting started. Both streams share this clock. |
| `source` | `mic` (your microphone) or `system` (what the PC played: the call). |
| `speaker` | See speaker labels below. |
| `text` | Transcribed English text. |
| `confidence` | 0–1, from the speech model's average token probability. |

Segments are written in `start_ms` order across both streams.

**Speaker labels**

| Label | When |
| --- | --- |
| `You` | Mic stream when mic diarization is off (the default). |
| `Remote` | System stream when system diarization is off. |
| `SPEAKER_01`, `SPEAKER_02`, … | Distinct voices in the system stream. |
| `MIC_SPEAKER_01`, … | Distinct voices on the mic, when "separate speakers on my microphone" is on (in-room meetings). |

Labels in `transcript.jsonl` are **provisional**. `transcript.final.jsonl` has the same
schema with refined labels from a second, more accurate pass over the whole recording;
the numbering may differ from the live file, and a live segment may be split in two
where the refined pass finds a speaker change mid-utterance. Treat the final file as the
source of truth once `DONE` exists.

**Echo.** Without headphones the mic hears the call. Mic segments that repeat
system-stream speech at the same time are dropped (setting: on by default), so remote
speech isn't attributed to `You`.

**Interrupted meetings.** If the app or PC dies mid-meeting, `transcript.jsonl` is valid
up to the last complete line. On the next start the recorder removes any half-written
last line, appends `meeting_end` with `"interrupted": true` (and `last_end_ms`), sets
`meta.json` `status` to `interrupted` and `clean_exit` to `false`, then runs the final pass
as usual.

## `meta.json`

Rewritten atomically (temp file + rename) whenever it changes.

| Field | Meaning |
| --- | --- |
| `meeting_id`, `title`, `schema`, `app_version` | Identity. |
| `status` | `recording` → `processing` → `complete`; or `interrupted` (crash, then finalized), `failed` (final pass error; `error` holds the message). |
| `clean_exit` | `null` while recording, `true` after a normal Stop, `false` after a crash. |
| `started_at`, `ended_at` | ISO 8601 with UTC offset. |
| `final_written_at`, `final_segments` | Set when the final transcript is in place. |
| `segments` | Lines in the live file. |
| `devices` | `{"mic": name, "system": name}` actually captured. |
| `models` | Speech, VAD and diarization models used. |
| `settings` | Diarization, echo and Keep audio settings in force for this meeting. |
| `gaps` | `[{"source","start_ms","end_ms","reason"}]`: periods with no audio, e.g. while re-binding after the default device changed. Silence is inserted so the clock stays aligned. |
| `audio_kept`, `refined_streams`, `echo_segments_dropped`, `duration_s` | Diagnostics. |
| `final_stats` | Diagnostics: after-Stop timing, `{"took_s", "audio_s", "speed" (took_s / audio_s; null without audio), "stages": {"read_audio", "diarize_<source>", "embed_pieces", "cluster", "write"}}`, seconds; only stages that ran (`embed_pieces`: voice fingerprints of the pieces; `cluster`: grouping them into speakers). |

## `speakers.json` (optional)

```json
{"speakers": {"SPEAKER_01": "Dana", "SPEAKER_02": "Luis"}, "updated_at": "2026-11-09T12:10:00-06:00"}
```

Written by "Rename speakers…" in the tray. Keys are the labels from
`transcript.final.jsonl`. Transcript files are never rewritten with names; apply the
mapping when you read.

## `audio\`

`mic.flac` and `system.flac` (16 kHz mono) plus `words.jsonl` (word timings used by the
final pass). Internal: deleted after the final transcript unless Keep audio is on, and
optionally auto-purged after N days.
