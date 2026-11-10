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
    marks.jsonl             # bookmarks, append-only; exists only if there was a bookmark
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
| `gaps` | `[{"source","start_ms","end_ms","reason"}]`: periods with no audio. Silence is inserted so the clock stays aligned. **Readers must check `reason`:** most gaps are lost audio (for example while re-binding after the default device changed), but `"muted"` is deliberate (see below). |
| `mic_muted_since_ms` | While the user has their mic out of the recording: the meeting time (ms) the mute began, so a live follower knows at once that the silence on `mic` is deliberate. `null` otherwise, and at the end of every meeting. |
| `marks`, `mark_kinds` | Set at Stop: the number of bookmarks (deleted ones not counted) and `{"risk": "Risk"}` for any user-defined bookmark types in use (`{}` for none). |
| `vocabulary` | Optional. The custom vocabulary terms actually given to speech recognition for this meeting (meeting terms first, then the user's list, cut at a token budget); `[]` if none. Absent in older meetings. |
| `vocabulary_changed_at_ms` | Optional. Meeting times (ms) at which the terms in use changed during the recording; `[]` if never. Lines written before a change used the earlier terms. |
| `audio_kept`, `refined_streams`, `echo_segments_dropped`, `duration_s` | Diagnostics. |
| `audio_deleted_at` | Optional, additive. ISO 8601 time at which the user deleted the kept `audio\` from the Meetings list (to the Recycle Bin or permanently); `audio_kept` is then `false`. Absent if that never happened. The transcripts, `speakers.json` and `marks.jsonl` are not touched. |
| `audio_purged_at` | Optional, additive. The same for the automatic purge of kept audio after N days (Settings). |
| `final_stats` | Diagnostics: after-Stop timing, `{"took_s", "audio_s", "speed" (took_s / audio_s; null without audio), "stages": {"read_audio", "diarize_<source>", "merge_small_<source>", "embed_pieces", "cluster", "write"}}`, seconds; only stages that ran (`merge_small_<source>`: re-fingerprinting sherpa's turns to merge small speakers, not included in `diarize_<source>`; `embed_pieces`: voice fingerprints of the pieces; `cluster`: grouping them into speakers). |

## `marks.jsonl` (bookmarks)

Append-only, one JSON object per line, flushed per line. Created on the first bookmark: a meeting
without bookmarks has no file. It is never deleted by the after-Stop pass; it belongs with the
transcript. Bookmarks can also be added, labelled, retyped or deleted after the meeting, so a reader
should apply the events rather than trust a count.

```json
{"type":"mark","id":1,"kind":"decision","at_ms":381200,"at":"2026-11-09T11:06:24-06:00","via":"hotkey"}
{"type":"mark","id":2,"kind":"bookmark","at_ms":512000,"at":"2026-11-09T11:08:35-06:00","via":"line","seq":44}
{"type":"mark_label","id":1,"label":"Ship Friday"}
{"type":"mark_kind","id":2,"kind":"action"}
{"type":"mark_delete","id":2}
```

| Field | Meaning |
| --- | --- |
| `id` | 1-based, increasing within the meeting; later events refer to it. An id is never reused. |
| `kind` | `bookmark` (plain), `decision`, `action`, `question`, or a user-defined type id (lower-case letters, digits and `-`; its display name is in `meta.json` `mark_kinds`). **Treat a kind you do not know as `bookmark`.** |
| `at_ms` | Meeting clock (same as `start_ms` in the transcripts) at the press, or the line's `start_ms` for a line bookmark. The line being spoken at the press is written 1-2 s later, so place a bookmark by `at_ms`, not by file order. |
| `at` | Wall-clock time of the press (or of the line, for a line bookmark). |
| `via` | `hotkey`, `button` (window), `tray`, or `line` (right-click on a line). |
| `seq` | Only for `via: "line"`: the line's `seq` in the file that was shown. The live and final files number lines differently, so rely on `at_ms`. |

`mark_label` sets or replaces a label (an empty string clears it); `mark_kind` changes the type;
`mark_delete` removes the bookmark. Readers apply events in order and ignore events for an `id` they have
not seen. A press of the same type within 2 s of the previous bookmark writes nothing; a press of a
different type within 2 s writes a `mark_kind` for the previous bookmark.

### Muted periods

When the user takes their microphone out of the recording ("Mute me in the recording"), the mic stream
is replaced by digital silence before anything is stored or analysed, and the period is recorded in
`meta.json` `gaps` with the reason `muted`:

```json
"gaps": [{"source": "mic", "start_ms": 305000, "end_ms": 421500, "reason": "muted"}],
"mic_muted_since_ms": null
```

* The entry is added when the user unmutes (or at Stop, if still muted), so every entry has both ends. The
  end is 250 ms after the unmute press, which covers audio already in the capture pipeline; the gap
  matches the silence in `audio\mic.flac` sample for sample. If the app dies while muted, the next start
  closes the period at the last audio.
* No line is added to `transcript.jsonl`: everything between `meeting_start` and `meeting_end` stays a
  `segment`. There are simply no `mic` segments in the period.
* **A `muted` gap is deliberate, not a fault.** The user chose not to be recorded: do not report it as lost
  audio and do not try to recover it. Check `reason` before treating a gap as a problem.
* The mute does not touch the call app: the other people still hear the user. The `system` stream keeps
  recording.

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
