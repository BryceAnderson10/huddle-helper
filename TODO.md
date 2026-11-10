# TODO

Items to investigate or correct, from real-world tests. Check items off as they're fixed
and note the commit.

## From Test #1: `2026-11-09_1324_test-meeting-1`

Setup: packaged tray app (`dist\HuddleHelper\HuddleHelper.exe`), base.en, HyperX headset
(mic + loopback), recorded work-meeting video as the remote side, 3 min 54 s.
Keep audio was off, so the audio for this meeting is gone.

What went well: no errors or audio gaps, transcription kept up live (empty backlog at
Stop), final transcript 35 s after Stop, and the final pass found 4 distinct voices.

### Speaker labels

- [x] **Live labels don't separate real voices.** All 29 system lines in `transcript.jsonl`
  are `SPEAKER_01`; the final pass found 4 speakers. Real voices over compressed call audio
  are more similar than the synthetic TTS voices `live_speaker_threshold` (0.6) was tuned on.
  *Investigate:* measure same-speaker and different-speaker embedding similarity on kept
  real audio, then retune the threshold and `min_new_speaker_s`, or compare against the
  centroids from the last N utterances. Code: `diarize.py` `OnlineClusterer`.
  → Investigated in Test #2 (see "Live labels: threshold is too low" below). Fixed in 079d8c9.
- [x] **Final pass splits sentences mid-way.** The refined labels split a sentence between
  speakers: "…part of 12.x I think so I did" (`SPEAKER_03`) / "the same with that as well."
  (`SPEAKER_02`).
  *Fix:* only split at a pause or sentence boundary, and don't split off pieces shorter than
  about 1 s / 3 words. Code: `finalize.py` `_split_by_words`. Fixed in 079d8c9.
- [x] **Final pass flips speaker word by word.** "Who could look at first? I found it. I
  found it. So this is the one…" came out as 4 segments alternating `SPEAKER_01` /
  `SPEAKER_03`. Same fix as above: fold short runs into the neighbouring speaker.
  Fixed in 079d8c9 (this exact case is a unit test).
- [ ] **Check whether 4 speakers is right** for this clip (it was 1 in live, 4 in final).
  Needs a recording with known speakers and Keep audio on. `final_cluster_threshold` is 0.6.
  Small look-alike speakers are now merged (079d8c9), which may change the count. The best
  check is a recording where you know how many people speak.

### Microphone

- [x] **Only one mic line was captured:** "This is a test. Yes." (confidence 0.39).
  *Investigate:* confirm how much was said into the mic. If more, check the mic input level
  and the VAD threshold (`vad_threshold` 0.5) and minimum-speech rule on real mic audio.
  → Test #2 showed the mic records very quietly (see "Mic level is very low" below). That is
  the likely cause here too. Quiet audio is now boosted before transcription, and the tray
  warns when the mic is too quiet (079d8c9).
- [x] **Mic segment timing is squeezed:** 5 words in 0.56 s (57048–57608 ms). The start and
  end come from Whisper word timestamps clamped to the utterance; one of them is probably
  wrong. Code: `recorder.py` `_transcribe_loop`.
  → Not seen in Test #3: all 6 mic lines have normal word lengths (60–500 ms). Likely the
  quiet-audio problem behind Test #2's hallucination; close if Test #4 is also clean.
  → Closed: Test #4's fastest mic line is 150 ms per word (three lines) and Test #3's is
  204 ms, with no near-zero-length words. Test #1's line was 112 ms per word, so the margin
  on Test #4 is small; reopen if it shows up again.

### Measurement

- [x] **Record line delay for tray recordings.** The tray app doesn't save how long lines take
  to land after speech ends, so the PRD's p90 under 5 s can't be checked from a meeting
  folder. Write median/p90 delay and maximum backlog to `meta.json` at Stop. (`record` and
  `simulate` on the command line already print it.)
  Fixed in 079d8c9: `meta.json` → `live_stats` has `line_delay_ms` (median, p90, max),
  `max_backlog`, `max_lag_s` and `speech_peak_median` per stream.

### Transcription quality

- [ ] **base.en mishears names and jargon**, e.g. "Gitli Cluster" (probably "GitLab").
  *Investigate:* run `huddle-helper bench` with small.en on the target laptop. If it keeps
  up with real time, consider making it the default.

### Process

