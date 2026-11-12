# Transcript Window: Live View, Speakers, Bookmarks and Meetings (draft)

Nov 9, 2026 · @Bryce · Status: draft for discussion

## Overview

Add a window to Huddle Helper that shows the transcript as it is written, lets the user
name speakers while the meeting is happening ("that's Eric"), and keeps showing the
transcript while the after-Stop pass refines speaker labels. Today the only way to see
the transcript is to open `transcript.jsonl`, and names can only be added afterwards with
**Rename speakers…**, which shows two sample lines per speaker.

**Problem.**
- Speaker labels like `SPEAKER_03` mean nothing to the downstream app or the user. The
  person best placed to name them is the user, during the meeting, while they can still
  hear who is talking. By the time the final file exists, they have to recognise people
  from two text samples.
- The user has no way to see that recording works (lines arriving, both streams heard, no
  lag) without opening files.
- After Stop, the tray only says "Finalizing: <meeting>". Refined labels can take minutes
  on a laptop, with no view of progress or of what changed.
- There is no quick way to flag "something important just happened" during a meeting;
  the user has to remember the moment and search for it later.
- Once recording starts, everything the mic hears is transcribed. The user can't take
  a side conversation or a private aside off the record without stopping the meeting.
- Whisper doesn't know the team's own words. In Test #4 (a real work call) base.en heard
  internal acronyms as everyday phrases or made-up words, "ETL" as "ETO" and "spreadsheet"
  as "spray cheat", so whole sentences lost their meaning for the downstream app. The user has no
  way to tell it which acronyms, product names and people's names to expect.
- Sharing a transcript means handing someone raw JSONL.
- Managing past meetings means Explorer. A meeting whose final pass failed stays
  `failed` with the error only in the log; nothing retries it.

**Scope.** Windows only, same Python/Tk stack as the tray. The window is a viewer plus
speaker naming, one-press bookmarks with types, muting the mic in the recording,
click-to-hear playback of a line, export to plain text and Markdown, a Meetings view
for housekeeping, and a custom vocabulary that helps transcription with the user's own
terms and names. It is not a transcript editor (PRD non-goal) and does no analysis.

## Goals and non-goals

**Goals**
- Show live transcript lines within a second of their being written, for both streams,
  in time order, with speaker labels.
- Let the user attach a name to a speaker, or to one line, during the meeting, in one or
  two clicks.
- Carry those names through the after-Stop pass to the refined labels, even though the
  refined pass renumbers speakers.
- Show the after-Stop pass's progress, then swap in the final transcript.
- Make the names available to the downstream app live, through a documented file, without
  changing the existing files' meaning.
- Let the user drop a bookmark with a single press, without leaving the call app, and
  say what kind of moment it was (decision, action item, question).
- Let the user take their own mic out of the recording and put it back, with one press,
  without stopping the meeting.
- Export any meeting as a readable `.txt` or `.md` file, with names and bookmarks.
- Let the user click a line to hear it, during the meeting and afterwards while the
  audio exists, so they can tell who spoke and check what was really said, without the
  clip being recorded again or heard by the call.
- Let the user see, retry, clean up and delete past meetings from one place.
- Let the user list the terms, acronyms and names their meetings use, so transcription
  spells them right, without slowing live lines past the PRD's latency target.

**Non-goals**
- Editing transcript text, or rewriting `transcript.jsonl` / `transcript.final.jsonl`.
- Remembering voices across meetings (see brainstorm; needs its own privacy review).
- Summaries, action items, talk-time analysis (downstream app).

## User stories

1. **Watch it work.** I start recording and the transcript window opens. I see my lines
   (`You`) and the call's lines appear a second or two after each person stops talking.
2. **Name a new voice.** A new label `SPEAKER_04` appears with a "Who is this?" chip. I
   click it, type or pick "Eric", and every `SPEAKER_04` line, past and future, shows
   "Eric".
3. **Fix one line.** The live pass put one of Dana's lines under Eric. I right-click it,
   choose "This line is… Dana". Only that line changes in the view, and it is a strong
   hint for the final pass.
4. **Same person, two labels.** The live pass split Eric into `SPEAKER_04` and
   `SPEAKER_06`. I name both "Eric" and the view shows them as one person.
5. **Watch it finish.** I click Stop. The window shows "Refining speaker labels 40%".
   When the final file is ready, the view switches to it, still with my names, and
   briefly highlights lines whose speaker changed.
6. **Review later.** I open a past meeting from the window and read it with names; I can
   still name speakers there (this replaces the current Rename speakers dialog).
7. **Bookmark a moment.** Someone says "OK, decision: we ship Friday." I press the
   Decision hotkey without switching away from the call. A Decision bookmark appears in
   the transcript at that point. Later I can add a label to it ("Ship Friday"), change
   its type, or leave it as is. When I'm not sure what kind of moment it is, the plain
   bookmark hotkey just marks it.
8. **Share the transcript.** After the meeting I click Export, pick Markdown, and get
   a file with the meeting title, date, speakers by name, the transcript, and my
   bookmarks, ready to paste into notes or send.
9. **Hear it.** A line reads "Gitli Cluster" and I'm not sure who said it. I click its
   play button and hear the 4 seconds of audio: it was Eric, saying "GitLab cluster". I
   name him from there. In the speaker bar, "Hear this voice" on an unnamed speaker plays
   a clear sample of them.
10. **Go off the record.** A colleague walks into my room mid-call with an unrelated
    question. I press the mute hotkey; the tray icon shows a crossed-out mic and the
    window says "You're muted in the recording". The call still hears me (this doesn't
    touch the call app) and the call audio is still recorded. I press it again when
    they leave. The transcript shows "You muted 11:05–11:07" and nothing I said in
    between was recorded or transcribed.
11. **Tidy up.** I open the Meetings view and see every meeting with its date, length,
    status and how much audio it keeps. One meeting says "Final pass failed" with the
    reason; I click Retry and it completes. I delete two test meetings (they go to the
    Recycle Bin) and remove the audio from last month's meetings to free 600 MB.
12. **Teach it our words.** Our calls are full of "KoVa", "QDM" and "Zentrix", and the
    transcript keeps writing "cover" and "Quodium". In Settings → Vocabulary I paste
    our acronyms and product names once. At Start I add two names for this meeting. From
    the next line on, the transcript spells them right. When I spot another miss in the
    window, I right-click the line, choose "Add to vocabulary…" and type the right word.

## Functional requirements

P0 items define this feature's first release; P1 items follow.

