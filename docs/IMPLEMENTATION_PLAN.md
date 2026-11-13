# Implementation plan: open TODOs and the Transcript window

Nov 9, 2026 · for the development agent · Status: finished (historical)

> **Historical.** WP0-WP9 and WP11 are built; WP10 (process loopback) was a spike and is out of
> scope (`.out-of-scope/process-loopback-capture.md`). Sections 3, 7 and 8 describe the code and
> the open questions as they were on Nov 9, before WP1. For how the app works now, read the
> [README](../README.md), [OUTPUT_SPEC.md](OUTPUT_SPEC.md) and [GLOSSARY.md](../GLOSSARY.md); the
> record of what was measured is [TODO.md](../TODO.md).

This plan turns two documents into ordered, checkable work:

- [TODO.md](../TODO.md): open findings from Tests #1–#4 (final-pass speaker merges,
  fragments, final-pass speed, misheard jargon).
- [LIVE_VIEW.md](LIVE_VIEW.md): the Transcript window (requirements L1–L40, milestones 1–9).

**Which document wins.** LIVE_VIEW.md defines *what* each feature does (behaviour, wording,
file formats). This plan defines *how and in what order* (modules, interfaces, threading,
UI construction, tests). Where this plan changes something LIVE_VIEW says, it is listed in
[Decisions made in this plan](#5-decisions-made-in-this-plan); everything else in LIVE_VIEW
stands. [OUTPUT_SPEC.md](OUTPUT_SPEC.md) is the file contract and is updated in the same
commit as any contract change.

## 1. Ground rules

1. **Never work on the kept recordings in place.** They are the only real test data and
   can't be re-recorded. Copy them to `lab\` (add `lab/` to `.gitignore`) and work there.

   | Recording | Kept audio | Source folder |
   | --- | --- | --- |
   | Test #1 | No (deleted) | `%USERPROFILE%\MeetingCompanion\meetings\2026-11-09_1324_test-meeting-1` |
   | Test #2 | Yes | `%USERPROFILE%\MeetingCompanion\meetings\2026-11-09_1334_test-2-2` |
   | Test #3 | Yes | `%USERPROFILE%\MeetingCompanion\meetings\2026-11-09_1441_test-3` |
   | Test #4 | Yes | `%USERPROFILE%\Downloads\testmeeting\2026-11-09_1605_*` (one folder) |
   | TTS sample | Generated | `python tools\make_sample_meeting.py --out sample --echo` |

2. **`huddle-helper finalize` can delete kept audio today.** `cli._finalize` passes
   `keep_audio` from Settings, not from the meeting, so re-running the final pass on a kept
   folder with Keep audio off removes `audio\`. Until WP0 fixes this, always pass
   `--keep-audio`.
3. **Nothing from a real meeting goes into git:** no audio, no transcript text, no new
   names. Test fixtures are synthetic or use line numbers and letters (A, B, C) only.
4. **Schema stays 1; changes are additive.** Existing files keep their meaning.
5. **No network at runtime, no new runtime dependencies.** Everything below is built from
   what is installed: Tk, ctypes, comtypes, PyAudioWPatch, soundfile, soxr, numpy.
6. **Logic lives in UI-free modules with unit tests; Tk code stays thin.** The existing
   test style (fake transcriber, embedder and capture in `tests/conftest.py`) continues.
   `pytest` must stay green (54 tests today, about 30 s).
7. **One work package per branch**, small commits in the repo's style ("LIVE_VIEW: …",
   "TODO: …"). When a TODO item is fixed, tick it in TODO.md with the commit, as that file
   asks. Update the README code map when modules are added.
8. **Rebuild and launch the packaged exe** (`packaging\build.ps1`) at the end of every work
   package that adds a module or touches `tray.py`. Add new packages to `hiddenimports` in
   `packaging\huddle_helper.spec`.
9. **Stop and report rather than guess** when a check marked "Needs Bryce" is the only way
   to confirm something. Those are collected in [section 7](#7-needs-bryce).

## 2. Order of work

| WP | What | Covers | Size | Depends on |
| --- | --- | --- | --- | --- |
| 0 | Foundations: safety fixes, locks, timings, lab tools | — | S | — |
| 1 | Final pass: stop merging real speakers; fragments | TODO Test #3/#4 | M | 0 |
| 2 | Final pass: speed | TODO Test #4 | S–M | 0 |
| 3 | Custom vocabulary | L33–L36 (milestone 9) | M | — |
| 4 | Transcript window, read-only, and export | L1–L4, L8–L10 (milestone 1) | L | 0 |
| 5 | Bookmarks, types and mute | L5–L7, L21–L23, L25–L27 (milestone 2) | L | 4 |
| 6 | Meetings view | L29–L31 (milestone 3) | M | 4 |
| 7 | Click to hear | L15–L18, L20 (milestone 4) | L | 4 |
| 8 | Naming speakers | L11, L12, L14 (milestone 5) | L | 1, 4 |
| 9 | P1 polish | L13, L19, L24, L28, L32, L37, L38, L40 (milestone 6) | M | 5–8 |
| 10 | Clips never captured (process loopback) | L17 layer 1 (milestone 7) | L, spike first | 7 |
| 11 | Same name, same speaker | L39 (milestone 8) | S | 8 |

WP1–WP3 come before the window because they fix what the user already depends on (the
final transcript and its words), need no UI, and LIVE_VIEW itself says milestone 9 can
ship first. WP1+2, WP3 and WP4 touch different files and can run in parallel; the only
shared files are `recorder.py` and `app.py`, in different functions.

## 3. What exists today (facts this plan relies on)

- **Threads.** Tk owns the main thread; pystray runs detached and posts menu actions to
  `TrayApp.calls`, drained every 100 ms by `_pump`. A recording adds capture callbacks,
  two stream workers (FLAC buffer + VAD), one transcribe worker and a 200 ms ticker.
  `AppController` runs one `finalize` worker thread fed by `_jobs`.
- **State.** `AppController.state` (`AppState`) holds phase, `recording: Status` (elapsed,
  lag, backlog, segments) and `finalizing`, a display string such as
  `"Identifying speakers (system) 40%"`. `_on_change` is called from worker threads.
- **Final pass** (`finalize.finalize_meeting`): sherpa offline diarization gives turns,
  used only to split lines; `_voice_labels` embeds every piece, then
  `cluster_voices` (average linkage, threshold 0.65) and `fold_small_clusters` (folds
  clusters under max(4 s, 3% of speech) into the most similar large one, with no
  similarity check) give labels; `join_fragments` joins same-speaker pieces.
- **Meta writes.** `MeetingFolder.update_meta` is read-modify-write under a lock that
  belongs to the *instance*. Two instances for one folder (which the window will create)
  can lose each other's updates.
- **Transcriber.** `WhisperTranscriber.transcribe(audio)` passes no prompt. Utterances are
  at most 12 s (`max_utterance_s`), so each is one Whisper window.
- **Settings.** `config.Settings` is a flat dataclass; the Settings dialog saves every
  field, so changing a tuning default needs a `SETTINGS_VERSION` bump and a `_MIGRATIONS`
  entry or the old value sticks.
- **Environment.** Python 3.14 with Tk 9.0 in the venv; the README also allows 3.12
  (Tk 8.6). faster-whisper 1.2.1, sherpa-onnx 2.0.1, PyAudioWPatch 0.2.12.9.

## 4. Design

### 4.1 Architecture

One process, as today. The window never talks to recorder threads directly: it reads the
meeting's files (the same ones the downstream app reads) and calls `AppController` for
every action.

```text
                         Tk thread                                  worker threads
┌──────────────────────────────────────────────────┐   ┌─────────────────────────────────────┐
│ TrayApp ── calls queue ◄── pystray menu          │   │ Recorder                            │
│    │                   ◄── HotkeyThread          │   │  capture → MicGate → stream workers │
│    ▼                                             │   │     ├─► FlacBuffer   (audio\*.flac) │
│ MainWindow                                       │   │     ├─► RecentAudio  (10 min, RAM)  │
│  ├ TranscriptView ◄─ ops ── TranscriptModel      │   │     └─► VAD → transcribe(prompt)    │
│  │                             ▲                 │   │           → playback drop → speaker │
│  │                 JsonlTail ──┘  (250 ms timer) │   │           → merger → transcript.jsonl│
│  └ MeetingsView  ◄── library.scan (2 s timer)    │   ├─────────────────────────────────────┤
│                                                  │   │ finalize worker                     │
│ user actions ──► AppController ──────────────────┼──►│  tags → rules → cluster → final file│
│   bookmark · mute · tag · play · retry · delete  │   │  → speakers.json → meta → DONE      │
└──────────────────────────────────────────────────┘   ├─────────────────────────────────────┤
          ▲ reads                    │ appends          │ Player (one clip at a time)         │
          │                          ▼                  └─────────────────────────────────────┘
   transcript.jsonl · transcript.final.jsonl · speakers.json · meta.json
   speaker_tags.jsonl (new) · marks.jsonl (new) · audio\
```

**Threading rules**

1. Only the Tk thread touches widgets. The window pulls on `after` timers; nothing pushes
   into it from another thread.
2. Worker threads reach the UI only through `TrayApp.calls` (hotkeys, playback finished).
3. `AppController` methods are safe to call from the Tk thread and return quickly. Anything
   slow (delete, scan sizes, read a clip) runs on a short-lived thread and reports back
   through `calls`.
4. File writers: `transcript.jsonl` has one writer (the recorder). `meta.json`,
   `speakers.json`, `marks.jsonl` and `speaker_tags.jsonl` are written by several threads,
   always under the per-folder lock from WP0.

### 4.2 Modules

| Module | New? | Role |
| --- | --- | --- |
| `meeting.py` | changed | Per-path lock registry; `append_jsonl`; `merge_speakers`; paths for the new files |
| `tail.py` | new | `JsonlTail`: read only complete lines appended since the last call |
| `transcript_model.py` | new | UI-free model of one meeting: lines + names + marks + gaps → ordered rows and change ops |
| `export.py` | new | Rows → `.md` / `.txt` text (pure functions) |
| `marks.py` | new | `marks.jsonl` events, reducer, `MarkLog` (ids, double-press rule) |
| `tags.py` | new | `speaker_tags.jsonl` events, reducer, tagged ranges, clustering rules, mapping to final speakers |
| `vocab.py` | new | Parse term lists, token budget, build the prompt |
| `audio/gate.py` | new | `MicGate`: replaces muted mic samples with zeros, records muted periods |
| `audio/ring.py` | new | `RecentAudio`: last 10 minutes of one stream, 16-bit, in memory |
| `playback.py` | new | `Player` (one clip at a time), clip sources, playback windows |
| `hotkeys.py` | new | `RegisterHotKey` thread, combo parsing, AltGr check |
| `shell.py` | new | Recycle Bin delete via `SHFileOperationW` |
| `library.py` | new | Meetings list: `MeetingInfo`, cached scan, delete meeting / audio |
| `gui/` | new package | `window.py`, `transcript_view.py`, `meetings_view.py`, `dialogs.py`, `theme.py` |
| `app.py` | changed | Controller API below; structured finalize progress; retry queue |
| `recorder.py` | changed | Gate, ring, prompt, playback drop, levels, `now_ms()` |
| `finalize.py`, `diarize.py` | changed | Timings, gated fold, rules, fragments, `speakers.json` from tags |
| `transcribe.py` | changed | `transcribe(audio, prompt=None)`, `count_tokens` |
| `tray.py`, `ui.py` | changed | Menu, icons, tabbed Settings, Start dialog fields; `SpeakersDialog` removed in WP8 |
| `cli.py` | changed | `export`, `tray --replay`, safe `finalize` |

### 4.3 Controller API

All UI actions go through these. Names can change during implementation; the split of
responsibilities should not.

```python
class AppController:
    # state
    def current_folder(self) -> MeetingFolder | None      # meeting being recorded
    def now_ms(self) -> int | None                        # meeting clock, None when idle
    def is_busy(self, meeting_id: str) -> bool            # recording, queued or processing
    # state.finalize: FinalizeProgress | None  (meeting_id, stage, frac, elapsed_s, eta_s)
    # state.recording gains: levels {"mic","system"}, mic_muted, marks

    # bookmarks (WP5)
    def bookmark(self, kind="bookmark", via="hotkey", *, folder=None, at_ms=None, seq=None) -> dict
    def edit_mark(self, folder, mark_id, *, label=None, kind=None, delete=False) -> None
    # mute (WP5)
    def set_mic_muted(self, muted: bool) -> None
    # vocabulary (WP3)
    def set_meeting_terms(self, terms: list[str]) -> None
    def add_vocabulary_term(self, term: str) -> None      # Settings + live prompt
    # meetings (WP6)
    def retry_final(self, meeting_id: str) -> str         # "started" | "queued"
    def delete_meetings(self, ids, *, audio_only=False, permanent=False) -> DeleteResult
    # playback (WP7)
    def play_line(self, folder, source, start_ms, end_ms, text) -> PlayResult
    def stop_playback(self) -> None
    def set_keep_audio(self, folder, keep: bool) -> None
    # names (WP8)
    def tag_speaker(self, folder, label: str, name: str | None) -> None   # None = untag
    def tag_line(self, folder, line: dict, name: str | None) -> None
    def set_final_name(self, folder, final_label: str, name: str | None) -> None
```

### 4.4 The transcript model

`TranscriptModel` is the single place that answers "what does this meeting look like
right now?". The window and the exporter both use it, so they can never disagree.

- **Inputs.** Transcript events (live or final), tag events, mark events, `meta.json`
  (`gaps`, `started_at`, status), `speakers.json`.
- **Rows.** `LineRow(source, start_ms, end_ms, label, name, text, confidence, seq)`,
  `MarkRow(id, kind, label, at_ms)`, `GapRow(source, start_ms, end_ms, reason)`.
- **Order.** Sort key `(at_ms, rank, tie)` with rank 0 = line (by `start_ms`), 1 = gap,
  2 = mark. A line that arrives after a bookmark but started before it is inserted above
  the bookmark, which is exactly the "ends up next to the right line" behaviour LIVE_VIEW
  asks for.
- **Names.** Live file: a line tag wins over a speaker tag, which wins over the raw label.
  Final file: `speakers.json`, else the raw label. `person_key` (the name if there is one,
  else the label) drives colour and chip grouping, so two labels named "Eric" are one chip.
- **Output.** Every mutator returns a list of ops (`Insert(i,row)`, `Update(i,row)`,
  `Remove(i)`, `Reset`), which the Tk view applies. Unit tests assert on ops and rows; no
  Tk needed.
- **Which file.** `transcript.final.jsonl` once `DONE` exists, otherwise the live file with
  `provisional=True`. `switch_to_final()` reloads from the final file and returns the set
  of rows to highlight (below).

**Changed lines after the swap (L3).** Final labels are renumbered, so compare people, not
labels: build live-label → final-label correspondence by total overlap (ms, same stream),
then a final line is "changed" when its label isn't the one its dominant live line maps to.
Highlight those for 10 s.

**Tailing.** `JsonlTail(path)` keeps a byte offset, reads new bytes in binary, splits on
`\n`, holds back a trailing partial line, and returns parsed events. A missing file returns
nothing; a file that shrank (final pass re-run) resets to 0 and signals `Reset`. Per tick
the view applies at most 200 events so opening a long meeting never freezes the UI.

| Timer | Period | Reads |
| --- | --- | --- |
| Transcript, tags, marks | 250 ms | `JsonlTail.poll()` on each file |
| Header and status bar | 500 ms | `app.state` (no file I/O) |
| Meta and `DONE` | 1 s | `meta.json` only if its mtime changed; `DONE` existence |
| Meetings list | 2 s, while visible | `library.scan` with its cache |

### 4.5 Window design

One `tk.Toplevel` (`gui.window.MainWindow`), created on first use and kept by `TrayApp`.
Closing it hides it; recording is unaffected. Default 780×560, minimum 560×360, position
and size remembered in Settings. Two views swapped in place: Transcript and Meetings.

```text
┌ Weekly sync ───────────────────────────────────────────────────────────── ─ □ ✕ ┐
│ ● 12:41 · 84 lines · live      [Mute me in the recording] [Export…] [Meetings]  │ header
├─────────────────────────────────────────────────────────────────────────────────┤
│ MIC MUTED  You're muted in the recording (the call still hears you)    [Unmute] │ mute bar (amber)
│ Provisional: speaker labels may change after Stop.                              │ notice bar
├─────────────────────────────────────────────────────────────────────────────────┤
│ ● You   ● Dana   ● Eric   ● SPEAKER_05 · Who is this?                           │ speaker bar
├─────────────────────────────────────────────────────────────────────────────────┤
│ ▶ 11:02  You         Let's start with the launch checklist.                   ▲ │
│ ▶ 11:03  Dana        Legal still needs one more day, then we can send it     █ │
│                      to the group.                                           █ │ transcript
│          — You muted 11:05–11:06 (1 min) —                                    │
│ ■ 11:06  Eric        OK, decision: we ship Friday.                            │
│ ✔ 11:06  Decision · Ship Friday                                               │
│ ▶ 11:07  SPEAKER_05  Can everyone hear me?                                    ▼ │
│                                                        [↓ 3 new lines]          │
├─────────────────────────────────────────────────────────────────────────────────┤
│ Mic ▮▮▮▯▯  Call ▮▮▮▮▯  Lag 1.2 s  Backlog 0          [⚑ Bookmark ▾]  [Stop]     │ status bar
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Construction**

- **Transcript:** one `tk.Text`, read-only, word wrap, with tab stops for the play, time,
  speaker and text columns and `lmargin2` set to the text column so wrapped lines align.
  Everything is text with tags, no embedded widgets (2–3k embedded buttons are slow).
  Each row has a mark `row:<id>` at its start and a tag `row:<id>` over its extent;
  clicks are resolved from the tag under the pointer. Shared tags: `play`, `time`,
  `spk:<person_key>`, `text`, `markrow`, `gaprow`, `changed`, `playing`, `selected`.
- **Renaming a speaker** rewrites only the ranges of that `spk:` tag (iterate
  `tag_ranges` from the end backwards), never the whole widget.
- **Speaker bar:** a second, 1–2 line read-only `tk.Text` holding chips as tagged ranges.
  It wraps by itself, so no custom flow layout is needed.
- **Auto-scroll:** if the view was at the bottom before an insert, `see("end")` after it.
  Otherwise leave the scroll position and show the "↓ N new lines" button, right-aligned in a
  strip packed under the transcript only while it shows, so it never covers a row (#27);
  clicking it or scrolling to the end hides it.
- **Meetings view:** `ttk.Treeview` with columns Date, Title, Length, Status, Speakers,
  Marks, Audio; a filter box and status filter above; action buttons below.
- **Symbols:** only characters in the Basic Multilingual Plane (▶ ■ ⚑ ✔ ☐ ? ● ▮ ▯ ↓). No
  emoji: Tk 8.6 (Python 3.12) can't render them reliably. The muted state is the words
  "MIC MUTED" on amber, not a mic emoji.
- **DPI:** call `SetProcessDpiAwareness(1)` (ctypes, `shcore`) before `tk.Tk()` in
  `TrayApp.__init__`, or the packaged exe renders blurred text at 125–150% scaling.
  Check the three existing dialogs still fit afterwards.

**Theme** (`gui/theme.py`; native `vista` ttk theme, light)

| Token | Value | Used for |
| --- | --- | --- |
| Font | Segoe UI 10; time column Segoe UI 9 | Transcript; chips and bars use Segoe UI 9 |
| Text / muted text | `#111827` / `#6b7280` | Body; times, gap rows, hints |
| Recording / busy / idle | `#dc2626` / `#d97706` / `#6e7681` | Header dot, same as tray icons |
| Mute bar | `#fef3c7` background, `#92400e` text | "MIC MUTED" bar |
| Notice bar | `#eff6ff` background, `#1e40af` text | Provisional, conflicts, failed |
| Changed line / playing / selected | `#fef9c3` / `#dbeafe` / `#e5e7eb` | Row backgrounds |
| "Who is this?" chip | `#fef3c7` background | Unnamed new voice |
| Speakers (cycle) | `#1d4ed8` `#b45309` `#047857` `#7c3aed` `#be123c` `#0e7490` `#a16207` `#4b5563` | Speaker name, bold; assigned per `person_key` in order of first appearance |
| You | `#111827` bold | Mic lines |
| Bookmark kinds | bookmark `#b45309`, decision `#047857`, action `#1d4ed8`, question `#7c3aed` | Mark rows on `#f3f4f6` |

All speaker and kind colours are at least 4.5:1 on white.

**Header and bars by meeting state**

| State | Header text | Notice bar | Status bar |
| --- | --- | --- | --- |
| Recording | `● 12:41 · 84 lines · live` | — | Levels, lag, backlog, Bookmark, Stop |
| Stopping | `Finishing live transcript…` | — | Disabled |
| Processing | `Refining speaker labels` | Provisional: speaker labels may change | Stage, percent, "about 6 min left" |
| Complete | `Final · 42 min · 203 lines` | Conflicts and unnamed speakers, if any | Bookmark this line, Export |
| Interrupted | `Recovered after a crash · final` | — | As Complete |
| Final pass failed | `Final pass failed` | The error text, with Retry | Showing the live transcript |

**Interactions**

| Target | Action | Result |
| --- | --- | --- |
| ▶ on a line | Click | Play the line; ▶ becomes ■; click again or `Esc` stops |
| Line | Click | Select; `Space` replays it |
| Line | Right-click | This line is… ▸ · Bookmark this line as… ▸ · Add to vocabulary… · Play · Copy |
| Speaker chip | Click | Name… · Hear this voice · Remove name |
| Mark row | Click | Edit label, change type, delete, play from 10 s before |
| Bookmark button | Click / arrow | Plain bookmark / menu of the four types with their hotkeys |
| Transcript | Scroll up | Auto-scroll pauses; "↓ N new lines" appears |

**Dialogs** (`gui/dialogs.py`): name picker (entry plus a list of names already used in
this meeting, `Enter` accepts); export (format, times, join same-speaker lines; Copy to
clipboard, Save As…); delete confirmation (custom, because it needs the "Delete
permanently" check box and the per-meeting list); mark editor.

**Tray menu (final shape):** status line · Start recording… · Stop recording · Bookmark ▸
(four types) · Mute me in the recording ✓ · Transcript window · Meetings… · Settings… ·
Quit. Icons gain `muted` (recording icon with a struck-through mic) and a one-second
`flag` after a bookmark.

**Settings dialog** becomes a `ttk.Notebook`: General (today's fields), Vocabulary (WP3),
Hotkeys (WP5), Storage (WP9).

### 4.6 Final-pass design

Refactor `_voice_labels` into three steps so they can be tested and timed separately:
`embed_pieces(pieces, embed) → (vecs, weights, idx)`, `label_voices(vecs, weights, …) →
labels`, then the existing neighbour assignment for pieces too short to fingerprint.

**Gated fold (WP1).** `fold_small_clusters(labels, vecs, weights, *, always_fold_s=4.0,
min_similarity=0.70, protected=None)`: a cluster under `always_fold_s` is folded as today;
a larger small cluster is folded only if it is at least `min_similarity` like its target;
a cluster in `protected` is never folded.

**Rules from names (WP8).** `cluster_voices(vecs, weights, threshold, names=None)`, where
`names[i]` is the user's name for piece `i` or `None`:

- Each cluster carries the set of names of its members.
- When the best pair has two different names between them, set that pair's similarity to
  −∞ and continue; after every merge, set −∞ between the merged cluster and every cluster
  holding a different name. Nothing else about the linkage changes, so with no names the
  result is identical to today's.
- A cluster containing any named piece is `protected` in the fold.
- WP11 (L39) adds the pre-merge of same-name pieces before the loop (done; built after WP9d).

**Which pieces are named.** `tags.piece_names(pieces, live_segments, tag_state, vecs)`:
give each piece the name of the tagged live range it overlaps most (same stream). A
line-level tag always counts. A speaker-level tag counts only if the piece is at least
0.70 like the weighted centroid of all pieces carrying that name (one pass, no iteration).

**Mapping (WP8).** After the final file is renamed into place,
`tags.map_final_speakers(final_segments, tagged_ranges)` sums overlap per final speaker
and name; the largest wins; a second name with at least 25% of the winner's overlap is a
conflict. `meeting.merge_speakers` re-reads `speakers.json`, overlays the user's names,
keeps every other entry, and writes `from_tags` and `conflicts`. Order: final file →
`speakers.json` → `meta.json` complete → `DONE`.

### 4.7 Recorder changes

```text
capture ─► Recorder._on_audio(source, samples)
             mic only: samples = MicGate.process(samples)      # zeros while muted
             level[source] = peak(samples)                      # for the meters
           ─► stream worker: FlacBuffer.write · RecentAudio.write · VAD
           ─► transcribe loop:
                 result = transcriber.transcribe(utt.audio, prompt=current_prompt)
                 if playback.matches(utt, result.text): drop, count, continue
                 speaker = clusterer.assign(embedding, speech_seconds_from_words)
                 → words.jsonl → merger → transcript.jsonl
```

- **`MicGate`** counts mic samples as they pass. `mute()` zeroes from the next chunk and
  records the start sample; `unmute()` keeps zeroing for a further 250 ms (audio already
  in the capture pipeline when the user pressed) and records the end. Because the gap is
  taken from sample counts, the `gaps` entry in `meta.json` matches the silence in
  `mic.flac` exactly. Everything downstream (FLAC, ring, VAD, transcription, fingerprints)
  only ever sees the gated samples, which is what L22 requires.
- **`RecentAudio`** is written by the stream worker, after the gate. `read(start_ms,
  end_ms)` returns `None` when the range has left the 10-minute window.
- **Prompt.** `Recorder.set_vocabulary(terms)` swaps the prompt string under a lock and
  appends the meeting time to `vocabulary_changed_at_ms`.
- **Playback drop** happens in the transcribe loop, before speaker assignment, so a
  replayed clip never reaches the live speaker clustering.

## 5. Decisions made in this plan

| # | Topic | LIVE_VIEW / TODO says | This plan | Why |
| --- | --- | --- | --- | --- |
| 1 | Order | Milestones 1–9 | Final-pass fixes and vocabulary first | They fix the transcript the user already relies on, and need no UI |
| 2 | Playback drop rule | "in the writer, next to the echo dedupe" | In the transcribe loop, before `_speaker()` | Speaker assignment happens before the merger; dropping later would already have fed the clip's voice to the live clustering (L17) |
| 3 | Named small clusters | Never folded into a cluster with another name | Never folded at all | Folding a named 28 s cluster into an unnamed large one would hand the name to the wrong person |
| 4 | Naming after `DONE` | "speaker naming works there too" | Speaker-level only; "This line is…" is disabled in the final view | `speakers.json` maps labels, not lines, and the transcript isn't edited. Per-line fixes after `DONE` need the "merge / split" brainstorm item |
| 5 | Sidecar writers | A small writer with the transcript writer's lock discipline | Open, append, fsync, close per event, under the per-folder lock | Bookmarks and names are also written for past meetings, when no recorder exists |
| 6 | Unmute | Immediate | Zeroing continues 250 ms after unmute | Audio captured before the press is still in the pipeline |
| 7 | Clip output | One PyAudioWPatch output stream | A `PyAudio` instance per clip, resampled to the device's rate, stopped before any capture rebind | Checked on the dev PC: a 16 kHz output stream fails with "Invalid sample rate" (the device runs at 48 kHz), and a second `PyAudio` instance survives the first one's `terminate()`. PortAudio's init is reference-counted, so an open playback instance would stop `capture._rebind` from refreshing the device list |
| 8 | Audio deletion | Final pass deletes `audio\` | Re-read `keep_audio` just before deleting; clip reads close the file at once; retry the delete once | A file open for playback survives `rmtree(ignore_errors=True)` on Windows, leaving audio the user believes is gone; L20 can be toggled during processing |
| 9 | Retry button | In the row | In the action bar and the row's right-click menu | `ttk.Treeview` rows can't hold buttons |
| 10 | Symbols | 🎤✕ in the mute bar | "MIC MUTED" text | Emoji don't render reliably on Tk 8.6 |
| 11 | Vocabulary spike | `hotwords` vs `initial_prompt` | Use `hotwords`; the spike compares prompt *wording* and budget | In faster-whisper 1.2.1 both end up in the same `<\|startofprev\|>` slot for a one-window utterance; `hotwords` also covers every window if utterances ever get longer |
| 12 | `huddle-helper finalize` | — | Keeps audio unless `--delete-audio` is passed. Since #61: follows the meeting's own Keep audio choice, which `--keep-audio` / `--delete-audio` set first | See ground rule 2 |

## 6. Work packages

Each package lists what to build, the automated tests, and what "done" means. "Replay"
means running kept audio through the real pipeline with `simulate` or the lab tool.

### WP0 Foundations

**Build**

1. `meeting.py`: a module-level registry of locks keyed by resolved folder path, used by
   `update_meta`, `add_gap`, speakers writes and a new `append_jsonl(path, event)`
   (open "a", write one line, flush, fsync, close). Add `marks_path`, `tags_path`.
2. `cli.cmd_finalize`: keep audio unless `--delete-audio`. `finalize_meeting` re-reads
   `meta.settings.keep_audio` immediately before removing `audio\`, retries the removal
   once after 1 s, and logs if anything is left.
3. Stage timings: `finalize_meeting` records wall time per stage and writes
   `meta.json` `final_stats`: `{"took_s", "audio_s", "speed", "stages": {"read_audio",
   "diarize_<src>", "merge_small", "embed_pieces", "cluster", "write"}}`. Document it in
   OUTPUT_SPEC under diagnostics.
4. `housekeeping.recover`: store `error=str(exc)` when it marks a meeting failed.
5. `tools/final_lab.py` (dev only, reads a *copy* of a meeting folder):
   - `embed <folder>`: run the production steps up to `embed_pieces` and cache vectors,
     weights and piece info as `.npz` next to the copy.
   - `sweep <folder>`: for a grid of thresholds and fold options print, per run, the
     clusters (seconds, line numbers) and whether the checks in the truth file hold.
   - `diff <folder> <baseline.jsonl>`: which lines changed speaker or text.
   - Truth files in `tests\data\truth\*.json` hold letters and line positions only, e.g.
     `{"groups": {"B": [["system", 61200], …], "C": […]}, "apart": [["B","C"]]}`.
     **Identify a line by `(source, start_ms)`, never by `seq`.** TODO.md's line numbers
     for Test #3 are the same in the live and final files (21 lines each), but its Test #4
     numbers are `seq` in the *baseline final file* (203 lines), not the live file (252
     lines): final 102 is live 133, final 196 is live 246. Final numbering also shifts
     whenever `join_fragments` changes, so convert every number to `start_ms` from the
     baseline once, here.
6. `tests/helpers.py`: `make_meeting(tmp_path, lines, *, final=None, marks=None,
   tags=None, gaps=None, status=…)` for building fixture folders.
7. Save baselines: run the current final pass on copies of Tests #2–#4 and the TTS sample
   and keep each `transcript.final.jsonl` in `lab\baseline\`.
8. Tick TODO "Mic segment timing is squeezed" (Test #1) and "Mic timing" (Test #2). The
   condition was "close if Test #4 is also clean". Checked while writing this plan: the
   fastest of Test #4's three mic lines is 150 ms per word and of Test #3's five is
   204 ms, with no near-zero-length words. Test #1's line was 112 ms per word, so the
   margin on Test #4 is small; say so in the TODO note.

**Tests.** Two `MeetingFolder` instances updating meta from two threads lose nothing;
`append_jsonl` from two threads yields only whole lines; `cmd_finalize` leaves `audio\`;
`final_stats` is present after a fixture finalize.

**Done when.** Tests pass; baselines exist; `final_lab sweep` reproduces the Test #3 table
in TODO.md at thresholds 0.60 / 0.65 / 0.70 (this proves the harness matches production).

### WP1 Final pass: stop merging real speakers; fragments

Closes TODO: *Remote D is merged into Remote B*, *A sentence end cut off onto another
speaker*, *Same-speaker fragments not joined*, *One spurious live speaker remains*.

**Build**

1. **Gated fold** as in 4.6. Ship this regardless of the threshold outcome: on its own it
   stops the Test #4 fold of a 28 s cluster at 0.64–0.75 similarity.
2. **Threshold tension (Test #3 wants 0.65, Test #4 wants ≥ 0.70).** Try in this order,
   in the lab, before touching defaults:
   - **(b) Anchors first.** Cluster only pieces of 3 s or more at 0.70; compute weighted
     centroids; assign each shorter piece to the nearest centroid if at least 0.60 alike,
     otherwise leave it for the neighbour rule. Test #3's split of speaker A at 0.70 came
     from short lines, which this takes out of the linkage.
   - **(a) 0.70 with the gated fold** and nothing else.
   - Sweep the three numbers (anchor length 2–4 s, threshold 0.68–0.74, assign 0.55–0.65)
     and pick the centre of the region that passes everything, not its edge.

   **Acceptance matrix** (numbers as written in TODO.md: Test #3's are the same in both
   files, Test #4's are baseline *final* `seq`; the truth files from WP0 hold the
   `start_ms` equivalents):

   | Recording | Must hold |
   | --- | --- |
   | Test #3 | A, B (10–12) and C (15–18) are three speakers; B and C are apart; A is one speaker |
   | Test #2 | The same two main speakers as the baseline |
   | Test #4 | Lines 102, 119, 127, 167, 196, 198 share a speaker with none of the 377 s speaker's other lines; the 74 s speaker has exactly the baseline's lines; the cluster of 170/172/176 is not folded below 0.70 |
   | TTS sample | Exactly two remote speakers (`pytest -m models`) |

   If no setting passes all four rows, ship the gated fold only, leave the threshold at
   0.65, and write the sweep table into TODO.md. If the default changes, bump
   `SETTINGS_VERSION` and add `final_voice_threshold` to `_MIGRATIONS`.
3. **`join_fragments`:** (i) a trailing `…` or `...` is not a sentence end; (ii) when the
   previous line has no closing punctuation, the gap is under 1 s, the stream is the same
   and the next line has three words or fewer, attach it to the previous line whatever
   its label. Leave the 2.6 s "default to the / The zone code address" case alone, as
   TODO says.
4. **Live clustering:** in `Recorder._speaker`, pass the span from the first to the last
   word (when word timings exist) instead of `utt.duration_s`, so a 1.6 s line padded to
   over 2 s can't found a speaker.

**Tests.** Unit tests for the gated fold (small-and-similar folds, small-and-different
stays, under 4 s always folds, protected never folds); anchors-first on synthetic
vectors; the two `join_fragments` rules, with the Test #4 shapes written as synthetic
text; live duration from words. Existing `test_cluster_voices_keeps_test3_speakers_apart`
and `test_cluster_voices_and_fold_small` keep passing (update the latter only for the new
signature).

**Done when.** The matrix holds (or the fallback above is documented), `final_lab diff`
against each baseline shows only expected changes, TODO.md is updated with results.

**Needs Bryce.** Whether Test #4's largest speaker is one person (lines 1–17, 117, 121),
and who said "So, so, so." in Test #2: neither blocks this package.

### WP2 Final pass: speed

Closes TODO: *The final transcript took 13 min 34 s after Stop*.

1. Read `final_stats` from WP0 for Test #4 on the dev PC to confirm where the time goes
   (TODO estimates about 4 min of 4 min 36 s in sherpa diarization, 35 s in clustering).
2. Try, one at a time, comparing output with `final_lab diff` and time with `final_stats`:
   - `SherpaOfflineDiarizer(merge_small=False)`: skips a full re-embedding of every turn.
     It now only influences split points; accept it if Tests #2–#4 change in no line or
     only at split points that are no worse.
   - Threads for the *final-pass* embedder and diarizer:
     `min(4, max(1, os.cpu_count() // 2))`, dropping to 1 while a meeting is recording
     (`AppController` knows). The live embedder stays at 1 thread.
   - Report progress from `embed_pieces` so the percentage doesn't sit still.
3. Only if those leave the pass slower than 0.1× the meeting's length on the dev PC
   (roughly 0.3× on the laptop, which measured about 3× slower): write up, don't build,
   the bigger option of finding split points from per-word voice changes instead of
   sherpa's offline diarization.

**Done when.** Timings before and after are in TODO.md, outputs are unchanged or
explained, and the item is left open with "confirm on the target laptop".

**Needs Bryce.** Re-run Test #4's final pass on the target laptop.

### WP3 Custom vocabulary (L33–L36)

**Spike first** (`tools/vocab_spike.py`, reads copies): re-run VAD and transcription over
Test #4's `system.flac` and `mic.flac`, and Tests #2–#3, with (1) no prompt, (2)
`hotwords="KoVa, QDM, ETL, FMS, GCP, vendor, code freeze, prod, spreadsheet, zone code,
Zentrix"`, (3) the same as a sentence ("Glossary: KoVa, QDM, …"), (4) variant 2 plus the
four attendee names. For each run report:

- hits of each expected term, and of each known mishear from TODO ("ETO",
  "bender", "co-trees", "spray cheat", and those of the internal terms);
- lines that exist only in the prompted run, and lines made only of vocabulary terms;
- names that appear where run 1 had none, with timestamps (for a human to listen to);
- total transcription time and the slowest utterance.

Choose the wording with the most recovered terms and no rise in prompt-only lines. If
names appear where nobody said them, attendee names stay out of the prompt by default
(L34's automatic add becomes opt-in). Write the table into TODO.md.

**Build**

1. `vocab.py`: `parse_terms(text)` (one per line, `#` comments, trim, case-insensitive
   de-dupe keeping the first spelling); `fit_budget(meeting_terms, settings_terms,
   count_tokens, budget=120) → (used, dropped)` with meeting terms first;
   `build_prompt(terms)`.
2. `transcribe.py`: `Transcriber.transcribe(audio, prompt: str | None = None)`;
   `WhisperTranscriber` passes it as `hotwords`; `count_tokens(text)` via
   `self._model.hf_tokenizer.encode(text, add_special_tokens=False).ids`. Update the fake
   transcribers in `tests/conftest.py` to accept `prompt`.
3. `config.Settings.vocabulary: list[str]` (`field(default_factory=list)`; additive, no
   version bump).
4. `recorder.py`: prompt as in 4.7; `Recorder.start(title, meeting_terms=())`; `meta.json`
   gets `vocabulary` (terms actually used) and `vocabulary_changed_at_ms`.
5. `app.py`: `update_settings` pushes a changed vocabulary into a running recorder instead
   of only saying "apply from the next recording"; `start(title, terms)`.
6. `ui.py`: Settings becomes tabbed and gains Vocabulary (multi-line box, live "42 of
   about 70 terms used", list of terms that would be dropped). The Start dialog gains
   "Extra terms for this meeting".
7. Guard, only if the spike shows a rise: drop a line under 1 s whose only words are
   vocabulary terms unless its confidence is above a floor chosen from the spike.
8. OUTPUT_SPEC: the two `meta.json` fields.

**Tests.** `parse_terms`, budget order and overflow, prompt reaches the transcriber, a
mid-meeting change applies from the next utterance and is recorded in meta, settings
round-trip with a list.

**Done when.** The spike table is recorded; on Test #4's audio the prompted run recovers
most of the listed terms with no increase in prompt-only lines; transcription time on the
dev PC rises by less than 10%.

**Needs Bryce.** Listen to the flagged name insertions; a real call on the target laptop
with `live_stats` p90 under 5 s.

### WP4 Transcript window, read-only, and export (L1–L4, L8–L10)

**Build**

1. `tail.py`, `transcript_model.py`, `export.py` (sections 4.4 and LIVE_VIEW "Export").
   The model already accepts marks, tags and gaps, even though nothing writes marks or
   tags yet, so later packages only add writers.
2. `app.py`: `FinalizeProgress` in state (keep the `finalizing` string for the tray);
   time-left estimate = remaining fraction × expected total, where expected total is the
   median `final_stats.speed` of the last five completed meetings × this meeting's
   length, blended with the pass's own rate once it is past 10%. No history: stage only.
   `Status.levels` (decaying peak per stream, computed in `_on_audio`).
3. `gui/`: `MainWindow`, `TranscriptView` in read-only form (rows, auto-scroll, header,
   notice bar, status bar with levels, lag, backlog; device gaps as rows), a simple
   "Open meeting…" picker until WP6, the Export dialog, DPI awareness.
4. After Stop: progress and estimate in the status bar; on `DONE`, `switch_to_final`,
   apply `speakers.json`, highlight changed lines for 10 s.
5. `tray.py`: "Transcript window" menu item; setting "Open the transcript window when
   recording starts" (default on).
6. `cli.py`: `export <folder> --format md|txt [--out FILE] [--times elapsed|clock]
   [--no-join]`; without `--out` it writes `<meeting_id>.<ext>` in the current folder.
   `tray --replay <folder>` (dev aid): the recorder is fed that folder's `audio\*.flac`
   through `FileCapture(realtime=True)`, so the window can be exercised against real
   audio without a call.

**Tests.** `JsonlTail` (partial last line, appended lines, missing file, shrink).
Model: ordering with marks and gaps, a late line landing above an earlier bookmark,
names precedence, final swap and changed-line detection. Export: golden files equal to
the two examples in LIVE_VIEW, plus live-only (provisional banner), interrupted, no
marks, a mark in a pause, clock times, CRLF in `.txt`. One GUI smoke test (create the
window, apply ops, read the text back), skipped when Tk can't open a display.

**Done when.**
- Exports of Tests #1–#4 and the TTS sample open correctly in a Markdown viewer and in
  Notepad.
- With `tray --replay` on Test #4's audio on the dev PC, `live_stats` p90 and max with
  the window open are within 10% of the same replay with it closed.
- Killing the app mid-replay and restarting shows the recovered meeting as final.

**Needs Bryce.** A real call on the target laptop with the window open the whole time:
p90 and max no worse than Test #4 (2.4 s and 6.8 s).

### WP5 Bookmarks, types and mute (L5–L7, L21–L23, L25–L27)

**Build**

1. `marks.py`: event constructors, `reduce(events) → {id: Mark}` (unknown `kind` reads as
   `bookmark`), `MarkLog(folder)` (next id from the file; the 2 s rule: same type again
   is ignored, a different type becomes `mark_kind` on the previous bookmark).
2. `hotkeys.py`: `parse("Ctrl+Alt+B")`; `HotkeyThread(bindings, on_press)` with its own
   message loop, `RegisterHotKey` with `MOD_NOREPEAT`, a per-binding success map, stopped
   by posting `WM_QUIT`; `altgr_char(combo)` via `ToUnicodeEx` (flag 0x4 so keyboard
   state isn't disturbed). Started at Start, stopped at Stop; presses go to `TrayApp.calls`.
3. `audio/gate.py` and the recorder wiring in 4.7; `mic_muted_since_ms` in meta; gap with
   `reason: "muted"` on unmute or Stop; `housekeeping.recover` closes an open mute at the
   last audio. Stop always unmutes.
4. `config.Settings.hotkeys: dict` (`bookmark`, `decision`, `action`, `question`, `mute`;
   empty string turns one off). Settings → Hotkeys tab with the registration result and
   the AltGr warning and suggestion.
5. Window: Bookmark split button, mark rows with the editor, "Bookmark this line as…",
   the mute button and amber bar, muted periods as rows, "muted" in place of the mic
   meter. Tray: Bookmark submenu, mute check item, `muted` and `flag` icons, bookmark
   count and mute state in the tooltip. No sounds anywhere.
6. Export: marks in place and grouped at the top in Markdown; muted periods as lines
   (already supported by the model; add the golden files).
7. At Stop: `meta.json` `marks` and `mark_kinds`. OUTPUT_SPEC: `marks.jsonl`, the `muted`
   gap reason with the "readers must check `reason`" note, `mic_muted_since_ms`.

**Tests.** Reducer (label, kind, delete, unknown kind), the 2 s rule, combo parsing,
`MicGate` on sample counts (mute and unmute boundaries, the 250 ms tail, muted at Stop),
an end-to-end fake recording started muted (no mic lines, silent `mic.flac`, one gap
covering the meeting, system stream unaffected), recovery of an open mute.

**Done when.** With `tray --replay`: each hotkey works while another app has focus and
doesn't take focus; bookmarks land beside the right line in the view and the export; a
one-minute mute yields exactly one minute of digital silence in `mic.flac` and a matching
gap.

**Needs Bryce.** The same on a real call (confirm nothing audible lands in
`system.flac`), and the AltGr warning on a German layout.

### WP6 Meetings view (L29–L31)

**Build**

1. `library.py`: `MeetingInfo` (id, title, started, length, status, speaker count, mark
   count, audio bytes, error, finished time); `scan(meetings_dir, cache)` re-reads a
   folder only when `meta.json`'s mtime, `DONE` or the marks file changed, and sizes
   `audio\` on a worker thread.
2. `shell.py`: `recycle(paths)` with `SHFileOperationW` (`FO_DELETE`, `FOF_ALLOWUNDO |
   FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI | FOF_WANTNUKEWARNING`, double-null
   path list); `has_recycle_bin(path)` (`GetDriveTypeW`: a remote drive has none).
3. `library.delete_meeting` / `delete_audio` (the latter sets `audio_kept: false` and
   `audio_deleted_at`); refused when `app.is_busy(id)`.
4. `AppController.retry_final`: clears `error`, queues the job; if a meeting is recording
   it waits in a separate list that is drained when Stop completes.
5. `gui/meetings_view.py`: list, filter, search, multi-select, Open, Export…, Show in
   Explorer, Delete audio, Delete meeting, Retry final pass; the confirmation dialog with
   the exact wording rules from LIVE_VIEW (what goes, whether it can come back, the
   24-hour warning, "permanently" when there is no Recycle Bin, Shift+Delete).
6. Tray: "Meetings…" replaces "Open meetings folder". "Rename speakers…" stays until WP8.

**Tests.** Scan and cache invalidation; status mapping; delete refused while busy; meta
after delete-audio; retry queued during recording and released at Stop; `recycle` on a
temp folder (the folder is gone afterwards; skipped off Windows).

**Done when.** The LIVE_VIEW milestone 3 check passes on the dev PC: force a failure by
renaming the diarization model folder, see the error, restore, Retry, complete; a retry
requested during a replay waits for Stop; a deleted meeting can be restored from the
Recycle Bin and a permanently deleted one can't.

### WP7 Click to hear (L15–L18, L20)

Build in two steps; step A carries no risk to the recording.

**A. After Stop**

1. `playback.py`: `Player.play(samples_16k, on_done)` / `stop()`: one clip at a time on a
   worker thread; open a `PyAudio` instance for the clip, resample with soxr to the
   output device's default rate, duplicate mono to the device's channels, close and
   terminate when done. `clip_from_files(folder, source, start_ms, end_ms)` reads
   `start − 300 … end + 300` ms with `soundfile` frame offsets inside a `with` block.
   Quiet clips are raised with the transcriber's gain rule (`normalize_level`).
2. Window: ▶/■, `Space`, `Esc`, "Hear this voice" (longest high-confidence line of that
   speaker, preferring 3–8 s), play from a bookmark (10 s before to 5 s after), disabled
   controls with the reason from LIVE_VIEW's table.
3. "Keep this meeting's audio" (L20): `set_keep_audio` writes `settings.keep_audio` in
   meta; honoured because of WP0's re-read.

**B. While recording**

4. `audio/ring.py` wired into the stream workers; `clip_source` prefers the ring, and
   older lines show "available after Stop".
5. Playback windows and the drop rule (4.7, decision 2), `meta.json` `playbacks` and
   `playback_segments_dropped`.
6. `WasapiCapture` takes a `before_reopen` callback, called at the top of `_rebind`;
   the controller wires it to `player.stop()` (decision 7).
7. The once-per-meeting headphone warning, and the confirmation when echo dedupe has
   already dropped mic lines.
8. Spike, written up in TODO.md: reading older audio mid-meeting via `SoundFile.flush()`
   and a second handle, versus 5-minute chunk files. Build neither until it is decided.

**Tests.** Clip bounds and padding at the file edges; ring wrap-around and out-of-window;
gain cap; drop rule (overlap and text match drops; overlap without match keeps; no
speaker created); file handle closed after a read (the folder can be deleted straight
after); `before_reopen` is called on rebind.

**Done when.** On Tests #2–#3 every line's clip starts and ends on its words. With
`tray --replay` and clips played through the default device, the played clips don't
appear as lines or new live speakers, and the counts are in meta.

**Needs Bryce.** The 20-clip real-call check with a headset, once without, and switching
the default device while a clip plays (capture must rebind and log the gap as before).

### WP8 Naming speakers (L11, L12, L14)

**Build**

1. `tags.py`: events; `reduce(events) → TagState` (speaker names by live label, line
   names by `(source, start_ms)`, later events win); `tagged_ranges`; `piece_names`;
   `map_final_speakers` (4.6).
2. `meeting.merge_speakers(folder, user_names, *, from_tags=None, conflicts=None)`.
3. `diarize.cluster_voices(..., names=)` and the `protected` fold; `finalize_meeting`
   reads the tags before clustering (rules) and again after the final file is in place
   (mapping), then writes `speakers.json`, then meta, then `DONE`. Crash recovery takes
   the same path because it calls the same function.
4. Window: chips with "Who is this?" on a new unnamed voice, the name picker, "This line
   is…" (live view only), chips merged by name, names applied to past and future lines,
   conflicts and unnamed speakers in the notice bar after the swap, naming in the final
   view writing `speakers.json`. The mic's `You` can be named the same way.
5. Export uses names from tags (live) or `speakers.json` (final): already in the model.
6. Remove `SpeakersDialog` and the "Rename speakers…" menu item. OUTPUT_SPEC:
   `speaker_tags.jsonl`, the new `speakers.json` fields, "user's names win", the new end
   order.

**Tests.** Reducer (tag, untag, line over speaker, later wins); mapping and the 25%
conflict rule; cannot-link on synthetic vectors (two named clusters at 0.9 similarity
stay apart; an unnamed cluster may join either); the 0.70 gate for speaker-level tags;
`merge_speakers` keeps a downstream name for a speaker the user didn't name; finalize
writes files in the stated order; **no tags file means byte-identical output to before**.

**Done when.** The LIVE_VIEW milestone 5 check passes by replay: Test #3 with lines 10
and 15 named differently, and Test #4 with live line 133 (LIVE_VIEW's "102", which is
its number in the final file) and one of the 377 s speaker's lines named differently,
give separate final speakers carrying the right names; Tests #2–#4 and
the TTS sample with no names match the post-WP1 baselines exactly.

**Needs Bryce.** The downstream app's reader for `speaker_tags.jsonl`, `speakers.json`
from tags and `marks.jsonl` lives outside this repo; LIVE_VIEW asks for it to be updated
alongside this package and WP5.

### WP9–WP11 Later

- **WP9 P1 polish.** One small branch each: attendee list, number keys and the "Who is
  this?" prompt (L13); Play from here, output device for clips (L19); mute reminders and
  Start muted (L24); user-defined bookmark types (L28); Storage panel (L32); "Add to
  vocabulary…" on a line (L37) and vocabulary import/export (L38); "short" speakers
  dimmed (L40).
- **WP10 Clips never captured.** A spike before any commitment: process-loopback capture
  (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`, excluding this process tree) through comtypes,
  proven on a 30-minute capture with a device switch. It replaces the system stream's
  capture path, so it re-runs the device-switch and soak checks.
- **WP11 Same name, same speaker (L39).** Pre-merge same-name pieces before the linkage
  loop in `cluster_voices`; re-run the WP8 checks.

## 7. Needs Bryce

Nothing here blocks the first packages; each item names the package that is waiting on it.

| # | What | For |
| --- | --- | --- |
| 1 | Who spoke Test #4 lines 1–17, 117 and 121 (is the 591 s speaker one person?) | TODO, after WP1 |
| 2 | Who said "So, so, so." in Test #2 line 1 | TODO, after WP1 |
| 3 | A new recording with a known number of speakers and Keep audio on (Test #1's audio is gone) | TODO "is 4 speakers right" |
| 4 | Was the 21-second "Test #2" false-start folder deleted by hand? | TODO housekeeping |
| 5 | On the target laptop: final-pass time after WP2; `bench` base.en vs small.en; raise the Kiyo Pro's input level | WP2, TODO |
| 6 | Listen to name insertions flagged by the vocabulary spike | WP3 |
| 7 | Real calls on the target laptop: window open (WP4), hotkeys and mute (WP5), clips with and without a headset (WP7), vocabulary (WP3) | Each package's last check |
| 8 | German keyboard layout check for the AltGr warning | WP5 |
| 9 | Update the downstream app's reader for the new files | WP5, WP8 |
| 10 | PRD leftovers: 30-minute capture spike, 2-hour soak, default-device switch mid-call, echo dedupe on real speakers | TODO; the device switch also gates WP7 step B |

## 8. TODO.md items and where they go

| Open item | Disposition |
| --- | --- |
| Test #1: is 4 speakers right | Needs Bryce 3 |
| Test #1, #2: mic segment timing squeezed | Close in WP0 step 8: Tests #3 and #4 are clean |
| Test #1, #4: base.en mishears names and jargon | WP3; small.en benchmark is Needs Bryce 5 |
| Test #2: final pass relabels lines the live pass got right | Re-check after WP1 with `final_lab diff`; truth is Needs Bryce 2 |
| Test #2: false-start folder | Needs Bryce 4 |
| Test #3: one spurious live speaker | WP1 step 4 |
| Test #4: Remote D merged into Remote B | WP1 steps 1–2; WP8 makes it certain when both are named |
| Test #4: `SPEAKER_01` may be several people | Needs Bryce 1 |
| Test #4: sentence end cut off; fragments not joined | WP1 step 3 |
| Test #4: quick back-and-forth inside one line | No change (by design) |
| Test #4: final-pass speed | WP2 |
| Test #4: quiet Kiyo Pro mic | Needs Bryce 5 |
| PRD milestones (spike, soak, device switch, echo) | Needs Bryce 10 |

## 9. Risks to watch while building

| Risk | Where it bites | Guard |
| --- | --- | --- |
| A threshold that fits four recordings is overfitted | WP1 | Pick the middle of the passing region; the gated fold and WP8's rules don't depend on the threshold |
| The window costs the transcriber CPU on the laptop | WP4 | Byte-offset tailing, 200 events per tick, no file I/O on the 500 ms timer; replay comparison in "Done when" |
| Playback interferes with capture rebinding | WP7 | Per-clip `PyAudio`, `before_reopen`, the device-switch check; fall back to `winsound` on the default device if it can't be made safe |
| Deleted audio survives because a file was open | WP7, WP0 | Short-lived reads, delete retry, test that deletes right after a read |
| Hotkey thread outlives a crash in Stop | WP5 | Stop the thread in a `finally`; registrations die with the thread in any case |
| DPI awareness changes the existing dialogs | WP4 | Check Start, Settings and the tray menu on the packaged exe at 100% and 150% |
| PyInstaller misses a new module | Every package | Ground rule 8 |
| A vocabulary prompt adds names nobody said | WP3 | Spike variant 4 decides whether attendee names enter the prompt at all |