- [x] Turn on **Settings → Keep audio after the final transcript** for test recordings, so
  findings can be reproduced and tuned against the real audio. (On for Test #2.)

## From Test #2: `2026-11-09_1334_test-2-2`

Setup: same as Test #1, with Keep audio **on**, so `audio\mic.flac`, `audio\system.flac` and
`audio\words.jsonl` are available. Recorded work-meeting video plus two mic lines spoken
over it to test overlapping speech. Length 3 min 46 s; 41 live lines, 51 final lines.

What went well: no errors or gaps, final transcript 36 s after Stop, and transcription kept
up live. Overlapping speech works: mic lines spoken over the video were captured and sit
in the correct time order. No echo lines were dropped, which is correct with a headset.

### Live labels: threshold is too low (root cause of Test #1 item)

- [x] **Raise `live_speaker_threshold` from 0.6 to about 0.72–0.75.** Fixed in 079d8c9: 0.75,
  `min_new_speaker_s` 1.5, and a settings version so the 0.6 already saved in
  `settings.json` is replaced. Measured on the kept
  system audio, grouping lines by the after-Stop pass's speakers:

  | Line pairs | Similarity p10 / p50 / p90 |
  | --- | --- |
  | Same speaker | 0.64 / 0.77 / 0.88 |
  | Different speakers | 0.43 / 0.59 / 0.71 |

  Agreement of live labels with the after-Stop pass, by speech time, replaying this meeting:
  55–58% at thresholds 0.6–0.70 (everything collapses into 1–2 speakers); **96–97% at
  0.72–0.80**. 0.72 is right at the edge of that jump, so prefer about 0.75, and keep
  `min_new_speaker_s` around 1.0–2.0 (2.0 gives fewer spurious one-off speakers). Also
  re-check against the synthetic TTS meeting, where same-speaker pairs scored 0.75–0.9 and
  different speakers 0.3–0.55. Code: `config.py` default, `diarize.py` `OnlineClusterer`.

### Final pass

- [x] **Mid-sentence splits are frequent** (same issue as Test #1, more examples): "…we
  can't do it like right" / "now, which I understand." · "Yeah, that's the" / "goal is we
  want to…" · "…but I did want to" / "share one thing that kind of came in." · "Oh you like" /
  "that's what I was going for." Splits fall between words with no pause, so they come from
  diarization turn boundaries rather than real speaker changes. Fix as in Test #1.
  Fixed in 079d8c9; on the Test #2 replay, all four of these now stay whole.
- [x] **Tiny spurious speakers.** The offline pass found 5 speakers, but 3 of them have only
  2 s, 9 s and 10 s of speech in total; they show up as `SPEAKER_03`–`SPEAKER_05` on a few
  fragments. *Fix:* merge clusters with under about 10 s total (or under ~5% of speech) into the
  nearest larger speaker, or raise `final_cluster_threshold`. Code: `finalize.py`.
  Fixed in 079d8c9: a speaker with under max(15 s, 5% of speech) is merged into the most
  similar larger speaker if they're at least 0.7 alike. Raising the threshold instead would
  have merged the two synthetic test voices (0.50 alike). The Test #2 replay now has 2 speakers.
- [ ] **Final pass also relabels lines the live pass got right**, e.g. line 1 is split into
  `SPEAKER_01` "…less descriptive than that." / `SPEAKER_02` "So, so.". Revisit after the
  two fixes above. On the replay the split remains, but now at the sentence end; whether
  it's right needs someone who knows who said "So, so, so." Re-check on the next test.

### Microphone

- [x] **Mic level is very low.** In `mic.flac` the peak is 0.047 (about −27 dBFS) and the
  overall RMS is 0.0006. Silero VAD still found both spoken lines, and they were the only
  sound above the noise floor (140–142 s and 165–167 s), so nothing said was lost this time.
  But Whisper is less reliable on audio this quiet. *Fix:* normalize each mic utterance to
  a target level (with a gain cap) before transcription, and warn in the tray when the
  mic stays very quiet. Also check the Windows input level for the HyperX mic.
  Fixed in 079d8c9: utterances are boosted toward a 0.5 peak (max 30×), and the tray warns
  once per meeting when mic speech peaks below 0.05. Raising the Windows input level is
  still the best fix.
- [x] **Whisper hallucinated a repetition loop on the quiet mic.** Live line 26: "Good to see
  how does it handle overlapping voices? Alright, let's go. Alright, let's go check this
  one." ×5, squeezed into 2.5 s. Re-transcribing the same kept audio offline gives the clean
  "…to see how does it handle overlapping voices?". *Fix:* level normalization (above),
  plus a repeated-phrase filter, and try faster-whisper's `no_repeat_ngram_size`,
  `repetition_penalty` and `hallucination_silence_threshold`. Code: `transcribe.py`.
  Fixed in 079d8c9: the looped words were 0–20 ms long, so sentences averaging under 60 ms
  per word are dropped, and a third identical sentence in a row is dropped. Replaying
  Test #2 gives "Good to see how does it handle overlapping voices?". The faster-whisper
  options weren't needed.
- [x] **Mic timing.** The two mic lines here had sensible timings (2.1 s and 2.5 s). The
  squeezed timing from Test #1 may also be a hallucination artifact; re-check after the
  fixes above.

### Housekeeping

- [ ] A 21-second false start ("Test #2", 13:34:09–13:34:30, 4 lines) appears in the log, but its
  folder is gone. If it was deleted by hand, nothing to do. Otherwise, find out why.

## From Test #3: `2026-11-09_1441_test-3`

Setup: rebuilt exe with the 079d8c9 fixes, Keep audio on, same headset. Recorded meeting
video plus three spoken mic passages (start, one over the video, end). 1 min 47 s; 21 lines.

What went well:
- **First `live_stats` from a real recording:** line delay median 1.0 s, **p90 1.4 s**
  (PRD target 5 s), max 2.0 s; backlog never above 1; max lag 1.8 s. No gaps or errors.
- **Mic:** speech peaks at 0.074, about double Test #2. No quiet-mic warning, which is
  correct. Every mic speech region was transcribed; no hallucinations.
- **Final pass:** no segment was split (final text and timings equal the live file).
- **Overlap:** the mic line spoken over the video (32.3 s) is captured and in order.

### Final pass merged two different people (regression risk in the new merge)

- [x] **The offline clustering put two speakers together, and the small-speaker merge hid
  the rest.** The voice evidence for the remote lines:

  | Line group (live label) | Similarity within group | Similarity to the other group |
  | --- | --- | --- |
  | B: lines 10–12 (`SPEAKER_04`) | 0.83–0.88 | 0.49–0.74 to C |
  | C: lines 15–18 (`SPEAKER_05`) | 0.81–0.95 | 0.49–0.74 to B |

  So B and C are different people, and the live labels had them right. **Confirmed by Bryce:**
  "I was timing myself" and "Eric pointed this out" are two different people. The sherpa clustering
  (threshold 0.6) put most of B (lines 11–12) in one cluster with C, and B's first 6 s
  (line 10) in its own cluster. The 079d8c9 merge then folded that 6 s cluster into the B+C
  cluster (similarity 0.71, just over 0.7), so the final file says lines 10–18 are all
  `SPEAKER_02`.

  *Proposed fix (tested offline, not implemented):* label final lines by clustering the
  transcript lines themselves over the whole meeting. Use average-linkage clustering of each
  line's voice fingerprint (weighted by duration), with the same embedding model as the live
  pass. Results on the three kept recordings:

  | Threshold | Test #3 (B ≠ C expected) | Test #2 | TTS sample (truth known) |
  | --- | --- | --- | --- |
  | 0.60 | 2 clusters, B+C merged ✗ | 3 (2 main + 1 single line) | correct |
  | **0.65** | **3: A / B / C** ✓ | 4 (same 2 main as today + 2 single short lines) | correct |
  | 0.70 | 5 (A split, short lines alone) | 5 | correct |

  So: 0.65, then fold clusters that are only one or two short lines into the closest
  cluster. Keep sherpa's turn boundaries only for splitting a line where the speaker
  changes mid-line, which the 079d8c9 phrase rules already limit. Then re-run all three
  recordings and the TTS e2e test. Code: `finalize.py`, `diarize.py`.
  **Fixed in 601d147** as proposed (`final_voice_threshold` 0.65). Re-running the final pass
  on the kept recordings:
  - **Test #3:** A, B and C are three separate speakers. B ("I was timing myself") and
    C ("Eric pointed this out") are no longer merged. Line 3 ("Really want to go deep.", 1 s)
    goes with B; that hasn't been checked.
  - **Test #2:** the same two main speakers as before.
  - **TTS sample:** correct.

  Line 3 is checked against the confirmed answer in a unit test that uses the measured
  similarities.
- [x] Until then, consider raising the small-speaker merge's `min_similarity` from 0.7 to
  about 0.75. Test #3's wrong merge was at 0.71; Test #2's justified merges were 0.83–0.90.
  Done in 601d147. The merge now only affects where lines may split, not labels.

### Live labels

- [x] **Short opening lines create extra live speakers.** Lines 3 (1.0 s) and 4 (1.6 s) became
  `SPEAKER_01` and `SPEAKER_02`, then the first long line of the same voice became
  `SPEAKER_03`. Fingerprints of lines this short are unreliable (0.57–0.70 similar to their
  own speaker's long lines). The rest of the live labels were right: A, B and C each got one
  label. *Ideas:* don't let a line under ~2 s found a new speaker unless no speaker exists yet,
  and let a provisional speaker built from short lines be absorbed by the next confident
  match. Live labels are provisional, so this is lower priority than the final pass.
  Partly fixed in 601d147: both ideas are in (new speaker needs 2.0 s; a speaker known from
  under 2 s of audio adopts a long line at 0.6+ similarity). The Test #3 replay goes from 5 live
  speakers to 4, with A, B and C each on one label.
- [x] **One spurious live speaker remains on the Test #3 replay:** line 4 ("Maybe it's
  technically complex.", 1.6 s of speech). The live pass measures the utterance including its
  VAD padding and trailing silence, so it counted as over 2 s. *Idea:* measure from the first
  to the last word. Low priority; the final file has it right.
  Fixed in WP1: the live pass now measures a line from its first to its last word. Unit-tested;
  not yet re-checked on a replay.

### Readability

- [x] **Mic sentences are broken into fragments at natural pauses.** "Alright, this is to kick
  off and initiate test number three." / "of Huddle Helper." and "we're going to close out." /
  "the" / "Test number three." The pauses are real (0.8–1.2 s, longer than the 500 ms that
  ends an utterance), and Whisper adds a full stop to each piece.
  *Fix (final file only, since the live file is append-only):* join consecutive lines from the same
  source and speaker when the gap is under ~1.5 s and the next line starts in lower case or the
  previous one has no closing punctuation, and drop the spurious full stop. Expected result:
  "…initiate test number three of Huddle Helper." and "we're going to close out the test
  number three." Code: `finalize.py`.
  Fixed in 601d147, with a 2 s gap limit (the first example's pause was 1.6 s). On the replay,
  the final file reads "Alright, this is to kick off and initiate test number three of Huddle
  Helper." and "Alright, we're going to close out the Test number three." The same rule
  also joins remote lines cut mid-sentence, e.g. "and that feels about average, but we've had"
  / "how many speakers…".

## From Test #4: `2026-11-09_1605_work-recording-2` (target user's laptop)

Setup: a real work call recorded on the target user's laptop. Mic: Razer Kiyo Pro (webcam
mic); system: Realtek headphones; base.en; Keep audio on. 22 min 41 s; 252 live lines,
203 final lines. The laptop's user spoke only 3–4 times; the rest is remote speech. From names
used on the call, there were at least 5 remote people: Remote A and Remote B (the QDM team), Remote C,
Remote D and probably Remote E. A copy of the folder is in `Downloads\testmeeting`.

What went well:
- **No gaps, errors or crashes**, and a clean exit. Echo dedupe correctly dropped nothing
  (headphones).
- **Live speed held up on the laptop over 22 minutes:** line delay median 1.3 s, **p90
  2.4 s** (PRD target 5 s), max 6.8 s; backlog never above 2; max lag 6.6 s.
- **Remote C is labelled correctly throughout:** every line of his has `SPEAKER_02` in the final
  file, and nobody else's does.
- The mic question split over two live lines is joined in the final file (601d147 working).

### Final pass: a real speaker was folded away (high priority)

- [x] **Remote D is merged into Remote B (`SPEAKER_03`).** Final result: 3 speakers —
  `SPEAKER_01` 591 s (Remote A plus others), `SPEAKER_02` 74 s (Remote C), `SPEAKER_03` 377 s
  (Remote B + Remote D). Remote D's lines form a clean, separate voice cluster: 6 lines, 28 s,
  lines 102 ("Yeah, so I can provide that." → "Thank you, Remote D"), 119, 127, 167, 196
  ("…Colleague G and I for now") and 198. They're only 0.64–0.75 similar to Remote B's centroid, the
  same range as clearly different speakers in Test #2. Two things merge them, and both need
  fixing:
  1. **The threshold.** At `final_voice_threshold` 0.65, average linkage already merges
     Remote D's lines into Remote B's cluster. From 0.70 to 0.80 they're a separate cluster every
     time.
  2. **The fold has no similarity check.** Even at 0.70, Remote D's 28 s is under the fold
     limit (3% of 1035 s of speech = 31 s). `fold_small_clusters` would then give it to the
     most similar large cluster however unlike it is. A second small cluster (lines 170, 172
     and 176, 13 s, "But Remote C and Colleague F's can keep you updated…") is folded the same way. The
     live pass also gave it its own label (`SPEAKER_06`), so it may be another person.

  → Re-measured with `tools/final_lab.py sweep` on cached fingerprints (WP0). Two details
  differ from the notes above: (1) from 0.70 up, Remote D's cluster also holds one line the
  0.65 result gives to Remote B (final line 67, 6.8 s), which makes it 35 s, over the fold limit
  (29 s: 3% of the 980 s of speech that has fingerprints), so at 0.70 it is *not* folded;
  (2) in Test #3 at 0.60 it is A and C that merge, not B and C, and at 0.70 only the two
  short lines (3 and 4) sit alone, which the fold then absorbs: A, B and C still come out
  as three speakers. So 0.70 with today's fold passes Test #3. Who spoke line 67 needs a
  listen.

  **Fixed in WP1:** `final_voice_threshold` 0.65 → 0.70, and `fold_small_clusters` now folds a
  cluster of 4 s or more only if it is at least 0.70 like its target. Re-running the final
  pass on the kept recordings (`tools/final_lab.py`):
  - **Test #4:** five remote speakers instead of three: 582 s, 74 s (Remote C, exactly the same
    lines as before), 343 s (Remote B), 38 s (Remote D's six lines plus line 67) and 13 s (lines
    170/172/176, kept apart by the similarity check).
  - **Test #3:** A, B and C are still three speakers. Line 3 (1 s, unchecked) moved from B to A.
  - **Test #2:** the same two main speakers.
  - **TTS sample:** correct.

  **The margin is thin.** Everything passes only from 0.685 to 0.71. Below that Remote D
  merges into Remote B; from 0.715 other lines join Remote C's speaker; from 0.73 Test #2's two
  main speakers change. "Anchors first" (option b) passed at no setting and was not built.
  A fixed threshold fitted to three recordings will miss on others; naming speakers (LIVE_VIEW
  L14) is what makes it certain. Re-check on every new kept recording.

  **Tension with Test #3:** there, 0.70 split speaker A into pieces (see the table under
  Test #3), which is why 0.65 was chosen. A fixed threshold that works for both may not
  exist. *Options to test:* (a) 0.70 plus a similarity-gated fold (always fold clusters under
  ~4 s; fold larger ones only if ≥ ~0.7 like the target), checked on Tests #2–#4 and the
  TTS sample; (b) build cluster centroids only from lines over ~3 s, whose fingerprints are
  steadier, then assign short lines to the nearest centroid. Test #3's A pieces were short
  lines. Code: `diarize.py` `cluster_voices`, `fold_small_clusters`; `finalize.py`
  `_voice_labels`.
- [ ] **`SPEAKER_01` may still be several people.** Remote A, the person who opens the call ("I'm
  just trying to capture the business need…") and the lines that sound like Remote E ("…Remote D,
  feel free to correct me", "So Remote A, Remote B, early Q1, does that work for you guys?") are all
  0.82–0.92 alike, as similar as Remote A is to himself, and they stay together even at 0.8. So
  either they're the same person, or this embedding model can't tell these voices apart over call
  audio. *Needs:* someone who was on the call to say who spoke lines 1–17, 117 and 121.

### Final pass: fragments and mixed lines

- [x] **A sentence end cut off onto another speaker.** "Can we get a copy of that or is it just
  a dump of the" (`SPEAKER_03`) / "GCP table." (`SPEAKER_01`); "I'm kind of just getting…"
  (`01`) / "validation from the FMX people here." (`02`); "…we can always get the address from
  joining you." (`02`) / "Another table." (`01`). The short second piece is too short to
  fingerprint reliably, so it takes a neighbour's label, which can be the wrong neighbour.
  `join_fragments` only joins the same speaker. *Idea:* when the previous line has no closing
  punctuation, the gap is under ~1 s and the next line is very short (≤ 3 words), attach it
  to the previous line whatever its label.
  Fixed in WP1 as proposed ("…dump of the" / "GCP table." is now one line). The other two
  examples aren't covered: their first line ends with a full stop or "…". A line that
  trails off with "…" doesn't count, because the short line after it is usually someone
  else's reply.
- [x] **Same-speaker fragments not joined:** "…all we would have to do is build out the…" /
  "Quodium side in the ETO process." Whisper's trailing "…" counts as closing punctuation.
  *Idea:* don't treat "…" as a sentence end in `join_fragments`. (Also "default to the" / "The
  zone code address" was not joined because its gap is 2.6 s; leave that as is.)
  Fixed in WP1: 14 such pairs are joined in Test #4 (203 lines → 190), 2 in Test #2, 1 in
  Test #3. The "…" stays in the joined text.
- [ ] **Quick back-and-forth stays inside one line** (low priority, by design): "…preload QDM?
  Yes. Yes.", "I think that is CSV. Yeah. Okay. Okay.", "…you're all good." Interjections under
  1 s / 3 words are folded into the surrounding line on purpose (Test #1 fix), so a short reply
  from someone else is attributed to the main speaker. Revisit only if the downstream app needs it.

### Speed of the final pass on the laptop

- [ ] **The final transcript took 13 min 34 s after Stop** for a 22.7-minute meeting
  (16:28:01 → 16:41:35), 0.6× the meeting's length. The PRD accepts "several minutes", but a
  1-hour meeting would take about 35 minutes before `DONE`. Re-running the pass on the dev
  PC (Ryzen 5 5600X) takes 4 min 36 s and gives the same 203 lines. About 4 min of that is
  sherpa's offline diarization (segmentation, embeddings, `merge_small_speakers`); voice
  clustering of the 181 pieces takes about 35 s. So the laptop is roughly 3× slower than the
  dev PC, and the diarization step is where the time goes.
  → Measured with `final_stats` on the dev PC (WP0): 267 s in total, of which **232 s is
  sherpa's offline diarization**, 34 s is fingerprinting the pieces and clustering is
  instant. Tests #2 and #3 have the same shape (0.19–0.23× the meeting's length).
  *Ideas:* labels now come from `cluster_voices`, so sherpa's offline diarization is only used
  for split points. That pass embeds the audio twice more: inside sherpa and again in
  `merge_small_speakers`. Run the embedders with more than 1 thread (`SherpaEmbedder`
  defaults to `num_threads=1`), skip `merge_small_speakers` (it no longer affects labels), or
  find split points from per-word voice changes instead. Measure each stage before choosing.
  → **WP2 (dev PC, 12 logical cores, Test #4, 1361 s of audio).** The final pass now skips
  `merge_small_speakers` and runs the diarizer and the fingerprinting on
  `min(4, cores // 2)` threads, or 1 thread if a recording is running when the pass loads
  its models. Output is identical to the WP1 reference in every run (190 of 190 lines;
  Tests #2 and #3 also unchanged with the merge step off).

  | | Total | × meeting length | Diarization | merge_small | Fingerprinting |
  | --- | --- | --- | --- | --- | --- |
  | Before (2 and 1 threads, merge on) | 319 s | 0.23 | 259 s | 25.5 s | 34 s |
  | Merge off, same threads | 308 s | 0.23 | 273 s | – | 34 s |
  | Merge off, 1 thread | 398 s | 0.29 | 363 s | – | 34 s |
  | Merge off, 2 threads | 271 s | 0.20 | 248 s | – | 22 s |
  | Merge off, 4 threads (new default here) | 197 s | 0.15 | 180 s | – | 17 s |

  Timings vary by 10–15 s between runs. **Still open:** 0.15× is above the 0.1× target,
  and 180 of the 197 s is sherpa's own diarization, so the remaining gain needs the bigger
  change (split points from per-word voice changes). Not yet measured on the laptop, and
  Tests #2 and #3 were not timed at 4 threads. "Grouping voices" now shows progress while
  the pieces are fingerprinted, and `final_stats` has a `merge_small_<source>` stage.

### Transcription quality

- [ ] **Domain jargon is misheard throughout** (base.en): KoVa → "cover" / "co-va" /
  "Co-ver", QDM → "Quodium" / "odium", ETL → "ETO", FMS → "FMX" / "famous" (fleet),
  vendor → "bender", code freeze → "co-trees", prod → "abroad", spreadsheet → "spray cheat",
  zone codes → "zonkers". The meaning of whole sentences is lost for a downstream summary.
  *Ideas:* (1) a **custom vocabulary** setting passed to faster-whisper as `hotwords` or
  `initial_prompt` (e.g. "KoVa, QDM, ETL, FMS, GCP, zone code, Zentrix"); test on this
  meeting's kept audio. (2) small.en (see the Test #1 item). The laptop's p90 of 2.4 s
  leaves some headroom, but small.en is roughly 3× slower; benchmark it on this laptop with
  `huddle-helper bench --audio …\system.flac --models base.en,small.en` first.
  → Custom vocabulary is now a requirement: `docs/LIVE_VIEW.md` L33–L38 ("Custom
  vocabulary", milestone 9), with a spike on this meeting's kept audio first.
  → **Spike run (`tools/vocab_spike.py`, base.en, `hotwords`, results in `lab\vocab\`).**
  Re-transcribing this meeting's 253 utterances:

  | | No prompt | List (29 tokens) | "Glossary: … ." (33) | List + 4 names (37) |
  | --- | --- | --- | --- | --- |
  | Expected terms found | 47 | 74 | 66 | 77 |
  | Known mishears left (of 12) | 11 | 2 | 2 | 2 |
  | New lines where the plain run had none | – | 0 | 0 | 0 |
  | Lines with 4+ extra words on the end | – | 13 | 7 | not counted |
  | Transcription time | 164 s | 537 s | 302 s | 904 s |

  **What works:** "cover" → "KoVa", "ETO" → "ETL", "FMX" → "FMS", "bender" → "vendor",
  "co-trees" → "code freeze", "spray cheat" → "spreadsheet", "Zendrix" → "Zentrix".
  "Quodium" is not fixed. Tests #2 and #3 gained no new lines.
  **What doesn't (not yet releasable under L36):**
  - *Junk on the end of correct lines*, e.g. "…SOT", "Mm. Mm.", "Make sure to subscribe to
    our channel." Also seen in Test #3. The plan's checks (new lines, vocabulary-only
    lines) don't catch these; count them.
  - *Much slower:* about 2–3× the transcription time, slowest utterance 10–17 s instead of
    1–6 s. The times were taken while other jobs ran on the PC, so re-measure on an idle
    machine before trusting the exact figures. Likely cause: the junk lowers the score and
    triggers the temperature retries; not confirmed.
  - *Names are not safe:* with names in the prompt, "Well," became "Remote A," three times and
    two lines gained an invented name on the end ("and Remote D.", "Remote B."). Attendee
    names stay out of the prompt by default (L34's automatic add becomes opt-in).
  → **Follow-up (`tools/vocab_spike.py run|diag|table`, one job at a time on the PC).**
  Clean timing confirms the slowdown: no prompt 162 s (median 0.56 s, p90 0.68 s per
  utterance), plain list as `hotwords` 578 s (median 1.68 s, p90 5.16 s), 3.6× overall.
  **Cause:** the junk is not in the speech. With word timings on, faster-whisper moves on
  to the end of the last word and decodes one more window over the 0.2 s of padding left.
  Without a prompt that window comes back empty; with a prompt the model invents words
  there (echoes of the list, "Mm.", "SOT", "Thank you"), the low score triggers the
  temperature retries, and each retry is a full decode. 129 of 253 utterances retried with
  the list, 11 without; all 16 lines with 4+ extra words had the extras in a later window
  decoded at a temperature above 0.
  **Fix that works on Test #4** (variant `e5` plus trim `T`):

  | | No prompt | List, `hotwords` | Sentence + stop 1.0 + trim | Sentence + stop 0.5 + trim |
  | --- | --- | --- | --- | --- |
  | Expected terms found | 47 | 72 (65 without the echoed junk) | 67 | 67 |
  | Known mishears left (of 12) | 12 | 2 | 2 | 2 |
  | Lines with 4+ / 2+ extra words | – | 16 / 76 | 2 / 8 | 2 / 9 |
  | Lines lost | – | 1 | 0 | 0 |
  | Time, total / median / p90 | 162 / 0.56 / 0.68 s | 578 / 1.68 / 5.16 s | 151 / 0.55 / 0.65 s | 194 / 0.59 / 1.27 s |

  - *Sentence prompt:* "This call is about KoVa, QDM, …, zone codes and Zentrix." as
    `hotwords`. (`initial_prompt` is not the same: it is 30% faster than `hotwords` with
    the list but still 2.5× the plain run.)
  - *Stop:* stop reading Whisper's segments once one ends within N seconds of the end of
    the utterance, so the leftover window is never decoded. This is what gets the time back.
  - *Trim:* drop any segment after the first whose average log-probability is below −0.8,
    compression ratio above 2.4, or no-speech probability above 0.2. Real later segments in
    the plain run sit at −0.77 or better and 0.06 or less; junk sits at −0.9 or worse and
    0.2 or more.
  - Turning off the temperature retries alone does not help (2.1×, 24 junk lines).
  **Costs and open points:**
  - The stop rule can drop a real last word. Without a prompt, stop 1.0 shortened 8 of 253
    lines ("Nope.", "Yeah.", "Okay.", "and", "the names."). Only 0.2 s of padding follows
    the speech, so up to 0.8 s of real speech can sit inside a 1.0 s stop. Use the stop
    only when a vocabulary is set.
  - The prompt can break a word the plain run got right: on Test #3 "Huddle Helper" became
    "Of HIDL helper".
  - The 8 lines that still gain 2+ words (system 03:15, 05:37, 08:18, 08:24, 16:10, 20:06,
    20:29, 22:32) look like real corrections ("co-trees" → "a prod code freeze") but have
    not been checked by ear. Nor have the extra "KoVa" hits.
  - Retries are random, so figures can move between runs. A second run of stop 1.0 gave
    the same counts (158 s). Names in the prompt were not retried.
  **Chosen for the build: sentence prompt, stop 0.7, trim** (`e8+T`). On Test #4 it gives
  the same result as stop 1.0 (67 terms, 2 mishears left, 2 / 8 lines with extra words,
  none lost) in 164 s (median 0.57 s, p90 0.71 s), and leaves less room to cut a real
  last word. On Test #2 (41 utterances, none of these terms): no lines lost, one line
  gains 2 words, 26 s against 32 s for the plain run. With no vocabulary set, nothing
  changes.
  → **Built (WP3):** Settings has a Vocabulary tab and the Start dialog takes extra terms
  for the meeting; a change during a recording applies from the next line. The app's own
  transcriber, run over Test #4 with these terms, matches the spike: 67 terms, 2 mishears
  left, no lines lost, 2 / 8 lines with extra words, 157 s. Still to do: look at the two
  dialogs by eye, try a real meeting on the laptop, and listen to the 8 changed lines.
  *Not built, on purpose:* attendee names are opt-in only; L34's automatic add is not built.
  The vocabulary-only-line guard (plan step 7) is skipped: the spike found no new lines.

### Microphone

- [ ] **The Kiyo Pro webcam mic is quiet:** speech peak median 0.046 (warning limit 0.05).
  **Confirmed by Bryce:** the quiet-mic notification appeared, so the 079d8c9 warning works on
  the target laptop. Every
  spoken mic passage was transcribed. Short sounds with no line at 03:29, 06:47 and 10:35 come
  out of Whisper as "Okay." or "I'm so sorry.", most likely noises or backchannel. "Zendrix."
  is probably "Zentrix" (a data-management product) said at the end of the question, so it's
  not a hallucination.
  *Action:* raise the mic's input level in Windows Sound settings on that laptop.

## Still open from the PRD milestones

- [ ] M1: 30-minute capture spike on a real call (`capture-spike --minutes 30`).
- [ ] M6: 2-hour soak test (lag, memory, dropped lines).
- [ ] Switch the default audio device mid-call and confirm rebind and the `gaps` entry in `meta.json`.
- [ ] Echo dedupe on real speakers (no headset): remote speech must not appear as "You".

## From building the live view (WP4–WP7)

### Decisions waiting

- [ ] **A missing diarization model does not fail the final pass.** The pass logs the error
  and completes on the live labels (`refined_streams: []`), so the LIVE_VIEW milestone 3
  check (rename the model folder, see the error, Retry) cannot pass as written. Decide
  whether a missing model should be a visible error. Retry itself was tested with a
  different forced failure.
- [ ] **Hearing audio older than 10 minutes while recording** (WP7 spike, nothing built).
  A second read-only handle on the live `audio\*.flac` works on Windows while the writer
  holds it: opens in 5–8 ms, reads are sample-exact up to about 3 s behind the writer, no
  `flush()` needed, and a killed process loses nothing either way. The reader must take
  the length from the meeting clock (the file reports it as unknown) and catch
  `LibsndfileError` near the end. 5-minute chunk files also work but change the
  documented `audio\` layout and about 8 call sites. *Recommended:* the second handle,
  only for lines older than the in-memory 10 minutes; don't build chunk files.
- [ ] **Delete and the Recycle Bin:** a meeting larger than the bin's size limit, or a
  drive with the bin switched off, may be deleted permanently while the dialog says it can
  be restored. Not tested. Consider a size check before the delete.

### Clip edges (WP7)

- [x] **A fixed 300 ms of padding cut speech on 9 of 42 lines** (Tests #2 and #3): speech
  ran up to 660 ms past Whisper's last word. Clips now run on (or back) up to 600 ms to
  the first quiet point; 41 of 42 lines pass the numeric check (the other starts at 0 ms).
  *To listen to:* the ends of Test #2 lines 5, 8, 15, 21, 24, 28 and Test #3 lines 1, 3,
  7, 8, 11, which the VAD still flags at a low level.
- [x] **Replaying a line a second after it was said dropped the original line** (found in
  replay). The drop rule now compares the words' own span with the playback window.

### Needs a person

- [ ] A real call on the laptop with the window open throughout: p90 and max delay no
  worse than Test #4 (2.4 s and 6.8 s); watch the header at Stop (it once read a clipped
  "Refining speaker lab" for 18 s; not reproduced since a layout fix).
- [ ] Mute on a real call: the other side still hears you, and nothing of your voice is in
  `system.flac`. The AltGr hotkey warning on a German keyboard. The tray's Bookmark
  submenu and mute item.
- [ ] Click to hear: 20 clips in a real call with a headset and without; switch the
  default output device while a clip plays (capture must rebind and log the gap).
- [ ] All new windows and dialogs at 125–150% display scaling; a saved export in Notepad
  and a Markdown viewer; Show in Explorer.

## From naming speakers (WP8)

- [x] **Names given in the window steer the final pass** (`speaker_tags.jsonl`, `tags.py`).
  With no tags the final output is byte-identical to before on Tests #2, #3 and #4
  (31, 15 and 192 lines). Test #3 replay with two names given while recording: both final
  speakers named, the third and `You` untouched. Test #4 final pass on copies with two
  names: the five-speaker partition is unchanged. At a looser threshold (0.50 / 0.60),
  where plain clustering merges people, the named ones stay apart.
- [x] **A named small cluster may fold into a larger cluster with the same name**
  (departure from the plan's "never folded"): naming a whole speaker otherwise left its
  1 s lines as extra speakers with the same name. It is still never folded into an
  unnamed or differently named cluster.
- [ ] **Only named people are protected.** At threshold 0.60 on Test #4 a named speaker
  took in 74 s of an unnamed one. Not a problem at the shipped 0.70; a reason not to
  lower it.
- [ ] A line under 0.5 s has no fingerprint, so naming it cannot steer clustering (it
  still counts for the mapping). Consider saying so in the picker.
- [ ] After the swap to the final view the top row can sit partly hidden (seen in
  `lab\wp8\shots\10`). Likely WP4 scroll behaviour.
- [ ] *Needs a person:* name people during a real call; the "This line is…" submenu (it
  could not be photographed by script); the full 22-minute Test #4 replay with tags; the
  downstream reader for `speaker_tags.jsonl` and the new `speakers.json` fields
  (`from_tags`, `conflicts`).

## From the P1 polish branches (WP9)

### WP9a: attendees, number keys, "Who is this?" (L13)

- [x] Built: "Who's on the call" on the Start dialog (`meta.json` `attendees`, absent when
  empty), a numbered strip, keys 1–9, a confirmation with Undo, and a "Who is this?" bar
  for each new call voice. Checked in a windowed replay of Test #3.
- [x] **Attendee names are not given to Whisper unless a box is ticked** (off by default),
  following the WP3 decision. LIVE_VIEW L34 still says "added automatically" in two
  places; the new Attendees paragraph is the one that is right.
- [ ] **Decide:** the "Who is this?" bar also appears when no attendees were entered, so
  every meeting shows it for every new call voice. Keep, or show it only with attendees?
- [ ] **Decide:** while the bar is showing, a number key answers the voice the bar asks
  about (oldest unanswered), not the latest line. Otherwise it names the latest unnamed
  call voice.
- [ ] *Needs a person:* the keys on a real keyboard (numpad, non-US layout) during a real
  call; that the bar never takes focus from another app.

### WP9b: Play from here, clip levels, output device (L19)

- [x] Built: "Play from here" on a line's menu (`playsequence.py`), clip levelling to
  -20 dBFS with a +24 dB cap and a soft limiter (`leveling.py`), and "Play clips on" in
  Settings (`outputs.py`, `clip_output_device`). 72 clips from Tests #2 and #3: no peak
  over 1.0, 67 within ±1 dB of the target, 4–5 very quiet mic lines capped. A windowed
  Test #3 replay with four runs at gain 0 gave the same live (21) and final (13) lines as
  a replay without playback; `playbacks` 16, dropped 0.
- [ ] **Not proven on real loopback:** the replay feeds files, so no played sound comes
  back. That a run's clips are dropped (the joined 12 s window) rests on unit tests.
- [ ] **Decide:** a run still playing at Stop carries on from the kept files.
- [ ] With no attendees the "Who is this?" bar and the headphone warning stack in the
  same yellow and take three rows (`lab\wp9b\shots\02`); see the WP9a decision.
- [ ] The "↓ N new lines" button overlaps the last row's text (older behaviour).
- [ ] *Needs a person:* listen to levelled clips and to line changes in a run (nothing
  was played audibly); a real non-default "Play clips on" device and unplugging it;
  switching the default output during a run.

### WP9c: Start muted, mute reminders, user-defined bookmark types (L24, L28)

- [x] Built: "Start with my mic muted" on the Start dialog, a reminder every 10 minutes of
  continuous mute (`mute_reminder.py`), and up to four user-defined bookmark types in a
  new Settings tab (`bookmark_types.py`, `Settings.custom_bookmark_types`). Windowed
  Test #3 replay started muted: no mic line and 0 of 626,400 nonzero samples in
  `mic.flac` over the first 39 s (the source peaks at 0.20 there); reminders came every
  interval and stopped at unmute and at Stop.
- [x] **A removed or renamed type keeps its name in old meetings:** `marks.jsonl` holds
  the id, `meta.json` `mark_kinds` holds the name as soon as the type is used.
- [ ] A new type's hotkey applies from the next recording (its name applies at once).
  The Bookmark types tab shows clashes and the AltGr warning, not whether Windows
  accepted the registration (the Hotkeys tab does).
- [ ] *Needs a person:* the real Windows notification for the reminder; the tray Bookmark
  submenu with a custom type (built on each open; only its construction was checked);
  whether the stronger amber bar for 60 s is wanted.