| ID | Requirement | Priority |
| --- | --- | --- |
| **Live view, export, speakers, playback** | | |
| L1 | A Transcript window shows the live transcript of the current meeting, both streams in time order, updating within 1 s of a line being written, without pushing live line delay past the PRD target on the target laptop | P0 |
| L2 | The window shows recording health: elapsed time, line count, lag, backlog, device gaps | P0 |
| L3 | After Stop, the window shows the after-Stop pass's progress and an estimate of the time left, then switches to the final transcript | P0 |
| L4 | The window opens any past meeting read-only | P0 |
| L5 | One press adds a bookmark at the current point in the meeting, from the window, the tray menu, or a global hotkey | P0 |
| L6 | Bookmarks are written to `marks.jsonl` in the meeting folder as soon as they are made, and shown in the transcript | P0 |
| L7 | A bookmark can be given a label or removed afterwards; a past line can be bookmarked from its right-click menu | P1 |
| L8 | Export a meeting as `.txt` or `.md`, with speaker names and bookmarks applied | P0 |
| L9 | Export works during the meeting and before `DONE` (from the live file, marked provisional) and after (from the final file) | P1 |
| L10 | `huddle-helper export` does the same from the command line | P1 |
| L11 | Name a speaker or a single line during or after the meeting; names are written to `speaker_tags.jsonl` | P0 |
| L12 | The after-Stop pass turns tags into `speakers.json` for the final labels, and reports conflicts | P0 |
| L13 | Attendee list at Start, number-key shortcuts, "Who is this?" prompt for new voices | P1 |
| L14 | Lines the user named as different people are never put in the same final speaker | P0 |
| L15 | Click a line's play control to hear that line from the buffered audio of its own stream | P0 |
| L16 | Playback works during the meeting, between Stop and `DONE`, and after `DONE` when the meeting's audio was kept; otherwise the control is disabled and says why | P0 |
| L17 | A clip played while recording must not become a transcript line or a voice in the speaker labels; the user is warned that, without headphones, the call can hear it | P0 |
| L18 | "Hear this voice" on a speaker plays a clear sample of them; a bookmark can be played from a few seconds before it | P1 |
| L19 | "Play from here" plays continuously and highlights the line being heard; quiet clips are raised to a normal level; clips can play on a chosen output device | P1 |
| L20 | "Keep this meeting's audio" in the window overrides the Keep audio setting for the current or a processing meeting | P1 |
| **Mute my mic** | | |
| L21 | One toggle takes the user's mic out of the recording and puts it back, from the window, the tray menu, or a global hotkey; the call app's own mic is untouched | P0 |
| L22 | While muted, no mic audio is buffered, transcribed, fingerprinted or written anywhere; the system stream keeps recording; the meeting clock stays aligned | P0 |
| L23 | Muted state is unmistakable: tray icon, tooltip, window header; muted periods are recorded in `meta.json` and shown in the view and exports | P0 |
| L24 | A reminder every 10 minutes while muted; "Start with my mic muted" on the Start dialog | P1 |
| **Bookmark types** | | |
| L25 | Bookmarks have a type: Bookmark (plain), Decision, Action item, Question; each has its own hotkey, and the window button and tray menu offer all four | P0 |
| L26 | A bookmark's type can be changed afterwards; the type is written to `marks.jsonl` | P0 |
| L27 | The view and exports show the type, and the Markdown export groups bookmarks by type at the top | P0 |
| L28 | Up to four user-defined types (name and hotkey) in Settings | P1 |
| **Meetings and housekeeping** | | |
| L29 | A Meetings view lists every meeting with date, title, length, status, speakers, bookmarks and kept-audio size; open, export or show it in Explorer from there | P0 |
| L30 | Delete a meeting, or only its kept audio, to the Recycle Bin, or permanently as a separate choice, after a confirmation that says what will go and whether it can be restored; not allowed while it is recording or processing | P0 |
| L31 | A meeting whose final pass failed shows the error and a Retry button that runs the final pass again, waiting until no meeting is recording | P0 |
| L32 | A Storage panel shows disk use of the meetings folder (transcripts vs kept audio), the auto-purge setting, and "Delete kept audio older than N days" now, with a preview | P1 |
| **Custom vocabulary** | | |
| L33 | A Vocabulary list in Settings (acronyms, product names, people's names, one per line) is given to Whisper for every utterance on both streams; changes apply from the next utterance, including during a meeting | P0 |
| L34 | The Start dialog takes extra terms for this meeting only; attendee names (L13) are added automatically | P0 |
| L35 | The vocabulary a meeting used is recorded in `meta.json`, so a transcript can be traced to the vocabulary it was made with | P0 |
| L36 | Vocabulary never adds words that weren't said: no measurable rise in hallucinated lines, and live line delay stays within the PRD target, measured on kept real audio before release | P0 |
| L37 | "Add to vocabulary…" on a line's right-click menu, with the selected words prefilled for the user to correct | P1 |
| L38 | Import and export the vocabulary as a plain text file, so a team can share one list | P1 |
| **Speaker naming, continued** | | |
| L39 | Lines the user named as the same person always end up in the same final speaker | P1 |
| L40 | Final speakers with little speech are shown dimmed and marked "short", never hidden | P1 |

## The core problem: live labels are not final labels

Live labels come from `OnlineClusterer` (`diarize.py`). The final file is labelled
by clustering every line's voice fingerprint over the whole meeting
(`finalize.py` `_voice_labels`, since 416de3a) and **numbers speakers independently**.
The final pass can also split a line in two or join fragments. So "`SPEAKER_04` = Eric"
from the live file cannot be copied into `speakers.json`, whose keys are final labels
(OUTPUT_SPEC).

**Decision (proposed): a tag is attached to audio, not to a label.** Each tag records the
stream and time range of the line(s) it names. After the final pass, each final speaker
gets the name whose tagged speech overlaps it most.

- Tagging a speaker chip creates one tag per live line with that label, including lines
  that arrive later (until the user changes it). Tagging one line creates one tag.
- Mapping after the final pass: for each final speaker, add up the overlap (ms) between
  its segments and each name's tagged ranges, on the same stream. The name with the most
  overlap wins. If a second name has at least 25% of the winner's overlap, record a
  conflict.
- Conflicts and untagged final speakers are shown in the window after the swap ("Final
  pass put Eric and Dana together: 3 of Dana's lines are under Eric"), so the user can
  re-tag. Untagged speakers keep their `SPEAKER_nn` label.
- Mic: with mic diarization off, every mic line is `You`. Tagging `You` sets a display
  name for the mic (it can default to a "My name" setting).

**Why mapping alone isn't enough: the final pass merges people.** Both real failures so
far were two different people put into one final speaker, not one person split in two:
Test #3 merged B and C, and Test #4 merged Remote D (6 lines, 28 s) into Remote B (377 s).
With mapping alone, if the user names both, the merged speaker gets Remote B's name
(more overlap), Remote D's name is lost, and re-tagging can't help because
the merge happens before names are applied. So names must also steer the clustering.

**Names as rules in the final pass (L14, P0; L39, P1).** The final pass's line
clustering (`cluster_voices`, average linkage) and the small-cluster fold
(`fold_small_clusters`) take the tags as rules:
- **Different names, different speakers (L14).** Two clusters whose lines carry
  different names are never merged, and a named cluster is never folded into a cluster
  with another name. This is the part that fixes Tests #3 and #4: two clicks during the
  call keep Remote D and Remote B apart even when their voices are 0.64–0.75 alike.
- **Same name, same speaker (L39).** Lines with the same name start in one cluster
  before clustering begins (`cluster_voices` merges them first, by the same weighted-average
  update the loop uses), so they always end in one final speaker. This is a hard rule, like
  the different-names rule: if the user gives the same name to lines of two different people,
  those lines are forced together, and the clustering then treats the merged cluster like any
  other (unnamed lines may join it by voice). The rule moves only the lines the user named;
  the rest of the other speaker stays where the voices put it.
- **Limit.** The rules only protect people the user named. An unnamed Remote D can still
  be merged into a named Remote B. Naming every voice live is the defence; how the window asks
  for names is under "Attendees (L13)". The fixed-threshold problem itself is tracked in TODO (Test #4).
- **Which lines are named.** Line-level tags name that line; speaker-level tags name
  every live line with that label. A speaker-level tag on a live label that mixes two
  people (see Risks) would push wrong lines into a cluster, so speaker-level
  tags count as rules only for lines whose voice is at least the voice profile's tag gate
  (`tag_gate`; 0.7 for the old model, 0.40 for ERes2Net) like the rest of that name's
  lines, and only if the line is not more like another name's **line-named** lines; the
  others are left to the clustering. The second condition comes from Test #7: a live label
  named Remote B also held Remote H's update. Remote H's other lines still passed the gate
  (0.41–0.60), because Remote B's average included them. The user named one of Remote H's
  lines, and the rest were far more like that line (up to 0.89). Before this condition,
  the speaker-level name won and pulled them into Remote B.

## Output contract changes

Additive only; schema stays 1. Existing readers are unaffected.

**New file: `speaker_tags.jsonl`** (append-only, flushed per line, same rules as
`transcript.jsonl`). The recorder writes it; the downstream app reads it.

```json
{"type":"tag","at":"2026-11-09T11:05:12-06:00","scope":"speaker","label":"SPEAKER_04","name":"Eric"}
{"type":"tag","at":"2026-11-09T11:06:40-06:00","scope":"line","seq":31,"source":"system","start_ms":381200,"end_ms":384900,"name":"Dana"}
{"type":"untag","at":"2026-11-09T11:07:02-06:00","scope":"speaker","label":"SPEAKER_04"}
```

- `scope: "speaker"` names every line with that **live** label (past and future, until
  changed); `scope: "line"` names one live line (overrides the speaker-level name).
- Later events win. A downstream app following the live file applies these to live
  labels. Once `DONE` exists, it uses `speakers.json` with the final file instead.

**`speakers.json`** keeps its meaning (final label → name). The final pass writes it
from the tags, **before** `DONE`, adding the mapping evidence:

```json
{"speakers":{"SPEAKER_01":"Dana","SPEAKER_02":"Eric"},
 "from_tags":true,
 "conflicts":[{"speaker":"SPEAKER_02","names":{"Eric":41200,"Dana":11800}}],
 "updated_at":"2026-11-09T11:46:02-06:00"}
```

Names the user enters after `DONE` (final view) are written straight to `speakers.json`,
as Rename speakers does today.

**Decision: when both apps write `speakers.json`, the user's names win.** For a speaker
the user named (by tag or in the final view), the user's name is written. For every
other speaker, a name the downstream app set is kept. The file is already replaced
atomically (`write_json_atomic`: temp file + rename), so a reader never sees half a
file. To avoid one writer overwriting the other's names, Huddle Helper re-reads the file
immediately before each write and merges into what it finds. Downstream apps that write
the file should do the same.

Order of the end of the after-Stop pass becomes: final file renamed into place →
`speakers.json` → `meta.json` complete → `DONE`.

**New file: `marks.jsonl`** (append-only, flushed per line). Created on the first
bookmark; a meeting without bookmarks has no file.

```json
{"type":"mark","id":1,"kind":"decision","at_ms":381200,"at":"2026-11-09T11:06:24-06:00","via":"hotkey"}
{"type":"mark","id":2,"kind":"bookmark","at_ms":512000,"at":"2026-11-09T11:08:35-06:00","via":"line","seq":44}
{"type":"mark_label","id":1,"label":"Ship Friday"}
{"type":"mark_kind","id":2,"kind":"action"}
{"type":"mark_delete","id":2}
```

| Field | Meaning |
| --- | --- |
| `id` | 1-based, increasing within the meeting. Later events refer to it. |
| `kind` | `bookmark` (plain), `decision`, `action`, `question`, or a user-defined type id (lower-case letters, digits and `-`, from Settings; its display name is in `meta.json` `mark_kinds`). Readers should treat a kind they don't know as `bookmark`. |
| `at_ms` | Meeting clock (same as `start_ms` in the transcripts) when the button was pressed, or the line's `start_ms` for a line bookmark. |
| `at` | Wall-clock time of the press (or of the line, for a line bookmark). |
| `via` | `hotkey`, `button` (window), `tray`, or `line` (right-click on a line). |
| `seq` | For `via: "line"` only: the line's `seq` in the file shown at the time; `at_ms` is what to rely on, since the live and final files number lines differently. |

`mark_label` sets or replaces a label (empty string clears it); `mark_kind` changes the
type; `mark_delete` removes a bookmark. Readers apply events in order. At Stop,
`meta.json` gets `marks` (count) and `mark_kinds` (`{"risk": "Risk", …}` for any
user-defined types used). The file is never deleted by the after-Stop pass; it belongs
with the transcript.

**`meta.json`: muted periods.** Muting is recorded with the existing `gaps` list, using
a new reason, so readers that already handle device gaps handle mutes too:

```json
"gaps": [{"source":"mic","start_ms":305000,"end_ms":421500,"reason":"muted"}],
"mic_muted_since_ms": null
```

- A `gaps` entry is added when the user unmutes (or at Stop, if still muted), so every
  entry has both ends, as today.
- While muted, `mic_muted_since_ms` holds the meeting time the mute started, so a live
  follower knows right away that mic silence is deliberate. It is `null` otherwise.
- No line is added to `transcript.jsonl`: the contract says everything between
  `meeting_start` and `meeting_end` is a `segment`, and that stays true.
- **Readers must check `reason`.** Until now every gap meant lost audio (a device
  change). A `muted` gap is deliberate: the user chose not to be recorded, so a
  downstream app must not report it as a fault or try to recover it. OUTPUT_SPEC gets
  this note in the same change that adds mute.

**Downstream app changes.** Names (`speaker_tags.jsonl`, `speakers.json` from tags)
and bookmarks (`marks.jsonl`) reach summaries only once the downstream app reads them.
Its reader should be updated alongside milestones 2 and 5, not after; otherwise the
features work in the window but change nothing downstream.

**`meta.json`: vocabulary.** `"vocabulary": ["KoVa", "QDM", "ETL", "Zentrix"]` holds the
terms passed to Whisper (after the token budget; empty list if none), and
`"vocabulary_changed_at_ms": [742000]` lists meeting times when it changed during the
meeting (empty if never). See "Custom vocabulary".

**Deleting meetings.** A meeting folder can now disappear when the user deletes it in
the Meetings view (and its `audio\` folder when they delete kept audio). Downstream apps
must tolerate a folder vanishing, including one they haven't processed yet. The
Meetings view can't know what the downstream app has done, so it warns more strongly
when the meeting finished in the last 24 hours.

## Bookmarks

**One press, no focus change.** The point is to mark a moment without leaving the
call app, so the global hotkeys are the main way in; the window button and tray menu
are for when the user is already there.

**Types.** Each bookmark has a type, so the user (and the downstream app) can tell a
decision from a question at a glance:

| Type | `kind` | Default hotkey | Shown as |
| --- | --- | --- | --- |
| Bookmark | `bookmark` | `Ctrl+Alt+B` | ⚑ Bookmark |
| Decision | `decision` | `Ctrl+Alt+D` | ✔ Decision |
| Action item | `action` | `Ctrl+Alt+A` | ☐ Action item |
| Question | `question` | `Ctrl+Alt+Q` | ? Question |

The plain Bookmark is for "something happened, I'll sort it out later"; it should stay
the easiest to hit. Up to four user-defined types (P1, L28, built in WP9c) can be added in
Settings → Bookmark types with a name and an optional hotkey, e.g. "Risk" or "Follow up with legal".
Names are 1-24 characters, not the same as a built-in or another custom type, and the hotkey must not
clash with any other configured hotkey (the AltGr check applies). They appear in the window's Bookmark
button, the mark editor's type list, the tray submenu, the hotkeys (only while recording), the view
(a grey ◆, one neutral colour for all of them) and the exports (Markdown groups: the four built-in types,
then the custom ones in Settings order). `marks.jsonl` holds the type's *id* (`risk`, made from the name once);
the name is copied into the meeting's `meta.json` `mark_kinds` when first used, so a type removed or renamed
in Settings later still reads with its original name in the meetings that used it.

- **Hotkeys.** All changeable in Settings (or turned off one by one). Registered with
  the Windows `RegisterHotKey` API only while recording, so they can't clash with other
  apps the rest of the time. If registration fails (another app owns the combination),
  Settings says so and the button and tray menu still work.
- **AltGr.** On many non-US keyboards `Ctrl+Alt` is the same as AltGr, and AltGr+letter
  types a character (AltGr+Q is `@` on a German keyboard, AltGr+M is `µ`). A
  registered hotkey would swallow it, so typing an email address in the call's chat
  would drop a Question bookmark instead. Settings checks each hotkey against the
  current keyboard layout (`ToUnicodeEx`) and, if it produces a character, warns and
  suggests `Ctrl+Alt+Shift+` the same letter.
- **Window and tray.** The window's Bookmark button is a split button: click for a plain
  bookmark, the arrow for the other types. The tray menu has a **Bookmark** submenu with
  all types.
- **Feedback without noise.** The tray icon shows a flag for about a second and its
  tooltip counts bookmarks ("● Recording 12:41 · 3 bookmarks"). **No sound:** it would
  be captured by the system-audio stream and end up in the recording.
- **Double presses.** A press of the same type within 2 s of the previous bookmark is
  ignored, so a nervous double-tap makes one bookmark. A press of a *different* type
  within 2 s changes the previous bookmark's type instead (`mark_kind`), so "oops,
  that was a decision" is one more press.
- **Where it lands.** `at_ms` is the meeting clock at the press. Lines land in the file
  about 1–2 s after the speaker pauses, so the line being spoken at the press usually
  appears *after* the bookmark is drawn; the window places bookmarks by `at_ms`, so it
  ends up next to the right line as lines arrive. Nothing tries to guess how far back
  the "moment" was; the export shows the surrounding lines.
- **Labels and types later.** In the window, click a bookmark to add or edit a label,
  change its type, or delete it. Labelling is optional; an unlabelled bookmark is still
  useful.
- **After the meeting.** Right-click any line in a past or final transcript → "Bookmark
  this line as…" with the types (`via: "line"`, `at_ms` = the line's start).

## Mute my mic

**What it does.** Takes the user's microphone out of *Huddle Helper's recording*. It
does not mute the user in the call app: people on the call still hear them. The UI says
so every time it shows the control ("Mute me in the recording"), because confusing the
two either way is bad. The system stream (the call) keeps recording.

- **One press.** Window header button, a **Mute me in the recording** check item in the
  tray menu, and a global hotkey (default `Ctrl+Alt+M`, same rules and AltGr check as
  the bookmark hotkeys). Press again to unmute.
- **Nothing is kept.** From the press, mic samples are replaced with silence *before*
  the audio buffer, the in-memory playback window (click-to-hear), VAD, transcription
  and voice fingerprinting, so the muted audio is never written, analysed or played
  back, not even in `audio\mic.flac`. Replacing (rather than
  stopping capture) keeps the mic and system streams on one clock, the way device gaps
  are handled today (`audio/aligner.py`).
- **Speech in progress.** Whatever the user said up to the press is kept: the silence
  ends the utterance through the normal end-of-speech rule and it is transcribed as
  usual. Unmuting starts fresh.
- **Shared mic.** When the meeting was started with "Several people on my mic" (a shared
  mic), the mic is the whole room, so muting takes everyone in the room out of the
  recording. The control's tooltip says so for such a meeting.
- **Can't be missed.** Tray icon: the red recording icon with a crossed-out mic.
  Tooltip: "● Recording 12:41 · your mic is muted". Window header: an amber "You're
  muted in the recording" bar with an Unmute button. The mic level meter shows "muted"
  instead of a level. No sound on mute or unmute (it would be recorded on the system
  stream).
- **Reminders (P1, built in WP9c).** Every 10 minutes of *continuous* mute, a tray notification: "Your mic is
  still muted in the recording (20 min)." (the app's normal notification, no sound of its own, no focus
  change). The window's amber bar changes at the same moment to "Muted for 20 min. …" in stronger colours
  for a minute. Unmuting starts the count over, and nothing is sent after Stop. The interval is
  `mute_reminder.INTERVAL_S`; only the test seam `HUDDLE_HELPER_MUTE_REMINDER_S` changes it. There is no "you seem to be talking" detection:
  that would mean analysing the audio the user asked not to record.
- **Start muted (P1, built in WP9c).** A "Start with my mic muted" check box on the Start dialog, clear
  every time (not remembered), for meetings where the user only wants the call recorded. The gate is closed
  from the first sample, `meta.json` has `mic_muted_since_ms: 0` and the muted gap starts at 0; the tray
  icon, tooltip and the window's bar show it from the start.
- **Ends with the meeting.** Stop always unmutes; the next meeting starts unmuted unless
  "Start muted" is ticked. Crash recovery closes an open mute as a gap ending at the
  last audio.
- **Shown in the transcript.** The view draws a row "— You muted 11:05–11:07 (2 min) —"
  from `meta.json` `gaps`; exports include the same line. Echo dedupe and mic speaker
  labels need no change: a muted mic produces no lines.

## Export (`.txt` and `.md`)

From the window's **Export…** button (and the meetings list), plus
`huddle-helper export <meeting folder> --format md|txt [--out FILE]`.

- **Which transcript.** The final file once `DONE` exists. Before that, the live file,
  with "Provisional: speaker labels may change" at the top.
- **Names.** Final file: `speakers.json`. Live file: `speaker_tags.jsonl` applied to live
  labels. Unnamed speakers keep `SPEAKER_nn`; the mic is `You` or the user's name.
- **Paragraphs.** Consecutive lines from the same speaker are joined into one paragraph
  with the first line's time (option, on by default), so the export reads like a
  conversation rather than a list of utterances.
- **Times.** Choice of time since start (`00:12:41`, default) or clock time (`11:12`).
- **Bookmarks** appear in place, by `at_ms`, with their type and label, and are also
  listed at the top in Markdown, grouped by type (Decisions, Action items, Questions,
  then Bookmarks and user-defined types), with links to their place in the file.
- **Muted periods and device gaps** appear in place as one line each.
- **Where it goes.** A Save As dialog, defaulting to Documents and the file name
  `<meeting_id>.md` / `.txt`. Exports are not written into the meeting folder, so the
  folder's contents stay exactly what OUTPUT_SPEC describes. "Copy to clipboard" offers
  the same text without a file.
- **Encoding.** UTF-8; `.txt` with Windows line endings so Notepad shows it right.

Markdown:

```markdown
# Weekly sync

2026-11-09, 11:00–11:42 (42 min) · Speakers: You, Dana, Eric

## Bookmarks

### Decisions
- [00:06:21 Ship Friday](#bookmark-1)

### Action items
- [00:09:02 Dana to chase legal](#bookmark-3)

### Bookmarks
- [00:08:32](#bookmark-2)

## Transcript

**00:00:01 You:** Let's start with the launch checklist.

**00:00:05 Dana:** Legal still needs one more day.

*00:05:05–00:06:05: you muted your mic in the recording (1 min).*

**00:06:18 Eric:** OK, decision: we ship Friday.

<a id="bookmark-1"></a>
> **Decision 00:06:21: Ship Friday**
```

Plain text:

```text
Weekly sync
2026-11-09, 11:00–11:42 (42 min)
Speakers: You, Dana, Eric

[00:00:01] You: Let's start with the launch checklist.
[00:00:05] Dana: Legal still needs one more day.
(00:05:05-00:06:05 you muted your mic in the recording, 1 min)
[00:06:18] Eric: OK, decision: we ship Friday.
---- Decision 00:06:21: Ship Friday ----
```

A bookmark is placed after the paragraph that contains its `at_ms` (or, if it falls in a
pause, before the next paragraph), so it sits next to the line it was about even though
that line started a little earlier.

## Click to hear

**What plays.** The line's own stream (`mic` or `system`), from `start_ms − 300` to
`end_ms + 300` ms so the first and last words aren't clipped. Audio is 16 kHz mono, so
it sounds like a phone call, which is enough to recognise a voice and a word. Quiet clips
(the quiet-mic problem from Test #2) are raised to a normal level for playback only
(`leveling.py`): the clip's *speech level* (rms of its active 20 ms steps) is moved to
-20 dBFS, never lowered, with a boost of at most +24 dB; the boosted noise floor is kept under
-30 dBFS; a clip with nothing standing out of its noise (or below -54 dBFS) is not raised at
all; a soft limiter keeps the result under 1.0 without flattening peaks. Measured on Tests
#2 and #3: 67 of 72 clips land within 1 dB of the target, 4-5 mic clips (the quietest, at
-45 to -55 dBFS) hit the cap and stay quieter. Clicking another line
stops the current clip; `Space` replays the selected line, `Esc` stops.

**When audio exists.**

| Meeting state | Audio source | Available |
| --- | --- | --- |
| Recording | In-memory recent audio, then the FLAC buffer (below) | Yes |
| Stopped, final pass running | `audio\mic.flac`, `audio\system.flac` | Yes |
| `DONE`, Keep audio on (or kept for this meeting) | Same | Yes, until deleted or auto-purged |
| `DONE`, Keep audio off | Deleted by the final pass | No: the play control is greyed out with "Audio was deleted after the final transcript. Turn on Keep audio to hear lines later." |

Because "can I hear it later?" depends on a setting made before the meeting, the window
offers **Keep this meeting's audio** (L20) while recording or processing. It writes
`keep_audio: true` into `meta.json` `settings`, which the final pass already reads for
that meeting.

**Reading audio that is still being written.** The FLAC buffer stays open until Stop
(`audio/buffer.py`), and libsndfile doesn't guarantee that another reader sees complete
frames. So:
- The recorder keeps the **last 10 minutes of each stream in memory** (16-bit, about
  19 MB per stream) and plays recent lines from there instantly. This covers the main
  use: naming who just spoke, checking a line that just appeared.
- For older lines during the meeting, a spike decides between (a) calling
  `SoundFile.flush()` every few seconds and reading the file through a second handle up
  to the last flushed frame, or (b) writing the buffer as 5-minute chunk files that are
  closed as they fill. (b) changes the internal `audio\` layout, so the final pass and
  Keep audio must read chunks. Until the spike lands, older lines during the meeting
  show "available after Stop".

**Not recording the clip again.** System audio is captured by WASAPI loopback on the
playback device, so a clip played there during the meeting would be captured, transcribed
as a new line at the current time, and fed to the speaker labels. Without headphones the
mic would also hear it, and so would everyone on the call. Three layers:

1. **Play somewhere that isn't captured (preferred, spike).** Windows 10 2004+ can
   capture system audio from every process *except* a given process tree
   (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS` with `PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE`).
   Switching the system stream to that mode, excluding Huddle Helper, removes the
   problem at the source. PyAudioWPatch doesn't expose it, so it needs new capture code
   (ctypes/comtypes, like `endpoints.py`). It also changes how device switches are
   handled, so it must pass the device-switch and soak tests again.
2. **Drop it (first release).** Until (1) lands, every clip played while recording is
   logged as a playback window `{start_ms, end_ms, text}` on the meeting clock. A
   system or mic utterance that overlaps a playback window and repeats the clip's words
   (`echo.py` `overlap_fraction`, same threshold) is dropped and counted, the same way
   echo is handled today. Its audio is not fed to the live speaker clustering. Recorded
   in `meta.json` as `playbacks` (count) and `playback_segments_dropped`.
3. **Warn.** The first clip played while recording shows, once per meeting: "Use
   headphones: without them, people on the call will hear this clip." If the echo
   dedupe has already dropped mic lines (a sign there are no headphones), the warning
   asks to confirm before playing. A **Play clips on** setting can send clips to a
   different output device, such as laptop speakers while the call is on a headset.

Layer 2 has a known gap: if a remote person speaks over the clip, the merged utterance
may be dropped too, or kept with clip words in it. That is why layer 1 is the real fix.

**Other ways in.**
- **Hear this voice** (speaker chip): plays the longest line of that speaker with high
  confidence, preferring 3–8 s. Live labels during the meeting, final labels after.
- **Bookmark:** play from 10 s before the bookmark to 5 s after.
- **Play from here** (L19, line menu): plays the line and every line after it, in transcript
  order, each from its own stream (overlapping lines of the two streams play one after the
  other, not mixed), on one output stream, and highlights the line being heard (the same
  row background as a single clip; its play control shows the stop square). The lines are
  those at the moment of the click: a run never follows lines that arrive while it plays, so
  it cannot feed on its own sound. Lines whose audio is not available (older than the 10 minutes
  held in memory, no file for that stream) are left out. Silence is not played: each clip has
  120 ms of padding (not 300 ms, which would play the same sound twice) and the pause between
  two lines is their gap minus that padding, at most 0.4 s, so a two-minute silence plays as a
  0.4 s pause. The view scrolls the heard line into view until the user scrolls themselves
  (wheel, scrollbar, navigation keys); a new run follows again. It ends after the last line, on
  `Esc`, on the stop square of the line being heard, when another clip is played, when the
  window leaves the meeting or the app quits, or when the capture re-opens its devices. A
  run that is still going when recording stops carries on from the kept audio files (decided in #18). If those files
  go while it plays (Keep audio off removes them after the final pass, or the audio or meeting is deleted), each clip
  that can no longer be read is skipped and the run ends quietly as finished: no error, no notice. Only a run that
  could read nothing at all says "Could not read that audio." While recording every clip of
  a run is a playback window like a single clip (drop rule, `playbacks` counts clips, the
  headphone warning once per meeting), plus a *joined* window over the clips of the last 12 s,
  because clips with under half a second between them come back through the loopback as one
  utterance holding the words of several, which no single clip's window would match.
- **Play clips on** (L19, Settings): the playback device, stored by name in
  `clip_output_device` (default: the Windows default output; not the capture's *Meeting audio
  from*). A device that is not connected any more is kept in the list as "(not connected)" and
  clips play on the default output with one notice. The clip's own `PyAudio` instance is opened
  per clip or run and terminated at its end, so the capture's re-bind still sees a fresh
  device list.

## Meetings and housekeeping

A second view in the same window (**Meetings**), also opened from the tray menu
("Meetings…", replacing "Open meetings folder" and "Rename speakers…").

```text
┌ Meetings ──────────────────────────────── 2.1 GB · 1.9 GB of it kept audio [Storage…] ┐
│ Date         Title          Length  Status             Speakers  Marks  Audio        │
│ 11-09 14:41  test-3         1:47    Complete           3         2      12 MB        │
│ 11-09 13:34  test-2-2       3:46    Complete           2         0      21 MB        │
│ 11-08 10:00  weekly-sync    42:10   Final pass failed  –         5      240 MB  [Retry] │
│ 11-07 09:30  standup        14:02   Processing 60%     –         1      80 MB        │
├──────────────────────────────────────────────────────────────────────────────────────┤
│ [Open]  [Export…]  [Show in Explorer]  [Delete audio]  [Delete meeting]               │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

**List (L29).** One row per meeting folder (`mt.iter_meetings`), newest first, read from
`meta.json` (status, length, title), `speakers.json` / speaker count from the final
file, `marks.jsonl`, and the size of `audio\`. Statuses: Recording, Processing (with
the final pass's progress), Complete, Interrupted (recovered), Final pass failed.
Filter by status and search by title. Double-click opens the meeting in the Transcript
view. Multi-select for delete.

**Delete (L30).**
- **Delete meeting** moves the whole folder to the Recycle Bin. **Delete audio** moves
  only `audio\` and sets `audio_kept: false` and `audio_deleted_at` in `meta.json`
  (auto-purge sets `audio_purged_at` the same way). Click-to-hear then explains that the audio was deleted.
- The confirmation names what goes and whether it can come back: "Move 2 meetings
  (test-2-2, test-3), 33 MB, to the Recycle Bin? They can be restored from there until
  the Recycle Bin is emptied." It warns more strongly for meetings finished in the last
  24 hours (the downstream app may not have processed them yet).
- **Delete permanently** is a separate choice (Shift+Delete, or a check box in the
  confirmation), for a sensitive meeting the user wants gone now. Recycle Bin is the
  default, but it means "deleted" transcripts and audio stay on disk until it is
  emptied, and the confirmation says so.
- Not available for a meeting that is recording or processing.
- Recycle Bin via the Windows shell (`SHFileOperationW` with `FOF_ALLOWUNDO`, through
  ctypes; no new dependency). If the meetings folder is on a drive without a Recycle
  Bin (a network share), Windows would delete permanently; the confirmation says
  "permanently" in that case (`FOF_WANTNUKEWARNING`).
- Auto-purge (F14) keeps deleting permanently, as today: it's an explicit retention
  setting, and sending gigabytes to the Recycle Bin wouldn't free any space.

**Retry failed final passes (L31).** Today `housekeeping.find_unfinished` skips
`failed` meetings, so a failed final pass stays failed with the error only in the log
and `meta.json` `error`. The row shows "Final pass failed" with the error on hover and a
**Retry** button that queues `finalize_meeting` for it, exactly as after Stop. It can
only work if the buffered audio is still there (it always is for a failed meeting: the
final pass deletes audio only after success). On the target laptop the final pass takes
about 0.6× the meeting's length (Test #4: 13.5 min for 22.7 min) and competes with live
transcription for the CPU, so a retry requested while a meeting is recording waits and
starts after Stop; the row says "Retry queued until recording stops". Interrupted meetings are already
recovered at start-up and need no button.

**Storage (L32, P1, built in WP9d).** A **Storage…** button in the Meetings view's header opens a
small panel (`gui/storage_dialog.py`; the numbers and the wording are in `storage.py`, no Tk):

- **Disk use.** The size of the meetings folder (the sum of the meeting folders) split into kept audio
  (`audio\`) and "transcripts and the rest", and the five meetings with the most audio. The numbers come
  from the Meetings view's last scan, whose sizes the library measures on a worker thread, so a large folder
  never blocks the window; until the measuring is done the panel says "measuring sizes…". Audio that has just
  been deleted stops counting at once.
- **Auto-delete.** The same setting as Settings → "Delete kept audio after N days" (`auto_purge_days`), with its
  own Save button; 0 means off.
- **Delete kept audio older than [N] days.** **Preview…** lists exactly what would go ("4 meetings, 55 MB:
  kept audio older than 30 days", with each meeting's title, finish date, age and audio size) and the
  wording of the Delete audio confirmation (can it come back, the Recycle Bin check box). The button that
  deletes (default "Move audio to Recycle Bin"; "Delete audio permanently" when ticked or the drive has no
  Recycle Bin) is given exactly the previewed meetings, not a new search. Changing N, or Cancel, drops the
  preview. Only the audio goes; transcripts and bookmarks stay: it is `library.delete_audio` per meeting, so a
  meeting that is recording or being processed is refused, and `audio_kept: false` / `audio_deleted_at` are
  written as in the Meetings list.
- **The age rule is auto-purge's:** a finished meeting (DONE exists) with a non-empty `audio\`, aged by the
  modification time of its DONE file. A meeting that is not finished (failed, interrupted) is never offered, and
  neither is one that is recording, queued or processing. A preview made while some sizes are still being
  measured says so and cannot be run until Preview is pressed again.

## Custom vocabulary

**Why.** Test #4 was a real call about a data platform. base.en got the general speech
right but misheard nearly every domain word: internal acronyms became everyday phrases
or made-up words, ETL → "ETO", vendor → "bender", code freeze → "co-trees",
spreadsheet → "spray cheat". A summary
built on that transcript would be wrong in exactly the places that matter. A larger
model (small.en) helps with ordinary words but still can't know a team's acronyms, and
it may not keep up live on the target laptop.

**What the user sets.**
- **Settings → Vocabulary (L33).** A multi-line box, one term per line, as it should be
  spelled: `KoVa`, `QDM`, `ETL`, `Zentrix`, `zone code`. Blank lines and `#` comments
  are ignored. A counter shows how much of the limit is used (below). Saved in
  `settings.json` as a list.
- **This meeting (L34).** The Start dialog has an "Extra terms for this meeting" box
  (names of attendees, a project codename). Attendee names from L13 are added
  automatically. These apply to this meeting only and aren't saved to Settings.
- **From a line (L37, P1, built in WP9d).** Right-click → "Add to vocabulary…" opens a small dialog
  with the selected text ("Quodium") for the user to replace with the right term
  ("QDM"). It's added to Settings and applies from the next utterance (during a recording it goes to the
  recorder like a Settings change, `Recorder.set_vocabulary`). It doesn't change
  the line that was misheard: the transcript isn't edited (PRD non-goal).
  *Prefill rule:* the part of the text selection that lies inside that line's own text (not its time or speaker
  columns), tidied into one term: line breaks and runs of spaces become one space, quotes and brackets are
  trimmed, and `. , ; : ! ?` at the end ("Quodium." becomes "Quodium"). With nothing selected the box is empty:
  the whole line is not a term, so there is nothing sensible to guess. *Checks as you type* (`vocab.check_new_term`):
  not empty, not starting with `#` (a comment), at most 80 characters, not already in the list (ignoring case:
  Add is off and the message names the existing spelling), and when the list is already at the token budget a
  warning says the new term would be left out until others are removed (it can still be added).
- **Sharing (L38, P1, built in WP9d).** Settings → Vocabulary has **Add from file…**, **Replace from file…**
  and **Export to file…**. The file is a plain `.txt`, one term per line, UTF-8 (a BOM and CRLF are accepted
  when reading, an ANSI file is read as cp1252; blank lines and `#` comment lines are ignored, duplicates
  dropped as in the box); export writes UTF-8 without a BOM, LF line ends. *Adding* appends the terms that are
  not already in the list (ignoring case); *replacing* swaps the list. Either way only the box changes: nothing is
  saved until Save, so Cancel undoes an import. A file over 1 MB or with binary content is refused with a message.
  `vocab.read_terms_file`, `write_terms_file` and `merge_import` hold the logic.

**How it's used.** Whisper accepts a short text prompt that biases what it writes. The
recorder builds one prompt per meeting, meeting terms first, then the Settings list, and
passes it with every utterance on both streams. faster-whisper offers two ways in:
`hotwords` and `initial_prompt`. A spike on Test #4's kept audio picks one (see
Milestones). Today `transcribe.py` passes neither and turns off
`condition_on_previous_text`, so the vocabulary is the only prompt.

- **Limit.** Whisper's prompt is capped at about 224 tokens, and long prompts slow down
  every utterance. The list is cut at a token budget (proposed: 120 tokens, roughly 60–80
  short terms). Meeting terms win over Settings terms when the budget runs out. Settings
  shows "42 of ~70 terms used" and names the terms that would be dropped.
- **Mid-meeting changes.** A change in Settings or from a line takes effect from the
  next utterance the transcriber picks up; lines already written stay as they are.
- **Recorded (L35).** `meta.json` gets `vocabulary`: the terms actually passed (after
  the budget), plus `vocabulary_changed_at_ms` for any change during the meeting. That's
  additive under schema 1. The downstream app can use the same list to read the
  transcript better.

**Guardrails (L36).** A prompt can make Whisper write a vocabulary word when it hears
noise or a short unclear sound, e.g. "QDM." for a cough. Before release, measure on kept
real audio (Tests #2–#4) with and without vocabulary:
- **Accuracy:** count how many Test #4 domain words come out right. The goal is most of
  the list above.
- **No new hallucinations:** lines made only of vocabulary terms, or that appear where
  the run without vocabulary had none, must not go up. The existing filters
  (`drop_hallucinated`, the no-speech check) stay on. If needed, add one rule: a line
  under ~1 s whose only word is a vocabulary term needs `confidence` above a floor.
- **Speed:** live line delay p90 stays under the PRD's 5 s on the target laptop (it was
  2.4 s in Test #4 without vocabulary).
- **Names:** people's names are the riskiest terms, because a wrongly inserted name is
  a wrong fact ("Thank you, Dana" when nobody said Dana), and attendee names are
  added automatically (L34). Test #4 has four known names on the call (Remote A–D),
  so count names that appear where the run without vocabulary had none, and
  check each against the audio. If names misfire, keep attendee names out of the prompt
  by default.

Vocabulary is plain text in settings and `meta.json`, like the transcript; nothing
leaves the PC.

## Window design

One resizable Tk window with two views, **Transcript** and **Meetings**, opened from
the tray menu and (setting, on by default) on the Transcript view automatically on
Start.

```text
┌ Weekly sync ─ ● 12:41 · 84 lines · live ── [Mute me] [Export…] [Meetings] ┐
│ Speakers:  [You]  [Dana]  [Eric]  [SPEAKER_05 · Who is this?]              │
├────────────────────────────────────────────────────────────────────────────┤
│ ▶ 11:02  You      Let's start with the launch checklist.                  │
│ ▶ 11:03  Dana     Legal still needs one more day.                          │
│   — You muted 11:05–11:06 (1 min) —                                        │
│ ■ 11:06  Eric     OK, decision: we ship Friday.        ← playing           │
│ ✔ 11:06  Decision 1 · Ship Friday                          [edit]          │
│ 11:07  SPEAKER_05  Can everyone hear me?             ← new voice           │
│                                                  [↓ Jump to live]          │
├────────────────────────────────────────────────────────────────────────────┤
│ Mic ▮▮▮▯▯  Call ▮▮▮▮▯  Lag 1.2 s  Backlog 0   [⚑ Bookmark ▾] [Stop]         │
└────────────────────────────────────────────────────────────────────────────┘

While muted, an amber bar sits under the header:
│ 🎤✕ You're muted in the recording (the call still hears you)   [Unmute]   │
```

- **Speaker bar.** One chip per live label (merged by name). Click to name; names entered
  earlier in the meeting and the attendee list (see brainstorm) are offered first.
  A new unnamed voice gets a highlighted "Who is this?" chip.
- **Attendees (L13, built in WP9a).** The Start dialog has an optional "Who's on the call" box (one
  name per line, or separated by commas or semicolons; an invite's `Name <email>` works). The list is
  saved in `meta.json` as `attendees` (only when not empty) and shown as a numbered strip under the speaker
  bar while the meeting is recording or being processed: "Keys 1-4 name SPEAKER_02: 1 Ana  2 Ben ✓  3 Cy". The name
  picker and "This line is…" offer attendees not yet used first (in the order typed), then the names already in use. On a line
  shorter than the fingerprint minimum (0.5 s, `diarize.MIN_FINGERPRINT_S`) both say "Too short to recognise
  the voice: the name applies to this line only." (#26): such a line cannot steer clustering, though its name
  still counts for the name mapping. The check uses the line's length; a line just over 0.5 s whose words
  span less still gets no fingerprint and no note.
  - **Number keys.** With the window focused and no text box focused, keys 1-9 name a voice as attendee N,
    exactly as the chip's Name… does (a speaker-level tag, past and future lines). The voice is the one the
    "Who is this?" bar is asking about, when it is showing; otherwise the speaker of the latest call line whose
    speaker is still unnamed; when every call speaker is named, the speaker of the latest call line (so a wrong
    name can be replaced). The strip's heading names the voice a key will name. The keys do nothing in the final
    view, on the Meetings page, without attendees, with Ctrl or Alt held, or while an entry, spin box or editable
    text has the focus. A key beyond the list ("7" with three names) says so in the status bar. That a key
    answers the bar's voice rather than the latest line while the bar is showing was confirmed in #18: the number
    goes to the voice the user is reading about.
  - **Confirmation.** A bar under the notice says "SPEAKER_03 is now Ana · [Undo] [OK]" for 10 s. Undo puts
    the touched labels and lines back to the tag they had (including none).
  - **"Who is this?" for new voices.** The first time a call label (not `You`/`Remote`) appears unnamed in the
    live view, a bar under the notice bar asks "New voice SPEAKER_03 ("…first words…"). Who is this?" with
    buttons for the attendees not yet used (each with its own number), "Other…" (the name picker) and "Not now". It is
    part of the window: no dialog, no focus change, nothing when the window is hidden. One voice at a time, the others
    queue, oldest first; naming a voice any other way, or "Not now", moves on, and a voice is asked about once. Voices
    already on screen when the window is opened mid-meeting are queued too. It appears **only in a meeting with
    attendees** (#18): without them every new call voice would raise it, stacked with the headphone warning in the
    same yellow. Without attendees an unnamed voice is marked only by its highlighted "Who is this?" chip, which names
    it the same way; more voices can then reach the final pass unnamed, where L14 does not protect them. With attendees the bar, the strip and the headphone warning can still stack; that is accepted.
  - **Names and speech recognition.** The names are *not* given to Whisper by default (WP3 spike: a wrong
    name is a wrong fact, so L34's automatic add was not built). The Start dialog has an opt-in box, "Also give
    these names to speech recognition", off by default; ticked, they join the meeting's terms (so they appear in
    `vocabulary` in `meta.json` like any term).
- **Transcript.** Colour per person, time from `start_ms` + `started_at`. Auto-scroll
  while at the bottom; scrolling up pauses it and shows "Jump to live". Right-click a
  line: "This line is…", "Add to vocabulary…", "Copy".
- **Status bar.** Elapsed time, line count, lag and backlog (`RecordingState`), mic/call
  levels, gaps (device rebind) shown inline as "— 4 s gap (device changed) —".
- **After Stop.** The status bar shows the after-Stop pass's stage and fraction (already
  reported by `finalize_meeting`'s progress callback; the tray only shows the name
  today) and an estimate of the time left ("about 20 min left"). The estimate comes from
  the speed of earlier final passes on this PC (final-pass time ÷ meeting length, stored
  in `meta.json`), refined by the pass's own progress; the first meeting on a PC shows
  only the stage. This matters on the target laptop, where an hour-long meeting takes
  about 35 minutes (Test #4). The live transcript stays usable throughout. On `DONE`
  the view reloads from `transcript.final.jsonl`, applies `speakers.json`, and
  highlights changed lines and any conflicts for ~10 s.
- **Short speakers (L40, built in WP9d).** In the final view, a speaker with little speech is shown dimmed
  and marked "short", never hidden: Test #4's Remote D had only 28 s, and a second possible person 13 s. The rule
  (`logic.short_speakers`): the **final view only** (a live label can still merge into another), and a person whose
  lines add up to **less than 30 s** of speech (the sum of line durations; two labels with the same name count as
  one person, so naming two voices alike can lift them over the limit). The number of lines is not used: one long
  line is not little speech. When *every* speaker is under 30 s (a very short meeting) none is marked, because
  nobody stands out. These three points (30 s of speech, final view only, nobody marked when all are short) were
  confirmed in #18. A named speaker is marked too ("Dana · short"). It applies to the chip ("You · short") and to the
  speaker column of each of that person's lines ("Ben · short"), whose text and name are drawn in a lighter
  colour; an unnamed one's chip still asks "Who is this?". Exports and every output file are
  unchanged.
- **Past meetings.** The Meetings view (above) opens any meeting read-only with names;
  speaker naming works there too. This replaces the Rename speakers dialog.
- **Bookmarks** are drawn as a row between lines, by `at_ms`, with the type's symbol and
  colour, an edit control for label and type, and a play control. The Bookmark split
  button lists every type with its hotkey.
- **Mute me** in the header toggles mute (L21) and shows its hotkey; muted periods are
  drawn as rows like device gaps.
- **Play controls.** A ▶ at the start of each line (■ while it plays); greyed out with a
  tooltip when the audio is gone or not yet readable. Speaker chips have "Hear this
  voice" in their menu. The header has a **Keep this meeting's audio** check box while
  recording or processing.
- **Export…** opens a small dialog: format (Markdown / Text), times (since start /
  clock), join same-speaker lines, then Save As or Copy.

**What "as they speak" really means.** A line exists only after the speaker pauses
(VAD end of utterance, then transcription: p90 1.4 s in Test #3). So the user names the
line that just appeared, not the voice mid-sentence. The status bar's level meter can
show that the call is active, but who is speaking is only known once the line lands.

## Technical approach

- **Where it runs.** In the tray process, on the Tk thread that already owns the dialogs
  (`tray.py`). No new UI framework; PyInstaller build unchanged.
- **Data source.** The window tails the meeting's files (`transcript.jsonl`,
  `speaker_tags.jsonl`, then `transcript.final.jsonl` and `speakers.json`) on a 250 ms
  Tk timer. It keeps a byte offset per file and parses only lines appended since the
  last read (a complete line ends in `\n`; a partial last line waits for the next
  tick), and only redraws what changed. Re-reading a 2-hour file with
  `writer.read_events` every tick would cost CPU the transcriber needs: on the target
  laptop live delay already reached a max of 6.8 s in Test #4. `read_events` is used
  once, to open a past or final transcript. It reads exactly what the downstream app reads,
  works the same for past meetings, and adds no coupling to recorder threads. Status
  (lag, backlog, levels, finalize progress) comes from `AppController.state`, which the
  tray already polls.
- **Writing tags.** The window calls an `AppController.tag(...)` method, which appends to
  `speaker_tags.jsonl` through a small writer (flush per line, same lock discipline as
  the transcript writer). Tags made after Stop but before `DONE` are picked up because
  the final pass reads the tags file at its end.
- **Final pass.** `_voice_labels` reads the tags first and passes "cannot share a
  cluster" pairs (L14) to `cluster_voices` and `fold_small_clusters`: average linkage
  skips any merge that would join two names, and the fold skips any target with a
  different name. After the final file is in place, a new step computes overlaps
  against final segments and writes `speakers.json` with `from_tags` and `conflicts`.
  Crash recovery (`housekeeping.py`) runs the same steps.
- **Bookmarks.** `AppController.bookmark(kind, via)` reads the recorder's meeting clock
  and appends to `marks.jsonl` (same flush-per-line writer as tags). Hotkeys (all
  bookmark types and mute) are registered with `RegisterHotKey` via ctypes on one small
  thread with its own message loop, started at Start and unregistered at Stop; presses
  are passed to the Tk thread through the tray's existing call queue. No new dependency.
- **Mute.** `Recorder.set_mic_muted(bool)` flips a flag read in the capture callback
  path, where mic samples are replaced with zeros before they reach the FLAC buffer and
  VAD. It updates `mic_muted_since_ms` and, on unmute or Stop, calls
  `MeetingFolder.add_gap("mic", …, reason="muted")`. The tray gets a fifth icon
  (recording + crossed-out mic).
- **Meetings view.** Reads folders on open and on a 2 s timer while visible (sizes are
  cached per folder by `meta.json` mtime). Retry calls the controller's existing
  finalize queue. Recycle Bin deletes go through a small `shell.py` wrapper around
  `SHFileOperationW`.
- **Playback.** A new `playback.py`: one output stream through PyAudioWPatch (already a
  dependency; it is PortAudio, so it plays as well as captures), one clip at a time on a
  worker thread. Clip sources: `Recorder.recent_audio(source, start_ms, end_ms)` for the
  in-memory window while recording, `read_audio` slices of the FLAC files afterwards.
  The recorder holds the playback windows and applies the drop rule in the writer,
  next to the echo dedupe.
- **Export.** A new `export.py` with pure functions: events + names + marks → text. The
  window, the meetings list and the CLI all call it; it is unit-tested against fixture
  meetings (live-only, final, interrupted, no marks, marks in pauses).
- **Vocabulary.** `Settings.vocabulary: list[str]` (new field, default empty; a settings
  version bump isn't needed since it's additive). `WhisperTranscriber.transcribe` takes
  the prompt and passes it as `hotwords` or `initial_prompt`; the recorder holds the
  current prompt and replaces it under its lock when Settings or a line adds a term. The
  token budget uses the model's own tokenizer (`WhisperModel.hf_tokenizer`). The Start
  dialog and Settings get the text boxes; `meta.json` is updated at Start and on change.
- **Scale.** A 2-hour meeting is ~2–3k lines; a Tk `Text` widget with per-speaker tags
  handles that. Speaker renames reapply a tag colour/name, not re-render every line.

## Milestones

1. **Read-only live view and export** (L1–L4, L8–L10). Window, tailing, auto-scroll,
   status bar, after-Stop progress, swap to final file, past meetings. Export to `.txt`
   and `.md` from the window and the CLI. Export only reads files, so it can ship first.
   *Check:* export Tests #1–#4 and the TTS sample; open the `.md` in a Markdown viewer
   and the `.txt` in Notepad. Record a real call on the target laptop with the window
   open the whole time: `live_stats` line delay p90 and max must be no worse than
   Test #4 without the window (p90 2.4 s, max 6.8 s).
2. **Bookmarks, types and mute** (L5–L7, L21–L27). One hotkey thread for all of them,
   AltGr check, button, tray menu, `marks.jsonl` with `kind`, bookmarks in the view
   and in exports, labels, type changes and deletion, line bookmarks. Mute in the
   capture path, `gaps` with reason `muted`, tray icon and header bar. OUTPUT_SPEC
   update.
   *Check:* during a real call, press each bookmark hotkey with the call app focused;
   confirm no focus change, no sound in `system.flac`, the right type, and that each
   bookmark sits next to the right line in the view and the export. Mute for 1 minute
   while talking: `mic.flac` is silent for exactly that minute, no mic lines in it, the
   system stream is unaffected, and the gap is in `meta.json`. Repeat with a German
   keyboard layout for the AltGr warning.
3. **Meetings view and housekeeping** (L29–L31). List, open, export, delete to the
   Recycle Bin, delete audio, retry failed final passes.
   *Check:* force a final-pass failure (e.g. remove the diarization model), confirm
   the error shows, restore the model, Retry, and the meeting completes. Retry during a
   recording waits until Stop. Delete a meeting and restore it from the Recycle Bin;
   delete one permanently and confirm it isn't in the Recycle Bin.
4. **Click to hear** (L15–L18, L20). Playback after Stop first (no capture risk), then
   during the meeting from the in-memory window with the drop rule and the headphone
   warning. Keep this meeting's audio. Spike on reading older audio mid-meeting.
   *Check:* on Test #2/#3 kept audio, every line's clip starts and ends on its words.
   During a real call with a headset, play at least 20 clips (short and long, mic and
   call lines); count how many got through as lines or created a live speaker, and how
   many real remote lines were dropped with them. The replayed clip is transcribed
   afresh, so its words won't always match the original; the result decides whether
   the drop rule is good enough until milestone 7. Repeat once without headphones and
   record what the call heard.
5. **Naming speakers** (L11, L12, L14). Speaker chips and per-line tags,
   `speaker_tags.jsonl`, different-names rule in the final pass's clustering and fold,
   mapping to `speakers.json` (user's names win, merge on write), conflict display,
   names in exports. OUTPUT_SPEC update; downstream reader updated in step.
   *Check:* replay Test #3's kept audio (`simulate`) with lines 10 and 15 named as two
   different people, and Test #4's with one of Remote D's lines (e.g. 102) and one of
   Remote B's named. B and C, and Remote D and Remote B, must come out as separate final
   speakers with the right names. Re-run Tests #2–#4 and the TTS sample with no names
   to confirm the result is unchanged when nobody names anyone.
6. **P1 polish** (L13, L19, L24, L28, L32, L40; L32 and L40 built in WP9d). Attendee list at Start, number-key
   shortcuts (1–9 = attendee) for the latest line, "Who is this?" prompt for new voices,
   Play from here, Play clips on, mute reminders and Start muted, user-defined bookmark
   types, Storage panel.
7. **Clips never captured** (L17 layer 1). Process-loopback capture that excludes
   Huddle Helper; re-run the device-switch test and a soak test.
8. **Same name, same speaker** (L39). Lines with the same name start in one cluster;
   re-run Tests #2–#4 and the TTS sample. (The different-names rule, L14, moved into
   milestone 5 after Test #4.)
9. **Custom vocabulary** (L33–L36, then L37–L38). Independent of the window: the Settings
   box, Start dialog box and `meta.json` field work without it, so this can ship before
   milestone 1. Starts with a spike: `hotwords` vs `initial_prompt`, and the token
   budget, on Test #4's kept audio. "Add to vocabulary…" follows once the Transcript view
   exists.
   *Check:* re-transcribe Test #4's kept audio with the list in "Custom vocabulary";
   count domain words right before and after, count vocabulary-only lines, and compare
   line delay. Re-run Tests #2 and #3 to confirm no new hallucinated lines. Then a real
   call on the target laptop with `live_stats` p90 under 5 s.

## Risks and open questions

| Risk / question | Notes |
| --- | --- |
| **The final pass merges two people the user named, and one name is lost** | The failure seen in practice (Test #3: B+C; Test #4: Remote D into Remote B). Mapping alone can't fix it. The different-names rule (L14, P0, milestone 5) keeps named people apart; checked on Tests #3 and #4. |
| The final pass merges an *unnamed* person into a named one | L14 only protects people the user named; naming every voice live is the defence (see "Attendees (L13)"). The threshold and fold problem is tracked in TODO (Test #4). Short speakers are shown, not hidden (L40). |
| Live label is wrong for some lines, so a speaker-level tag names the wrong person on them | Overlap mapping is by majority, so a few wrong lines don't win the name. For the clustering rules, a speaker-level tag only counts for lines whose voice is at least 0.7 like the rest of that name's lines. Per-line tags override. This does *not* cover a whole person being merged; see the first row. |
| User tags a live label that the final pass splits between two people | Lines of that label that count as named (line-level tags always; speaker-level only when at least 0.7 like the rest of that name, see above) are put in one final speaker (L39), so the name lands on one speaker. Pieces the gate leaves out are not forced: if they are a different voice they stay in another speaker, which stays `SPEAKER_nn` and is shown as untagged. Unnamed lines of the same person that fall outside the cluster by voice can also end up in an unnamed speaker. The mapping after the pass (L12) goes by overlap with the tagged live range, gated-out pieces included, but a name goes only to the final speaker (per stream) that overlaps it most, so those pieces cannot give a second speaker the same name (seen on Test #4 before this rule: 4 s of them named a 570 s speaker). |
| Downstream app also writes `speakers.json` | Decided: the user's names win for speakers the user named, the downstream app's names are kept for the rest. Writes are atomic; Huddle Helper re-reads and merges just before writing. |
| **The window slows live transcription on the target laptop** | Test #4 already reached max delay 6.8 s and max lag 6.6 s without the window. The window reads only appended lines and redraws only changes; milestone 1 checks live delay with the window open on the target laptop (L1). |
| **Long wait before the final transcript on the target laptop** | 0.6× the meeting's length (13.5 min for 22.7 min; ~35 min for an hour). The window shows an estimate of the time left (L3) and the live transcript stays usable. Making the pass faster is tracked in TODO (Test #4). |
| Window distracts during the meeting | Compact mode and a setting to not auto-open. Bookmarks work from the hotkey with the window closed. |
| Bookmark hotkey clashes with the call app or another tool | Registered only while recording; changeable or off in Settings; failure shown in Settings, button and tray item still work. |
| User presses the bookmark a few seconds after the moment | Export and view show the bookmark after the paragraph it falls in, so the line just before is right there. Open: a "look back N s" setting, decided after milestone 2's real-call test. |
| A clip played during the meeting is captured by loopback and transcribed again | Drop rule on playback windows (first release); process-loopback exclusion removes it at the source (milestone 7). The replayed clip is transcribed afresh, so its words may not match the original; milestone 4 measures how many get through. |
| Without headphones, people on the call hear the clip | One-time warning, confirmation when echo has been seen, Play clips on a different device. Can't be prevented in software while the mic is live. |
| Remote speech over a playing clip is dropped with it | Known gap of the drop rule; counted in `meta.json`; fixed by milestone 7. |
| User forgets they muted and loses part of the meeting | Amber bar, tray icon, tooltip, 10-minute reminders (P1); Stop always unmutes. |
| User thinks "Mute me" mutes them in the call (or the reverse) | Every label says "in the recording" and the bar says "the call still hears you". |
| Mute with a shared mic mutes the whole room | Tooltip says so for a meeting with a shared mic. |
| Muted audio could still be played back | Silence replaces the mic samples before the in-memory playback window as well as the buffer and transcription. |
| **The downstream app reads a muted gap as lost audio** | Until now every `gaps` entry meant a device fault. OUTPUT_SPEC tells readers to check `reason`; `muted` is deliberate. |
| **The downstream app ignores the new files** | Names and bookmarks never reach summaries. Update its reader alongside milestones 2 and 5, not after. |
| `Ctrl+Alt+letter` hotkeys swallow AltGr characters on non-US layouts | Layout check in Settings with a suggested alternative; hotkeys only registered while recording. |
| Too many bookmark types to remember under pressure | Four built-in, plain Bookmark always the easiest; a different-type press within 2 s retypes the last one. |
| Deleting the wrong meeting, or one the downstream app hasn't read | Recycle Bin, confirmation naming each meeting, stronger warning for the last 24 hours, not allowed while recording or processing. |
| **"Deleted" meetings stay on disk in the Recycle Bin** | The opposite of deleting the wrong one: a sensitive meeting the user thinks is gone. The confirmation says it can be restored; Delete permanently is a separate choice (L30). |
| Retry on a failed meeting fails again the same way | Error stays visible; retry is manual, never automatic in a loop. |
| Retry slows a meeting that is recording | The final pass competes with live transcription; a retry waits until no meeting is recording (L31). |
| Users expect to hear lines later but Keep audio was off | Greyed control explains why; Keep this meeting's audio during the meeting. |
| Memory for the in-memory audio window | ~38 MB for 10 min of both streams; length is a constant that can shrink. |
| Exported files spread transcripts outside the meetings folder | User picks the location each time; nothing is exported automatically. Same privacy as copying the JSONL. |
| Names are personal data | Stored only in the meeting folder like the transcript; no network. |
| Vocabulary makes Whisper write terms that weren't said (on noise or short sounds) | Measured before release (L36); existing hallucination filters stay on; a confidence floor for very short vocabulary-only lines if needed. |
| **Names in the vocabulary appear where nobody said them** | A wrong name is a wrong fact downstream, and attendee names are added automatically (L34). Measured on Test #4, which has known names; if names misfire, attendee names stay out of the prompt by default. |
| A long vocabulary slows every utterance or crowds out the audio | Token budget with a visible counter; meeting terms first; speed checked on the target laptop. |
| Vocabulary helps less than expected on base.en | Measure in the milestone 9 spike; pair with the small.en benchmark (TODO) if base.en still misses the terms. |

**Decided in the risk review (Nov 9, 2026)**

| Question | Decision |
| --- | --- |
| Should a speaker tag also apply to later lines of that live label? | Yes, until the user changes or removes it. |
| Should the final view hide `SPEAKER_nn` for speakers with little speech? | No. Show them dimmed and marked "short" (L40). Test #4's Remote D had only 28 s, and another possible person 13 s. |
| Who wins when both apps write `speakers.json`? | The user, for speakers the user named; the downstream app's names are kept for the rest. |
| Should vocabulary also correct known mishears ("Quodium" → "QDM") after transcription? | No. That edits transcript text, a PRD non-goal; prompt-only. |
| When should names steer the final pass? | Now: the different-names rule (L14) is P0 in milestone 5, not "Later". Same name, same speaker (L39) stays P1 in milestone 8. |

## Brainstorm: other things a GUI would make possible

Grouped by theme. ⚠ marks items that touch a PRD non-goal or the file contract.
Bookmarks and their types, `.txt`/`.md` export, click-to-hear, mute my mic and the
meetings/housekeeping items started here and are now requirements (L5–L10, L15–L32).

**During the meeting**
- **Side notes.** Type notes in the window, timestamped on the meeting clock, saved
  next to the transcript.
- **Pause everything.** Like mute, but for both streams: take a whole stretch of the
  call off the record, written to `meta.json` `gaps` with reason `paused`.
- **Live health.** Per-stream level meters, "mic too quiet" shown in place (today it is a
  one-off toast), lag/backlog gauge, a warning when the call stream has been silent for
  minutes (wrong output device).
- **Always-on-top recording badge.** A small floating indicator, stronger than the tray
  icon, for the recording-transparency requirement; plus a "consent said" check-off.
- **Attendee list.** Paste names at Start (from the invite); they become one-click tags
  and help the user spot a missing voice.

**Reading and using the transcript**
- **Search** within a meeting and across all meetings.
- **Re-transcribe a line** with a larger model (small.en or medium.en) from its audio,
  shown as a suggestion next to the original, for the mishears click-to-hear exposes.
- **Low-confidence highlighting** using `confidence`, so likely mishears ("Gitli
  Cluster") stand out.
- **More export formats:** subtitles (SRT/VTT) to pair with a screen recording, or
  Word. Export "only the bookmarked parts" (each bookmark with the minute around it).
- ⚠ **Corrections without editing.** Fix a misheard word or redact a line as an overlay
  file (`corrections.jsonl`) instead of editing the transcript. Close to the "no
  transcript editor" non-goal; redaction may still be worth it for privacy.

**Speakers**
- **Merge / split speakers** in the final view ("these two are the same person", "this
  line is someone else"), feeding a re-run of the final pass.
- **Re-run the final pass** with different settings from the window (today:
  `huddle-helper finalize`).
- ⚠ **Remembered voices.** Opt-in, local voice profiles so a regular colleague is named
  automatically in future meetings. Highest value, but it stores biometric-like data;
  needs consent and retention rules of its own.

**Setup and diagnostics**
- **First-run check:** play a test sound and speak, confirm both streams are heard
  (would catch the quiet HyperX mic up front).
- **Model picker with a speed test** (`bench` in a button), to settle base.en vs
  small.en on the user's own laptop.
- **Stats view** of `live_stats` (line delay p90, backlog) per meeting, for the PRD's
  latency and soak checks.

**Downstream app**
- **"Open in analyzer"** button that launches the downstream app on this meeting.
- ⚠ **Show the downstream app's output** (summary, action items) in the same window.
  That is analysis, which the PRD keeps out of this app; only worth it if the
  downstream app writes a documented results file.
