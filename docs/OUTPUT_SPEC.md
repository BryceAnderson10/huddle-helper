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
    echo_dropped.jsonl      # mic lines dropped as echo, append-only; exists only if any were dropped
    heartbeat.json          # recorder's "still alive" time, refreshed every 2 s while recording
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
* **Is it still recording? Ask `meta.json` and `heartbeat.json`, not the transcript.** A meeting
  is active only while its `status` is `recording` **and** its heartbeat is fresh; see
  [`heartbeat.json`](#heartbeatjson-is-the-recorder-alive) below.

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

**Transcript version.** Optional, additive. The final file's `meeting_start` line has
`"transcript_version"`, an opaque string that names this one final transcript, and `meta.json` has the
same value once the pass ends. Every final pass writes a new one, even if the words come out the same:
re-running `huddle-helper finalize`, or the second transcription, replaces the file and its version.
Earlier final transcripts are not kept. Compare versions for equality only; do not parse them. A reader
that quoted the final transcript keeps the version it read: a different version means the wording or
timing may have changed. Naming a speaker or editing a bookmark never changes it (those have their own
times: `names_changed_at` in `speakers.json`, `changed_at` in `marks.jsonl`). The file's first line is
authoritative: while a re-run is finishing, `meta.json` still holds the previous version until it is set
to `complete`. The live file has no version: it is append-only, so the lines read so far say how current
it is. Final files written before this field existed have none.

**Echo.** Without headphones the mic hears the call. Mic segments that repeat
system-stream speech at the same time are dropped (setting: on by default), so remote
speech isn't attributed to `You`. Every dropped mic line is kept in `echo_dropped.jsonl` (below).

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
| `transcript_version` | Optional, additive. The version of the final transcript in place, set with `final_written_at` (see Transcript version above). |
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
| `audio_kept`, `refined_streams`, `echo_segments_dropped`, `duration_s` | Diagnostics. `echo_segments_dropped` is the number of lines in `echo_dropped.jsonl` (0 when there is no file); a line the app could not write is missing from the file and reported in the app log. |
| `playbacks`, `playback_segments_dropped` | Optional, additive (set at Stop; absent in older meetings). `playbacks`: how many clips the user played from the Transcript window while the meeting was recording (every line of a Play from here run counts as one clip). `playback_segments_dropped`: utterances that overlapped such a clip in time and repeated its words, so they were left out of the transcript (the clip came back through the system audio or the microphone) and never reached the live speaker labels. Kept as a diagnostic; both are 0 when nothing was played. |
| `audio_deleted_at` | Optional, additive. ISO 8601 time at which the user deleted the kept `audio\` from the Meetings list (to the Recycle Bin or permanently); `audio_kept` is then `false`. Absent if that never happened. The transcripts, `speakers.json` and `marks.jsonl` are not touched. |
| `audio_purged_at` | Optional, additive. The same for the automatic purge of kept audio after N days (Settings). |
| `final_stats` | Diagnostics: after-Stop timing, `{"took_s", "audio_s", "speed" (took_s / audio_s; null without audio), "stages": {"read_audio", "diarize_<source>", "merge_small_<source>", "embed_pieces", "cluster", "write"}}`, seconds; only stages that ran (`merge_small_<source>`: re-fingerprinting sherpa's turns to merge small speakers, not included in `diarize_<source>`; `embed_pieces`: voice fingerprints of the pieces; `cluster`: grouping them into speakers). |
| `final_transcriber` | Present only when the kept audio was transcribed a second time after Stop (WP15, off by default): the model's name. The text of a final line may then differ from the live line at the same time; `transcript.jsonl` and `audio\words.jsonl` are never rewritten. `final_stats` then also has `stages.retranscribe` (seconds), `retx_lines_changed` and `retx_lines_kept_live` (lines whose new text was refused as empty, under half or over twice the live line's words); `retx_failed: true` instead when the second transcription failed and the live text was kept. |

## `marks.jsonl` (bookmarks)

Append-only, one JSON object per line, flushed per line. Created on the first bookmark: a meeting
without bookmarks has no file. It is never deleted by the after-Stop pass; it belongs with the
transcript. Bookmarks can also be added, labelled, retyped or deleted after the meeting, so a reader
should apply the events rather than trust a count.

```json
{"type":"mark","id":1,"kind":"decision","at_ms":381200,"at":"2026-11-09T11:06:24-06:00","via":"hotkey","changed_at":"2026-11-09T11:06:24.512-06:00"}
{"type":"mark","id":2,"kind":"bookmark","at_ms":512000,"at":"2026-11-09T11:08:35-06:00","via":"line","seq":44,"changed_at":"2026-11-09T11:08:40.020-06:00"}
{"type":"mark_label","id":1,"label":"Ship Friday","changed_at":"2026-11-09T11:09:02.300-06:00"}
{"type":"mark_kind","id":2,"kind":"action","changed_at":"2026-11-09T11:09:10.004-06:00"}
{"type":"mark_delete","id":2,"changed_at":"2026-11-09T12:15:47.881-06:00"}
```

| Field | Meaning |
| --- | --- |
| `id` | 1-based, increasing within the meeting; later events refer to it. An id is never reused. |
| `kind` | `bookmark` (plain), `decision`, `action`, `question`, or a user-defined type id (lower-case letters, digits and `-`, up to four in Settings; made once from the name the user gave it, e.g. `follow-up-with-legal`, and stable when the type is renamed; its display name is in `meta.json` `mark_kinds`). **Treat a kind you do not know, or that `mark_kinds` does not name, as `bookmark`.** |
| `at_ms` | Meeting clock (same as `start_ms` in the transcripts) at the press, or the line's `start_ms` for a line bookmark. The line being spoken at the press is written 1-2 s later, so place a bookmark by `at_ms`, not by file order. |
| `at` | Wall-clock time of the press (or of the line, for a line bookmark). |
| `via` | `hotkey`, `button` (window), `tray`, or `line` (right-click on a line). |
| `seq` | Only for `via: "line"`: the line's `seq` in the file that was shown. The live and final files number lines differently, so rely on `at_ms`. |
| `changed_at` | Optional, additive, on every event. Wall-clock time the event was written (ISO 8601 with milliseconds and UTC offset), each strictly later than the one before; compare as times, not as text (the offset changes with daylight saving). Unlike `at`, which for a line bookmark is the line's time, this is when the change was made. **The last line's `changed_at` is when the bookmarks last changed.** It never changes the transcript version. Absent in files written before it existed. |

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

## `echo_dropped.jsonl` (mic lines dropped as echo)

A diagnostic: the mic lines the echo dedupe left out of the transcript, so a false drop (a line the user
really said) can be checked after the meeting. Append-only, one JSON object per line, flushed per line as
each line is dropped, so it survives a crash. Created on the first drop: a meeting with no drops (or with
echo dedupe off) has no file. It is never deleted by the after-Stop pass. Nothing in it is in
`transcript.jsonl` or `transcript.final.jsonl`.

```json
{"type":"echo_drop","start_ms":512000,"end_ms":513100,"text":"Yeah.","overlap":1.0,"matched":[{"start_ms":511800,"end_ms":514000,"text":"Yeah, that works for me."}]}
```

| Field | Meaning |
| --- | --- |
| `start_ms`, `end_ms`, `text` | The dropped mic line, on the meeting clock (same as `start_ms` in the transcripts). |
| `matched` | The `system` lines it was compared with (those within 1 s of it), in time order, with their times and text. |
| `overlap` | The share of the mic line's words found, in order, in the matched lines' words. A line is dropped at 0.6 or more, or at 1.0 when it has two words or fewer. |

Each drop is also written to the app log at INFO level.

## `heartbeat.json` (is the recorder alive?)

Additive to schema 1 (absent in meetings recorded before it). Written into the folder with `meta.json`,
before the folder appears, then replaced every **2 s** while the meeting records, by a timer of its own:
it goes on in total silence and while the mic is muted, and while Stop finishes the live transcript. It
stops once Stop has set `status` to `processing`; a crash stops it too, and then `meta.json` keeps
`status: "recording"` until the app next starts. The file stays in the folder with its last beat.

```json
{"at":"2026-11-09T11:42:31.207-06:00"}
```

| Field | Meaning |
| --- | --- |
| `at` | Wall-clock time of the last beat, ISO 8601 with milliseconds and UTC offset. |

Replaced atomically (temp file `heartbeat.json.tmp` + rename); ignore the temp file. Compare `at` with your
own clock (same PC); do not use the file's modification time.

**Telling the three states apart.** Read `status` first, then the heartbeat:

| `status` | Heartbeat | State |
| --- | --- | --- |
| `recording` | `at` within **15 s** of now (either way) | **Recording.** The meeting is active, even if nobody has spoken for a long time: a quiet transcript is not a fault. |
| `recording` | older than 15 s, more than 15 s in the future, missing or unreadable | **Recorder died.** Never treat it as active, however recently it started. The next app start marks it `interrupted` and finishes it. |
| anything else | ignored | **Not recording.** Stopped (final pass running), done, interrupted or failed. |

15 s is about seven missed beats, so a slow disk or a reader holding the file open for a moment does not trip
it. On Windows, open the file, read it and close it straight away: while it is open the recorder cannot
replace it (it tries again at the next beat).

"Recorder died" is what the files show, not a verdict: if the PC sleeps mid-meeting the heartbeat goes stale and
starts again on wake, with `status` still `recording`. Ask again before acting on a dead recorder; once the app
has started again the meeting is `interrupted`.

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
 "names_changed_at": "2026-11-09T12:10:00.412-06:00",
 "updated_at": "2026-11-09T12:10:00-06:00"}
```

Keys are the labels from `transcript.final.jsonl`. Transcript files are never rewritten with names;
apply the mapping when you read.

| Field | Meaning |
| --- | --- |
| `speakers` | Final label to display name. A speaker without an entry has no name (keep showing the label). |
| `from_tags` | Optional, additive. `true` when the after-Stop pass wrote the names from `speaker_tags.jsonl`. |
| `conflicts` | Optional, additive. Final speakers that hold the speech of two named people: `{"speaker", "names": {name: ms}}`, milliseconds of that person's tagged speech inside the speaker, largest first. The speaker is listed under the largest name in `speakers`. A second name is reported when it has at least 25% of the largest one's overlap. The user is shown these in the window; a downstream app can treat the speaker as uncertain. |
| `names_changed_at` | Optional, additive. When `speakers` last changed (ISO 8601 with milliseconds and UTC offset, strictly later than the time before; compare as times, not as text). Huddle Helper moves it only when a name is added, changed or removed, not when it writes the same names again, and never with the transcript version: a reader holding quotes re-applies the names without treating the quotes as outdated. Absent until Huddle Helper first changes a name. A downstream app that changes `speakers` should set it too. |
| `updated_at` | When the file was last written. |

How the names are made: the pass sums, for each final speaker, the overlap in ms between its segments and
each name's tagged lines on the same stream, and the largest wins. The tags also steer the clustering
itself: two lines the user gave different names are never put in the same final speaker, lines the user gave the
same name always are (even if their voices differ; only lines the user named are moved, and a speaker-level
name counts only for pieces at least 0.7 like the rest of that name's pieces), and a speaker
holding a named line is never folded into a speaker that is unnamed or has another name. The names themselves come from overlap, and one name names one final speaker per stream (the one overlapping it most), so
the pieces a speaker-level name left out (below 0.7) cannot give a second final speaker the same name. Two
entries in `speakers` may still hold the same display name: on different streams, or when set after `DONE`. With no `speaker_tags.jsonl` the pass writes no
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

## Reading a meeting from another app

The files above are the contract. The supported way to read them without writing your own tailer is one
read-only command that prints a bounded window of one meeting as JSON on stdout (ADR 0001):

```text
huddle-helper context [--meeting latest|active|<meeting id>] [--transcript auto|live|final]
                      [--last SECONDS | --from SECONDS --to SECONDS | --bookmark ID | --whole]
                      [--meetings-dir DIR] [--pretty]
```

In the installed app the console program is `huddle-helper-cli.exe` (the tray program has no console).
It never writes to the meeting folder. Exit code 0 when `ok` is `true`, 1 when the answer is an error.

| Option | Meaning |
| --- | --- |
| `--meeting latest` (default) | The **active meeting** if there is one, else the most recently started meeting in whatever state it is in (still in its final pass, done, interrupted, failed). A meeting that started seconds ago *is* the latest, even if nearly empty. `meeting.selected_as` says which of the two you got: `active` or `latest` (`id` for a named meeting). |
| `--meeting active` | Only a meeting whose `status` is `recording` **and** whose heartbeat is fresh. With none: `"meeting": null`, `"no_active_meeting": true`, and `just_ended` names the most recently started meeting (id, title, state, times), or is `null` if there are no meetings. A stopped meeting, or one whose recorder died, is never returned as active. |
| `--meeting <id>` | That meeting. |
| `--transcript auto` (default) | The final transcript once `DONE` exists, else the live one. `live` or `final` on request (`final` before it exists is the error `final_not_ready`). |
| (no window option) | The last **3 minutes**: up to now on the meeting clock for the active meeting, else up to the end of the meeting. |
| `--last SECONDS` | The last SECONDS. |
| `--from S --to S` | Meeting time S to S (seconds since the meeting started, i.e. `start_ms / 1000`). |
| `--bookmark ID` | **3 minutes before to 30 seconds after** the bookmark's `at_ms`. |
| `--whole` | The whole meeting, with no cap. |

`--last` and `--from`/`--to` are capped at **15 minutes** (`window.capped` is then `true`; a `--from` window
keeps its start). Ask for `--whole` deliberately when you need more.

An answer (made-up data, shortened):

```json
{"ok": true, "generated_at": "2026-11-09T11:20:00.120-06:00",
 "meeting": {"id": "2026-11-09_1100_weekly-sync", "title": "Weekly sync", "state": "recording", "active": true,
             "started_at": "2026-11-09T11:00:03-06:00", "ended_at": null, "selected_as": "active",
             "heartbeat": {"state": "active", "last_beat": "2026-11-09T11:19:59.204-06:00", "age_s": 0.9}},
 "transcript": {"kind": "live", "version": null, "lines": 212, "start_ms": 1200, "reaches_ms": 1195400,
                "finished": false},
 "window": {"mode": "last", "start_ms": 1017000, "end_ms": 1197000, "capped": false,
            "incomplete": true, "incomplete_reasons": ["still_recording"]},
 "lines": [{"stream": "system", "start_ms": 1190100, "end_ms": 1195400, "text": "Legal still needs one more day.",
            "confidence": 0.88, "speaker_label": "SPEAKER_02", "speaker_name": "Dana",
            "name_from": "speaker_tags.jsonl"}],
 "speakers": {"conflicts": [], "names_changed_at": null},
 "bookmarks": [{"id": 3, "type": "decision", "type_name": "Decision", "label": "Ship Friday", "at_ms": 1101000}],
 "bookmarks_changed_at": "2026-11-09T11:18:40.020-06:00",
 "gaps": [{"stream": "mic", "start_ms": 1150000, "end_ms": null, "reason": "muted", "deliberate": true, "open": true}],
 "attendees": {"names": ["Dana", "Luis"],
               "meaning": "Names typed in the Start dialog to make naming speakers quicker. Not a record of who attended, and not who spoke."},
 "warnings": []}
```

| Field | Meaning |
| --- | --- |
| `meeting.state` | `recording` (active), `recorder_died` (status `recording` but the heartbeat is stale or missing), or the `meta.json` `status` (`processing`, `complete`, `interrupted`, `failed`). `heartbeat` is `heartbeat.json` compared with the clock of the PC. |
| `transcript` | `kind` is `final` or `live`. `version` is the final transcript's version (`null` for live, and for final files older than the field); a version different from the one you quoted means the wording or timing may have changed. `lines`, `start_ms` and `reaches_ms` (end of the last complete line) describe the whole transcript, not the window; for live, `reaches_ms` and `finished` say how far it goes. |
| `window` | The span chosen, in meeting ms. `incomplete` is `true` when audio was lost in it (`lost_audio`: a gap whose reason is not `muted`), when the meeting is still recording past its last written line (`still_recording`: lines arrive 1-2 s late), or when the recorder died before the window's end (`recorder_died`). A muted gap alone does not make a window incomplete. |
| `lines` | Every line overlapping the window, both streams in time order, text exactly as written (no summaries, no rewriting). `speaker_name` is `null` when nobody named the speaker; `name_from` is `speaker_tags.jsonl` (live: the user's tags), `speakers.json` (final), or `null`. Final answers only: `live_wording` (see "Where live and final disagree"). |
| `speakers` | `conflicts`: the `speakers.json` conflicts for speakers in the window. `names_changed_at`: from `speakers.json` (final only). |
| `bookmarks`, `bookmarks_changed_at` | Bookmarks in the window, all events applied, placed by `at_ms`. `type` is normalised (an unknown kind reads as `bookmark`); `type_name` is its display name. `bookmarks_changed_at` is the last `changed_at` in `marks.jsonl`. |
| `gaps` | Gaps overlapping the window; `deliberate` is `true` for `muted`. A mute still in progress (`mic_muted_since_ms`) is an `open` gap with `end_ms: null`. |
| `attendees` | `meta.json` `attendees`, labelled: not who attended, not who spoke. |
| `error` | Only when `ok` is `false`: `{"code", "message", "file"?}`. Codes: `unreadable` (a file could not be read; `file` names it), `not_found`, `no_meetings`, `final_not_ready`, `bookmark_not_found`, `bad_request`. |
| `warnings` | Files that could not be read but did not stop the answer: the `meta.json` of a meeting older than the one chosen (one that may be newer is the error `unreadable`), or this meeting's `marks.jsonl`, `speaker_tags.jsonl` or `speakers.json`, or (final answers) its `transcript.jsonl`. |

**Following a growing meeting.** Run the command again; every answer is a fresh read. A half-written last
line is left out until its newline arrives. When `transcript.kind` turns from `live` to `final`, or
`transcript.version` changes, text you hold may have changed: read it again.

**Find lines by time, never by `seq`.** The live and final files number their lines differently, and every
final pass numbers them anew. A line is `(stream, start_ms)`; a bookmark is its `at_ms` (the `seq` of a line
bookmark points into whichever file was shown when it was made).

**What the text is and is not.**

* "Final" does not mean verified. The final transcript is the source of truth once the meeting is done, but
  live and final may disagree, and either may be wrong.
* `confidence` comes from the speech model's token probabilities. It is not a calibrated probability that
  the words are right.
* Speaker names are display labels the user (or a downstream app) gave a speaker label. They are not
  confirmed identities, and not recipients to send anything to.
* Attendees are the names typed at Start. They do not say who was on the call or who spoke.

**Where live and final disagree.** When the answer is the final transcript, every line has `live_wording`:
`null` when the live transcript said the same words there (case and punctuation aside) or had no line at
that time on that stream, else what the live transcript said instead (made-up data):

```json
{"stream": "system", "start_ms": 10000, "end_ms": 13000, "text": "We shipped 32 items today.",
 "confidence": 0.71, "speaker_label": "SPEAKER_01", "speaker_name": null, "name_from": null,
 "live_wording": {"text": "We shipped our two items today.", "start_ms": 10000, "end_ms": 13000,
                  "flags": ["number"]}}
```

* Live lines are matched to the final line by time on the same stream (they overlap it), never by `seq`.
  `text` is the live words that line up with this final line; `start_ms`/`end_ms` span the live line(s)
  they come from. Live words at this line's start or end that nothing here stands in for, and whose estimated
  time (a live line's words spread evenly over it) falls outside this line, are left to the neighbouring
  final line.
* `flags` say what kind of words differ, on either side, in this order (empty when none applies):
  `number` (digits or number words: "two", "hundred", "half"), `negation` ("not", "no", "never", "can't"…),
  `name` (a capitalised word that does not start a sentence: a name, product or team, or just a different
  word), `date_time` (days, months, "tomorrow", "morning", clock times such as 3:15, ordinals such as 15th).
  They are coarse word lists, not understanding.
* It is a hint, not a verdict: it never says which wording is right. Either may be wrong, and the final
  line is still the transcript's text. Neither file is changed.
* A low `confidence` cutoff alone does not find these: a final line that changed a number can have middling
  confidence. Check `live_wording` for lines whose numbers, negations, names or dates matter.
* Live answers have no `live_wording`. If the live file cannot be read, every `live_wording` is `null` and
  `warnings` names the file.

**Vocabulary.** The user adds terms that help speech recognition in Settings → Vocabulary, and for one
meeting in the Start dialog ("Extra terms for this meeting"). `meta.json` `vocabulary` shows the terms a
meeting actually used. The downstream app does not supply vocabulary.
