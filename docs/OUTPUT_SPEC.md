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
    speaker_tags.jsonl      # the user's speaker names, append-only; exists only if they named anyone
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
  name (`transcript.final.jsonl.tmp`) and renamed into place when complete. The end of the
  pass always runs in this order: final file renamed into place, then `speakers.json` (only if
  the user named someone), then the buffered audio is removed (unless kept), then `meta.json`
  is set to `complete`, and `DONE` is created last. Everything the downstream app needs is
  therefore in place when `DONE` appears.

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
| `settings` | Diarization, echo and Keep audio settings in force for this meeting. `keep_audio` starts as the Keep audio setting and the user can change it for this one meeting while it is recording or processing (Transcript window, "Keep this meeting's audio"); the after-Stop pass reads it just before deleting `audio\`. |
| `gaps` | `[{"source","start_ms","end_ms","reason"}]`: periods with no audio. Silence is inserted so the clock stays aligned. **Readers must check `reason`:** most gaps are lost audio (for example while re-binding after the default device changed), but `"muted"` is deliberate (see below). |
| `mic_muted_since_ms` | While the user has their mic out of the recording: the meeting time (ms) the mute began, so a live follower knows at once that the silence on `mic` is deliberate. `null` otherwise, and at the end of every meeting. A meeting started with "Start with my mic muted" has `0` from the first moment. |
| `marks`, `mark_kinds` | `marks`: set at Stop, the number of bookmarks (deleted ones not counted). `mark_kinds`: `{"risk": "Risk"}`, the display names of the user-defined bookmark types this meeting's bookmarks use (`{}` for none; absent while the meeting has not used one). Written as soon as a bookmark of such a type is made (so a live follower can name it) and again at Stop; the name is the one in Settings at the time and is **kept in the meeting**, so removing or renaming the type in Settings later does not change how this meeting reads. Entries are in the user's Settings order. Built-in types never appear here. |
| `vocabulary` | Optional. The custom vocabulary terms actually given to speech recognition for this meeting (meeting terms first, then the user's list, cut at a token budget); `[]` if none. Absent in older meetings. |
| `attendees` | Optional, additive. The names typed in the Start dialog's "Who's on the call" box, in order (the number keys 1-9 in the Transcript window mean the first nine), at most 30 names of at most 40 characters, repeats and `You`/`Remote` removed. **Absent when none were entered** (unlike `vocabulary`, which is `[]`), so a meeting without attendees has the same `meta.json` as before. The names are not added to `vocabulary` unless the user ticked "Also give these names to speech recognition". Nothing else in the folder changes; names given to speakers are still in `speaker_tags.jsonl` and `speakers.json`. |
| `vocabulary_changed_at_ms` | Optional. Meeting times (ms) at which the terms in use changed during the recording; `[]` if never. Lines written before a change used the earlier terms. |
| `audio_kept`, `refined_streams`, `echo_segments_dropped`, `duration_s` | Diagnostics. |
| `playbacks`, `playback_segments_dropped` | Optional, additive (set at Stop; absent in older meetings). `playbacks`: how many clips the user played from the Transcript window while the meeting was recording (every line of a Play from here run counts as one clip). `playback_segments_dropped`: utterances that overlapped such a clip in time and repeated its words, so they were left out of the transcript (the clip came back through the system audio or the microphone) and never reached the live speaker labels. Kept as a diagnostic; both are 0 when nothing was played. |
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
| `kind` | `bookmark` (plain), `decision`, `action`, `question`, or a user-defined type id (lower-case letters, digits and `-`, up to four in Settings; made once from the name the user gave it, e.g. `follow-up-with-legal`, and stable when the type is renamed; its display name is in `meta.json` `mark_kinds`). **Treat a kind you do not know, or that `mark_kinds` does not name, as `bookmark`.** |
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
* A meeting started with "Start with my mic muted" is muted from the first sample: `mic_muted_since_ms` is `0` while it lasts and the `muted` gap starts at `0`. Nothing from the mic is stored or transcribed before the user unmutes, exactly as for a later mute.
* The mute does not touch the call app: the other people still hear the user. The `system` stream keeps
  recording.

## `speaker_tags.jsonl` (the user's speaker names)

Append-only, one JSON object per line, flushed per line. Created when the user first names a speaker
or a line in the Transcript window; a meeting nobody was named in has no file. It is never deleted by
the after-Stop pass. Names can also be added after Stop until `DONE` (the pass reads the file again at
its end), so apply the events rather than trust a count.

```json
{"type":"tag","at":"2026-11-09T11:05:12-06:00","scope":"speaker","label":"SPEAKER_04","name":"Eric"}
{"type":"tag","at":"2026-11-09T11:06:40-06:00","scope":"line","seq":31,"source":"system","start_ms":381200,"end_ms":384900,"name":"Dana"}
{"type":"untag","at":"2026-11-09T11:07:02-06:00","scope":"speaker","label":"SPEAKER_04"}
```

| Field | Meaning |
| --- | --- |
| `type` | `tag` (set a name) or `untag` (remove it; no `name`). |
| `scope` | `speaker`: every line with that **live** label in `transcript.jsonl`, past and future, until changed. `line`: one live line; it beats the speaker-level name for that line. |
| `label` | `scope: "speaker"` only: the label as it is in `transcript.jsonl` (`SPEAKER_04`, `MIC_SPEAKER_01`, or `You` for the mic). |
| `source`, `start_ms`, `end_ms`, `seq` | `scope: "line"`: the line's stream and times in `transcript.jsonl`. **Identify the line by `(source, start_ms)`**: `seq` is only the live file's number, and the final file numbers its lines differently. |
| `at` | Wall-clock time of the event. |

Later events win. Tags name **live** labels, so a reader following `transcript.jsonl` applies them to it.
Once `DONE` exists, use `transcript.final.jsonl` with `speakers.json` instead and ignore the tags: the
final pass has already turned them into names for the final labels.

## `speakers.json` (optional)

```json
{"speakers": {"SPEAKER_01": "Dana", "SPEAKER_02": "Luis"},
 "from_tags": true,
 "conflicts": [{"speaker": "SPEAKER_02", "names": {"Luis": 41200, "Dana": 11800}}],
 "updated_at": "2026-11-09T12:10:00-06:00"}
```

Keys are the labels from `transcript.final.jsonl`. Transcript files are never rewritten with names;
apply the mapping when you read.

| Field | Meaning |
| --- | --- |
| `speakers` | Final label to display name. A speaker without an entry has no name (keep showing the label). |
| `from_tags` | Optional, additive. `true` when the after-Stop pass wrote the names from `speaker_tags.jsonl`. |
| `conflicts` | Optional, additive. Final speakers that hold the speech of two named people: `{"speaker", "names": {name: ms}}`, milliseconds of that person's tagged speech inside the speaker, largest first. The speaker is listed under the largest name in `speakers`. A second name is reported when it has at least 25% of the largest one's overlap. The user is shown these in the window; a downstream app can treat the speaker as uncertain. |
| `updated_at` | When the file was last written. |

How the names are made: the pass sums, for each final speaker, the overlap in ms between its segments and
each name's tagged lines on the same stream, and the largest wins. The tags also steer the clustering
itself: two lines the user gave different names are never put in the same final speaker, and a speaker
holding a named line is never folded into a speaker that is unnamed or has another name. With no `speaker_tags.jsonl` the pass writes no
`speakers.json` and the final transcript is exactly what it was before names existed.

**The user's names win.** For a speaker the user named (by tag, or in the window after `DONE`, which
writes straight to this file), the user's name is written. For every other speaker, a name the downstream
app wrote is kept: Huddle Helper re-reads the file immediately before each write and merges into what it
finds. The file is replaced atomically (temp file + rename), so a reader never sees half a file. A
downstream app that writes this file should do the same: re-read, change only its own entries, write
back, and keep the fields it does not know.

## `audio\`

`mic.flac` and `system.flac` (16 kHz mono) plus `words.jsonl` (word timings used by the
final pass). Internal: deleted after the final transcript unless Keep audio is on, and
optionally auto-purged after N days.
