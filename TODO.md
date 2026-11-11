# TODO

Findings from real-world tests, kept as the record of what was seen and measured.

Open items now live in GitHub Issues: each one below is marked with the issue that tracks it.
File new findings as issues (`gh issue create --label needs-triage`), not here.

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
  → Investigated in Test #2 (see "Live labels: threshold is too low" below). Fixed in 5599735.
- [x] **Final pass splits sentences mid-way.** The refined labels split a sentence between
  speakers: "…part of 12.x I think so I did" (`SPEAKER_03`) / "the same with that as well."
  (`SPEAKER_02`).
  *Fix:* only split at a pause or sentence boundary, and don't split off pieces shorter than
  about 1 s / 3 words. Code: `finalize.py` `_split_by_words`. Fixed in 5599735.
- [x] **Final pass flips speaker word by word.** "Who could look at first? I found it. I
  found it. So this is the one…" came out as 4 segments alternating `SPEAKER_01` /
  `SPEAKER_03`. Same fix as above: fold short runs into the neighbouring speaker.
  Fixed in 5599735 (this exact case is a unit test).
- [ ] (#13) **Check whether 4 speakers is right** for this clip (it was 1 in live, 4 in final).
  Needs a recording with known speakers and Keep audio on. `final_cluster_threshold` is 0.6.
  Small look-alike speakers are now merged (5599735), which may change the count. The best
  check is a recording where you know how many people speak.

### Microphone

- [x] **Only one mic line was captured:** "This is a test. Yes." (confidence 0.39).
  *Investigate:* confirm how much was said into the mic. If more, check the mic input level
  and the VAD threshold (`vad_threshold` 0.5) and minimum-speech rule on real mic audio.
  → Test #2 showed the mic records very quietly (see "Mic level is very low" below). That is
  the likely cause here too. Quiet audio is now boosted before transcription, and the tray
  warns when the mic is too quiet (5599735).
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
  Fixed in 5599735: `meta.json` → `live_stats` has `line_delay_ms` (median, p90, max),
  `max_backlog`, `max_lag_s` and `speech_peak_median` per stream.

### Transcription quality

- [ ] (#32) **base.en mishears names and jargon**, e.g. "Gitli Cluster" (probably "GitLab").
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

- [x] **Raise `live_speaker_threshold` from 0.6 to about 0.72–0.75.** Fixed in 5599735: 0.75,
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
  Fixed in 5599735; on the Test #2 replay, all four of these now stay whole.
- [x] **Tiny spurious speakers.** The offline pass found 5 speakers, but 3 of them have only
  2 s, 9 s and 10 s of speech in total; they show up as `SPEAKER_03`–`SPEAKER_05` on a few
  fragments. *Fix:* merge clusters with under about 10 s total (or under ~5% of speech) into the
  nearest larger speaker, or raise `final_cluster_threshold`. Code: `finalize.py`.
  Fixed in 5599735: a speaker with under max(15 s, 5% of speech) is merged into the most
  similar larger speaker if they're at least 0.7 alike. Raising the threshold instead would
  have merged the two synthetic test voices (0.50 alike). The Test #2 replay now has 2 speakers.
- [ ] (#13) **Final pass also relabels lines the live pass got right**, e.g. line 1 is split into
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
  Fixed in 5599735: utterances are boosted toward a 0.5 peak (max 30×), and the tray warns
  once per meeting when mic speech peaks below 0.05. Raising the Windows input level is
  still the best fix.
- [x] **Whisper hallucinated a repetition loop on the quiet mic.** Live line 26: "Good to see
  how does it handle overlapping voices? Alright, let's go. Alright, let's go check this
  one." ×5, squeezed into 2.5 s. Re-transcribing the same kept audio offline gives the clean
  "…to see how does it handle overlapping voices?". *Fix:* level normalization (above),
  plus a repeated-phrase filter, and try faster-whisper's `no_repeat_ngram_size`,
  `repetition_penalty` and `hallucination_silence_threshold`. Code: `transcribe.py`.
  Fixed in 5599735: the looped words were 0–20 ms long, so sentences averaging under 60 ms
  per word are dropped, and a third identical sentence in a row is dropped. Replaying
  Test #2 gives "Good to see how does it handle overlapping voices?". The faster-whisper
  options weren't needed.
- [x] **Mic timing.** The two mic lines here had sensible timings (2.1 s and 2.5 s). The
  squeezed timing from Test #1 may also be a hallucination artifact; re-check after the
  fixes above.

### Housekeeping

- [x] (dropped in #21: no folder or other record to investigate) A 21-second false start ("Test #2", 13:34:09–13:34:30, 4 lines) appears in the log, but its
  folder is gone. If it was deleted by hand, nothing to do. Otherwise, find out why.

## From Test #3: `2026-11-09_1441_test-3`

Setup: rebuilt exe with the 5599735 fixes, Keep audio on, same headset. Recorded meeting
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
  (line 10) in its own cluster. The 5599735 merge then folded that 6 s cluster into the B+C
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
  changes mid-line, which the 5599735 phrase rules already limit. Then re-run all three
  recordings and the TTS e2e test. Code: `finalize.py`, `diarize.py`.
  **Fixed in 416de3a** as proposed (`final_voice_threshold` 0.65). Re-running the final pass
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
  Done in 416de3a. The merge now only affects where lines may split, not labels.

### Live labels

- [x] **Short opening lines create extra live speakers.** Lines 3 (1.0 s) and 4 (1.6 s) became
  `SPEAKER_01` and `SPEAKER_02`, then the first long line of the same voice became
  `SPEAKER_03`. Fingerprints of lines this short are unreliable (0.57–0.70 similar to their
  own speaker's long lines). The rest of the live labels were right: A, B and C each got one
  label. *Ideas:* don't let a line under ~2 s found a new speaker unless no speaker exists yet,
  and let a provisional speaker built from short lines be absorbed by the next confident
  match. Live labels are provisional, so this is lower priority than the final pass.
  Partly fixed in 416de3a: both ideas are in (new speaker needs 2.0 s; a speaker known from
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
  Fixed in 416de3a, with a 2 s gap limit (the first example's pause was 1.6 s). On the replay,
  the final file reads "Alright, this is to kick off and initiate test number three of Huddle
  Helper." and "Alright, we're going to close out the Test number three." The same rule
  also joins remote lines cut mid-sentence, e.g. "and that feels about average, but we've had"
  / "how many speakers…".

## From Test #4: `2026-11-09_1605_work-recording-2` (target user's laptop)

Setup: a real work call recorded on the target user's laptop. Mic: Razer Kiyo Pro (webcam
mic); system: Realtek headphones; base.en; Keep audio on. 22 min 41 s; 252 live lines,
203 final lines. The laptop's user spoke only 3–4 times; the rest is remote speech. From names
used on the call, there were at least 5 remote people, called Remote A–E here. A copy of the
folder is in `Downloads\testmeeting` (its real name differs: `2026-11-09_1605_*`).

Privacy: names, the company and the team's domain terms from this call are kept out of the
repo. Call speech is paraphrased, and the domain terms are made-up stand-ins (KoVa, QDM,
FMS, Zentrix, zone code); their real mishears are described, not quoted.

What went well:
- **No gaps, errors or crashes**, and a clean exit. Echo dedupe correctly dropped nothing
  (headphones).
- **Live speed held up on the laptop over 22 minutes:** line delay median 1.3 s, **p90
  2.4 s** (PRD target 5 s), max 6.8 s; backlog never above 2; max lag 6.6 s.
- **Remote C is labelled correctly throughout:** every line of theirs has `SPEAKER_02` in the final
  file, and nobody else's does.
- The mic question split over two live lines is joined in the final file (416de3a working).

### Final pass: a real speaker was folded away (high priority)

- [x] **Remote D is merged into Remote B (`SPEAKER_03`).** Final result: 3 speakers —
  `SPEAKER_01` 591 s (Remote A plus others), `SPEAKER_02` 74 s (Remote C), `SPEAKER_03` 377 s
  (Remote B + Remote D). Remote D's lines form a clean, separate voice cluster: 6 lines, 28 s,
  lines 102 (an offer to send something, then thanks to Remote D by name), 119, 127, 167,
  196 and 198. They're only 0.64–0.75 similar to Remote B's centroid, the
  same range as clearly different speakers in Test #2. Two things merge them, and both need
  fixing:
  1. **The threshold.** At `final_voice_threshold` 0.65, average linkage already merges
     Remote D's lines into Remote B's cluster. From 0.70 to 0.80 they're a separate cluster every
     time.
  2. **The fold has no similarity check.** Even at 0.70, Remote D's 28 s is under the fold
     limit (3% of 1035 s of speech = 31 s). `fold_small_clusters` would then give it to the
     most similar large cluster however unlike it is. A second small cluster (lines 170, 172
     and 176, 13 s, a promise that two named colleagues will keep the others updated) is folded the same way. The
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
- [ ] (#13) **`SPEAKER_01` may still be several people.** Remote A, the person who opens the call (setting out
  the business need) and the lines that sound like Remote E (inviting Remote D to correct them;
  asking Remote A and B whether a date works) are all
  0.82–0.92 alike, as similar as Remote A is to themself, and they stay together even at 0.8. So
  either they're the same person, or this embedding model can't tell these voices apart over call
  audio. *Needs:* someone who was on the call to say who spoke lines 1–17, 117 and 121.

### Final pass: fragments and mixed lines

- [x] **A sentence end cut off onto another speaker.** A question ending "…a
  dump of the" (`SPEAKER_03`) / "GCP table." (`SPEAKER_01`), and two more pairs where a short
  closing piece (2–6 words) took a different label from the line it finishes (`01` / `02`,
  `02` / `01`). The short second piece is too short to
  fingerprint reliably, so it takes a neighbour's label, which can be the wrong neighbour.
  `join_fragments` only joins the same speaker. *Idea:* when the previous line has no closing
  punctuation, the gap is under ~1 s and the next line is very short (≤ 3 words), attach it
  to the previous line whatever its label.
  Fixed in WP1 as proposed ("…dump of the" / "GCP table." is now one line). The other two
  examples aren't covered: their first line ends with a full stop or "…". A line that
  trails off with "…" doesn't count, because the short line after it is usually someone
  else's reply.
- [x] **Same-speaker fragments not joined:** "…build out the…" / a short
  line naming two domain terms (both misheard). Whisper's trailing "…" counts as closing punctuation.
  *Idea:* don't treat "…" as a sentence end in `join_fragments`. (Also "default to the" / "The
  zone code address" was not joined because its gap is 2.6 s; leave that as is.)
  Fixed in WP1: 14 such pairs are joined in Test #4 (203 lines → 190), 2 in Test #2, 1 in
  Test #3. The "…" stays in the joined text.
- [x] (#24, closed: fixed by the WP13 model or by design) **Quick back-and-forth stays inside one line** (low priority, by design): a question
  answered "Yes. Yes." inside the same line, and two more like it Interjections under
  1 s / 3 words are folded into the surrounding line on purpose (Test #1 fix), so a short reply
  from someone else is attributed to the main speaker. Revisit only if the downstream app needs it.

### Speed of the final pass on the laptop

- [ ] (#14) **The final transcript took 13 min 34 s after Stop** for a 22.7-minute meeting
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

- [ ] (#32) **Domain jargon is misheard throughout** (base.en): KoVa (an internal acronym) → three
  different everyday phrases, QDM → two made-up words, ETL → "ETO", FMS → a similar acronym
  or an everyday word, vendor → "bender", code freeze → "co-trees", prod → "abroad",
  spreadsheet → "spray cheat", zone codes → an everyday word. The meaning of whole sentences is lost for a downstream summary.
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

  **What works:** KoVa's mishear → "KoVa", "ETO" → "ETL", FMS's mishear → "FMS",
  "bender" → "vendor", "co-trees" → "code freeze", "spray cheat" → "spreadsheet", Zentrix's
  mishear → "Zentrix". QDM's mishear is not fixed. Tests #2 and #3 gained no new lines.
  **What doesn't (not yet releasable under L36):**
  - *Junk on the end of correct lines*, e.g. "…SOT", "Mm. Mm.", "Make sure to subscribe to
    our channel." Also seen in Test #3. The plan's checks (new lines, vocabulary-only
    lines) don't catch these; count them.
  - *Much slower:* about 2–3× the transcription time, slowest utterance 10–17 s instead of
    1–6 s. The times were taken while other jobs ran on the PC, so re-measure on an idle
    machine before trusting the exact figures. Likely cause: the junk lowers the score and
    triggers the temperature retries; not confirmed.
  - *Names are not safe:* with names in the prompt, "Well," became Remote A's name three times
    and two lines gained an invented attendee name on the end. Attendee
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

- [ ] (#15) **The Kiyo Pro webcam mic is quiet:** speech peak median 0.046 (warning limit 0.05).
  **Confirmed by Bryce:** the quiet-mic notification appeared, so the 5599735 warning works on
  the target laptop. Every
  spoken mic passage was transcribed. Short sounds with no line at 03:29, 06:47 and 10:35 come
  out of Whisper as "Okay." or "I'm so sorry.", most likely noises or backchannel. One short
  line is probably Zentrix (a data-management product) said at the end of the question, so it's
  not a hallucination.
  *Action:* raise the mic's input level in Windows Sound settings on that laptop.

## Still open from the PRD milestones

- [ ] (#6) M1: 30-minute capture spike on a real call (`capture-spike --minutes 30`).
- [ ] (#7) M6: 2-hour soak test (lag, memory, dropped lines).
- [ ] (#6) Switch the default audio device mid-call and confirm rebind and the `gaps` entry in `meta.json`.
- [ ] (#6) Echo dedupe on real speakers (no headset): remote speech must not appear as "You".

## From building the live view (WP4–WP7)

### Decisions waiting

- [ ] (#16) **A missing diarization model does not fail the final pass.** The pass logs the error
  and completes on the live labels (`refined_streams: []`), so the LIVE_VIEW milestone 3
  check (rename the model folder, see the error, Retry) cannot pass as written. Decide
  whether a missing model should be a visible error. Retry itself was tested with a
  different forced failure.
- [x] (#17, won't build: .out-of-scope/hear-old-audio-while-recording.md) **Hearing audio older than 10 minutes while recording** (WP7 spike, nothing built).
  A second read-only handle on the live `audio\*.flac` works on Windows while the writer
  holds it: opens in 5–8 ms, reads are sample-exact up to about 3 s behind the writer, no
  `flush()` needed, and a killed process loses nothing either way. The reader must take
  the length from the meeting clock (the file reports it as unknown) and catch
  `LibsndfileError` near the end. 5-minute chunk files also work but change the
  documented `audio\` layout and about 8 call sites. *Recommended:* the second handle,
  only for lines older than the in-memory 10 minutes; don't build chunk files.
- [ ] (#22) **Delete and the Recycle Bin:** a meeting larger than the bin's size limit, or a
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

- [ ] (#8) A real call on the laptop with the window open throughout: p90 and max delay no
  worse than Test #4 (2.4 s and 6.8 s); watch the header at Stop (it once read a clipped
  "Refining speaker lab" for 18 s; not reproduced since a layout fix).
- [ ] (#8) Mute on a real call: the other side still hears you, and nothing of your voice is in
  `system.flac`. The AltGr hotkey warning on a German keyboard. The tray's Bookmark
  submenu and mute item.
- [ ] (#8) Click to hear: 20 clips in a real call with a headset and without; switch the
  default output device while a clip plays (capture must rebind and log the gap).
- [ ] (#8) All new windows and dialogs at 125–150% display scaling; a saved export in Notepad
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
- [ ] (#25) **Only named people are protected.** At threshold 0.60 on Test #4 a named speaker
  took in 74 s of an unnamed one. Not a problem at the shipped 0.70; a reason not to
  lower it.
- [x] (#26, closed: the picker says so) A line under 0.5 s has no fingerprint, so naming it
  cannot steer clustering (it still counts for the mapping).
- [ ] (#28) After the swap to the final view the top row can sit partly hidden (seen in
  `lab\wp8\shots\10`). Likely WP4 scroll behaviour.
- [ ] (#9) *Needs a person:* name people during a real call; the "This line is…" submenu (it
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
- [x] (#18) **Decided:** the "Who is this?" bar appears only when attendees were entered;
  without them the unnamed voice's chip asks instead (`WhoPrompter.update` takes the list).
- [x] (#18) **Decided, kept:** while the bar is showing, a number key answers the voice the bar
  asks about (oldest unanswered), not the latest line. Otherwise it names the latest unnamed
  call voice.
- [ ] (#9) *Needs a person:* the keys on a real keyboard (numpad, non-US layout) during a real
  call; that the bar never takes focus from another app.

### WP9b: Play from here, clip levels, output device (L19)

- [x] Built: "Play from here" on a line's menu (`playsequence.py`), clip levelling to
  -20 dBFS with a +24 dB cap and a soft limiter (`leveling.py`), and "Play clips on" in
  Settings (`outputs.py`, `clip_output_device`). 72 clips from Tests #2 and #3: no peak
  over 1.0, 67 within ±1 dB of the target, 4–5 very quiet mic lines capped. A windowed
  Test #3 replay with four runs at gain 0 gave the same live (21) and final (13) lines as
  a replay without playback; `playbacks` 16, dropped 0.
- [ ] (#10) **Not proven on real loopback:** the replay feeds files, so no played sound comes
  back. That a run's clips are dropped (the joined 12 s window) rests on unit tests.
- [x] (#18) **Decided, kept:** a run still playing at Stop carries on from the kept files. If
  they go mid-run (Keep audio off, a delete), the clips it cannot read are skipped and it ends
  quietly as finished (tested).
- [x] (#18) With no attendees the "Who is this?" bar and the headphone warning stacked in the
  same yellow and took three rows (`lab\wp9b\shots\02`). Gone with the WP9a decision; with
  attendees the bar, strip and warning can still stack, which is accepted.
- [ ] (#27) The "↓ N new lines" button overlaps the last row's text (older behaviour).
- [ ] (#10) *Needs a person:* listen to levelled clips and to line changes in a run (nothing
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
- [ ] (#30) A new type's hotkey applies from the next recording (its name applies at once).
  The Bookmark types tab shows clashes and the AltGr warning, not whether Windows
  accepted the registration (the Hotkeys tab does).
- [ ] (#11) *Needs a person:* the real Windows notification for the reminder; the tray Bookmark
  submenu with a custom type (built on each open; only its construction was checked);
  whether the stronger amber bar for 60 s is wanted.

### WP9d: Storage panel, vocabulary from a line and from a file, short speakers (L32, L37, L38, L40)

- [x] Built: "Storage…" in the Meetings view (`storage.py`), "Add to vocabulary…" on a
  line, "Add from file… / Replace from file… / Export to file…" on the Vocabulary tab,
  and "· short" on final speakers under 30 s of speech. On a scratch folder of 7
  meetings the panel's sizes matched an independent sum to the byte, and the delete
  removed exactly the 4 previewed `audio\` folders (12 files) and nothing else.
- [x] **`You` is never marked short** (changed in review: Test #4's `You`, 8.8 s, was).
  With that, no speaker of the Test #4 copy is marked.
- [x] (#18) **Decided, kept:** short is under 30 s of speech only (the line count is not used),
  in the final view only, and when every speaker is under 30 s nobody is marked.
- [ ] (#29) Adding a term from a line while the Settings dialog is open: the dialog's Save
  overwrites it. The Storage panel does not follow a meetings-folder change made while
  it is open.
- [ ] (#12) *Needs a person:* the Storage delete against the real Recycle Bin on a throwaway
  meeting (every check used a fake bin or a permanent delete on scratch copies); the
  file dialogs for import and export; adding a term during a real recording.

## From same name, same speaker (WP11, L39)

- [x] **Lines the user gave the same name start as one cluster** (`cluster_voices`). No
  names: Tests #2, #3 and #4 are byte-identical to before. The WP8 runs (Test #3 with two
  names, Test #4 with two names) give the same speakers and names. On Test #4 the same
  name on a line of the 582 s speaker and one of the 12.5 s speaker merges the two
  (594 s, four remote speakers). Clustering time is unchanged (about 4.5 ms).
- [x] **A name names one final speaker per stream** (`map_final_speakers`, added in
  review). Before, a speaker-level name on a live label that mixed two voices named both
  final speakers: 4 s of left-out pieces named a 570 s speaker. Re-mapping the seven
  WP11 runs changes only that case.
- [ ] (#25) **The rule obeys the user even when wrong:** the same name on lines of two
  different people moves those lines (7–11 s on Test #4) into one speaker; the rest of
  the other person stays apart. Nothing warns about it.
- [ ] (#25) **A speaker-level name cannot split voices 0.7 or more alike:** naming a live label
  that mixed Test #4's 74 s and 38 s speakers merged them (118 s). A line-level name on
  one of them is the way out.

## From the process-loopback spike (WP10)

Nothing in the product changed. Finding and numbers: `docs/WP10_SPIKE.md`; tool:
`tools/process_loopback_spike.py`.

- [x] **Process loopback works through comtypes** (Windows build 26300, no native code).
  A tone this process played was at -34 dBFS in device loopback and exact zero in process
  loopback; tones from other processes were at -34 in both. A 20-minute capture gave
  1200.14 s of audio for 1200.13 s of wall clock with no flags or timeouts.
- [x] (#19, won't build: .out-of-scope/process-loopback-capture.md) **Decide: build it** (recommended, with device loopback kept as the fallback when
  activation fails). About 300–400 lines: a new `audio\procloop.py`, edits in
  `capture.py`, a `meta.json` field for the capture method.
- [ ] (#19) **Tree exclusion reached only direct children on this build:** a grandchild's tone
  was captured. A helper process that plays sound would have to be a direct child.
- [ ] (#19) The client delivers zeros every 10 ms during silence (device loopback delivers
  nothing), which would simplify the meeting clock. The soak ran in silence only. One
  23 ms step between packets in a 14 s run is unexplained.
- [ ] (#19) *Needs a person before building:* a 30-minute capture during a real call with a
  clip played; switching the default output mid-capture (expected to carry on without a
  re-open; untested); a Bluetooth headset; the packaged exe; a Windows build below
  20348 for the fallback; the engine's 16 kHz conversion against soxr.

## From Test #5: `2026-11-10_1448_test-meeting-4` (first run of the packaged app with the live view)

Titled "Test Meeting 4" by the user; called Test #5 here because Test #4 is the laptop
recording. 125 s, headset, a recording played as the call. Copy in `lab\src\`.

### Worked

- [x] Clean Stop, `DONE`, no gaps. Live delay median 0.9 s, p90 1.3 s, max 2.4 s (33
  lines). Final pass 10.3 s for 124 s of audio (0.08×).
- [x] Attendee, a meeting vocabulary term (it appears, correctly cased, in one line), a
  labelled bookmark, one clip played while recording (no line dropped, none added), and
  Keep audio all left the expected files and `meta.json` fields.
- [x] Naming: two live labels were given the same name; the final pass put all their lines
  in one final speaker and named it (`speakers.json`, `from_tags: true`, no conflicts).

### Open

- [ ] (#21) **The app wrote no log for this run.** `%LOCALAPPDATA%\HuddleHelper\logs\huddle-helper.log`
  ends at 03:19 that day; the app was started from Explorer at 14:45 with no arguments
  and was still running at the time of the check. A bare start of the same exe with
  `HUDDLE_HELPER_DATA_DIR` set did log. Cause not found.
- [ ] (#23) **The main remote voice is split in two in the final file** (45 s and 18 s), and the
  two alternate inside sentences, which reads like one person. The live pass had it as one
  label (56 s). Without names the clustering gives the same split (51 s / 19 s at 0.70);
  the two halves only join at 0.60. Bryce: two people speak in the played recording, so
  three remote speakers is one too many. The smaller half is mostly pieces under 1.5 s
  plus two of about 4 s; the long pieces (3 s and up, 46 s to the end) agree with each
  other at 0.74 to 0.88. So the split is short pieces clustering apart from long ones,
  not two people.
- [x] (#20, kept as is: fix with a line-level name) **A speaker tag carried a later live mistake into the final file.** The only piece
  that is clearly another voice is the 6.5 s line at 71 s (0.51 to 0.58 against the long
  pieces). The live pass gave it the label the user had named at 33 s, so the tag covered
  it, and the same-name rule (L39) then forced it into the named speaker. *Decision:*
  should a speaker tag bind only the lines that existed when it was made, and be a soft
  hint for later ones? Bryce: the named person is the main voice. So the final file
  credits him with 13 s (the opening lines plus the other person's 71 s line) and leaves
  about 60 s of his speech unnamed across two speakers.
- [x] (#24, closed: fixed by the WP13 model or by design) **The live pass made four remote labels in the first 40 s**, two of them from lines
  under 1 s ("Uh...", one word). The user named two of them the same person.
- [x] (#24, closed: fixed by the WP13 model or by design) Short fillers ("Um.", "Like...", "So...") are still lines of their own, live and
  final, and are the lines whose speaker flips.
- [x] (#24, closed: fixed by the WP13 model or by design) Two junk system lines at 24 s (confidence 0.30 and 0.50, under 0.5 s each) as the
  played recording started. One mic line misheard "four" as "for".

## From the quality experiments (2026-11-10, nothing in the product changed)

Scored with `tools/final_lab.py score` (new): seconds of confirmed speech given to the wrong
speaker, from `tests/data/truth/` (Tests #3, #4 and the new `test-5.json`; 542 s). Test #5's
truth is partly inferred: the 71 s line as the second person rests on voice evidence.

### Speakers: the fingerprint model is the limit, not the clustering

- [x] **Per-meeting normalisation: rejected.** Subtracting the meeting's mean fingerprint (plain,
  long pieces only, or one vote per first-pass speaker) ranks pairs better inside a meeting
  (AUC 0.95 → 0.99 on Test #4) but the best cutoff then differs by 0.3–0.5 between meetings;
  no value works on all four. The plain mean also wipes out a dominant speaker (Test #5).
- [x] **Long pieces first: small gain, not robust.** With 4 s anchors and every shorter piece
  given to the nearest speaker: 0 s wrong at 0.70–0.72 (today: 1.4 s at 0.70) and the working
  range goes from 0.70–0.71 to 0.70–0.73. With 3 s anchors Test #5 still splits 8.1 s. A
  bonus for the neighbour in time made it worse. Not built.
- [x] **Centroid linkage and joining neighbours into turns: rejected** (no setting passes all four).
- [x] (done in WP13) **Next: compare fingerprint models** on the kept audio with the same score. The lower
  bound of every working range is Test #4's Remote D/Remote B pair (must be 0.70 or more to split)
  and the upper is one voice's own 3–4 s pieces (0.73), so only a model that separates
  those can widen it. Pieces of 3 s or more already pool at 5% error against 11% for all.

### Transcript: re-transcribing after Stop with small.en works

Each live line's audio span (200 ms before, 300 ms after) re-transcribed on the dev PC;
`lab\retx\`. The base.en control on the same spans is the first row.

| Test #4 (252 lines) | Time | × speech | Expected terms | Known mishears |
| --- | --- | --- | --- | --- |
| base.en, beam 1 | 163 s | 0.16 | 49 | 8 |
| small.en, beam 5 | 414 s | 0.41 | 53 | 2 |
| small.en, beam 5, vocabulary | 428 s | 0.43 | 65 | 0 |

- No line came back empty on any of the four recordings; about half the lines change.
  Seen fixes: "binder" → "vendor", "a couple of engines" → "a couple of decisions",
  "error if I" → "a verify". Seen losses: "Huddle Helper" → "Lidle Helper", one invented
  "See ya." on a line end. No reference transcript exists, so there is no word error rate.
- [x] (built in WP15; acceptance in #32) Not built. Open: time on the laptop (about 3× the dev PC), whether to keep the live
  text when the new line scores lower, and larger models (need a download).

### Speakers: other fingerprint models (2026-11-10, nothing in the product changed)

Nine models from the sherpa-onnx "speaker-recongition-models" release (checksums verified,
in `lab\models\speaker`), each fingerprinting the same pieces of the four kept recordings
(`tools/speaker_models_embed.py`, then `tools/speaker_models_score.py`). Pair error is the
share of confirmed same/different pairs on the wrong side of the best single cutoff; range
is the cutoffs at which Tests #3–#5 have 2 s or less with the wrong speaker.

| Model | Size | Pair error | Wrong seconds at best | Working range |
| --- | --- | --- | --- | --- |
| wespeaker ResNet34-LM (shipped) | 26 MB | 11.4% | 1.4 | 0.70–0.71 |
| 3D-Speaker ERes2Net, English | 26 MB | 0.2% | 0 | 0.26–0.60 |
| 3D-Speaker CAM++, zh+en "advanced" | 28 MB | 0.2% | 0 | 0.35–0.62 |
| NeMo TitaNet small | 40 MB | 0.5% | 0 | 0.25–0.58 |
| NeMo TitaNet large | 101 MB | 0.3% | 0 | 0.24–0.57 |
| wespeaker ResNet152 / 221 / 293-LM | 79–114 MB | 13–31% | 8–113 | none |
| wespeaker CAM++-LM, 3D-Speaker CAM++ English | 29 MB | 37–47% | 176–180 | none |

- The four good models give the same speakers as each other on every recording (99.7–100% of
  pairs of pieces of 2 s or more, unlabelled pieces included) and the same result across a
  0.2-wide span of cutoffs. They agree with the shipped model on Test #3 only.
- **Test #5:** 64 s + 7 s, which is what Bryce described (one main voice plus the 71 s line).
  The shipped model gives 51 s + 19 s.
- **Test #4:** six remote speakers (326, 320, 177, 103, 41, 12 s) against five; the shipped
  model's 534 s speaker becomes 320 + 177 + part of another. That is the "may be several
  people" item above. *Needs someone who was on the call.*
- **Test #2:** four speakers (84, 42, 41, 24 s) against two. Nobody has confirmed how many
  people speak in that clip. *Needs Bryce.*
- Speed at 4 threads on Test #4's 980 s of speech: 6 s (TitaNet small, CAM++), 13–14 s
  (ERes2Net, TitaNet large); the shipped model takes about 17 s.
- [x] (done in WP13) **Not built.** A swap means re-fitting every cutoff on the new scale (live 0.75 and
  0.6, final 0.70, both folds, the offline diarizer's 0.6), a settings migration, the model
  download and the bundle, and a live replay. Check each model's licence first. Why the
  larger wespeaker models and both English CAM++ models do so badly here was not looked into.

### What public benchmarks say (2026-11-10, read before more testing)

- **Fingerprint models: public rankings do not match ours.** VoxCeleb1-O error: wespeaker
  ResNet293-LM 0.45%, ResNet221-LM 0.51%, CAM++ 0.65%, TitaNet-L 0.66–0.68%, ResNet34-LM 0.72%,
  ERes2Net-base 0.84%, TitaNet-S 1.15%. On our pieces the order is reversed. wespeaker's own
  page says its LM models "perform better on long audios, e.g. >3s"; the plain ResNet34
  (same size, same release) is untested. TitaNet is the only candidate with published
  diarization results on meetings and telephone audio (AMI 1.7–2.2% and CH109 1.1–1.2%
  with given speech regions; small about equal to large). sherpa-onnx's own diarization
  examples use TitaNet small and 3D-Speaker ERes2Net, not wespeaker.
- [ ] (#3) Unexplained: the large wespeaker models and both English CAM++ models score far below
  what is published, here. Rule out a problem in how they are run before trusting the
  shipped model's numbers (it is the same family).
- **Whole diarization systems: little to gain for this app.** pyannote community-1 beats
  3.1 by 2–3 points on meetings (AMI 18.8 → 17.0 and 22.7 → 19.9) through better speaker
  assignment and counting; it needs PyTorch and is not in sherpa-onnx. DiariZen is the best
  open system (English 7.0 against 11.5 for pyannote 3.1) but is a large GPU-measured model.
  Sortformer falls to 22–24% with five or more speakers. Reverb's segmentation model in
  sherpa-onnx is under a non-commercial licence.
- **Speech-to-text: Parakeet is the larger step.** Meeting audio (AMI) word error: base.en
  21.1%, small.en 17.9%, Parakeet TDT 0.6B v2 11.2% (12.0% through a telephone codec).
  Average over eight sets: Parakeet v2 6.05%, distil-large-v3.5 7.21%, large-v3 7.44%,
  large-v3-turbo 7.83%. A sherpa-onnx int8 build exists (CC-BY-4.0). Not known: its speed on
  our CPUs, and whether a vocabulary can be given to it.

## From the fingerprint model swap (WP12 tools, WP13)

- [x] **The app now ships 3D-Speaker ERes2Net (English)** in place of wespeaker ResNet34-LM
  (same 26 MB, Apache-2.0, checksum checked on download). Chosen over TitaNet small: equal on
  final and live labels, better on pieces under 2 s (pair ranking 0.999 against 0.993 on
  Test #4), and no licence attribution to carry. TitaNet small is 3x faster per line (33 ms
  against 99 ms at 1 thread) if live speed ever matters; ERes2Net costs the same as the old model.
- [x] **Cutoffs moved into one profile per model** (`diarize.PROFILES`). The three numbers in
  Settings now default to "the profile's value"; settings version 5 drops numbers saved for the
  old model. Values: live match 0.42, live adopt 0.30, final match / fold / tag gate 0.40,
  split clustering 0.9.

  | Check (real final pass on copies, `lab\wp13`) | Old model | New model |
  | --- | --- | --- |
  | Confirmed speech with the wrong speaker, Tests #3-#5 | 1.4 s at 0.70, passes 0.70-0.71 | 0 s from 0.27 to 0.47 |
  | Same speakers on every recording | - | from 0.34 to 0.47 |
  | Test #5 remote speakers | 51 s + 19 s (one voice split) | 64 s + 5 s without names |
  | Test #4 remote speakers | 5 (534 s, 325, 72, 37, 12) | 6 (351, 341, 188, 114, 45, 13) |
  | Test #2 remote speakers | 2 (107, 84) | 4 (91, 42, 41, 26) |
  | Test #3, TTS sample (`pytest -m models`) | 3, 2 | 3, 2 |
  | Live replay: confirmed speech with the wrong live label (`tools/live_lab.py`) | 84 s | 8 s (match 0.38-0.46) |
  | Live replay: live speakers / final speakers, Tests #2-#5 | 2/2, 3/3, 7/5, 5/2 | 4/4, 3/3, 7/6, 2/2 |
  | Final pass, Test #4, 4 threads, PC busy with other jobs | 197 s when idle | 250 s |

- [ ] (#1, #23) **The range is 0.13 wide where the plan asked for 0.15**, and its upper end is set by an
  old patch, not by the model: from 0.48 the two halves of Test #5's 5 s speaker stop joining,
  and `fold_small_clusters` then gives each half away because a cluster under 4 s is always
  folded. That rule was written for the old model's unreliable short pieces. *Next:* fold a
  cluster under 4 s only when it is alike enough, and measure; nothing was removed in WP13.
- [x] (#20, kept as is: fix with a line-level name) **Test #5 with its names still reads 13 s + 64 s**: the named speaker holds the opening
  lines plus the other person's 71 s line, exactly the open "speaker tag carried a later live
  mistake" item above. The model swap does not touch that rule.
- [ ] (#31) **Split points:** pieces per recording are within 10% of before at any split setting from
  0.6 to 0.9 (Test #2 44 → 43, Test #3 15 → 16, Test #5 31 → 32, Test #4 190 → 184 final
  lines). Line boundaries that moved were not read one by one.
- [ ] (#14, #31, #3) **Not done:** the final-pass time was not measured on an idle PC or on the laptop; the
  WP8/WP11 runs with names on Tests #3 and #4 were not repeated; the wespeaker check (why the
  larger wespeaker models do badly here) was started and stopped by a usage limit; the exe
  was not rebuilt yet.
- [ ] (#13) **Needs Bryce:** how many people speak in Test #2 (now four), whether six remote speakers
  is right for Test #4, and whether Test #5's 71 s line is the second person.

## From the second transcription after Stop (WP15, built, off by default, not yet accepted)

- [x] **Built:** `retranscribe.py` and a stage in `finalize_meeting` ("Improving the transcript").
  Settings: "Improve the transcript after the meeting (slower), with <model>" (`final_transcribe`,
  `final_model` small.en, `final_beam_size` 5); `huddle-helper finalize --final-model` /
  `--no-final-transcribe`. Each line keeps its place; the live text is kept when the new text
  is empty, under half or over twice the words, when a call fails, or when the whole stage
  fails. Later invented segments are dropped on every second-pass call. The meeting's whole
  vocabulary is used for every line (a term added late now helps the early lines too).
- [ ] (#32) **Paused before acceptance (Bryce, 2026-11-11):** wait for the public meeting audio and
  transcript as the test baseline. Not run yet: the real final pass with the stage on (Tests
  #2-#5), word error against a reference, time on the dev PC and the laptop, the Settings
  dialog by eye, the exe rebuild. Unit tests only so far (11, scripted transcriber).
- [ ] (#32) **WP14 (Parakeet against small.en) stopped part-way** on a usage limit. `tools/retx_lab.py`,
  `tools/wer.py` and `tests/test_wer.py` are committed (197797d) but unfinished; no
  results were reported. Until it is done the stage ships Whisper only.
- [ ] (#2) Departures from the plan: no check on `drop_hallucinated` (the transcriber does not report
  it); a final line's start and end follow the new word times, within 200 ms of the live line's.

## From Test #6: a real work call on the target laptop (2026-11-11)

Setup: the packaged app on the target user's laptop, the same devices as Test #4 (webcam mic,
Realtek headphones), base.en, Keep audio on. 26 min 17 s; 329 live lines, 234 final lines.
Three remote people plus the laptop's user (confirmed by Bryce); here they are Remote A, B
and C. Bryce named speakers while recording, placed one bookmark, muted three times, and
added a vocabulary term from the final transcript after Stop (it applies from the next
meeting). Copy in `lab\src\` (the 2026-11-11 08:33 folder; `lab\` is not in git). No names
or titles from this call go into the repo or the issues.

### Worked

- [x] Clean Stop, `DONE`, no gaps except the three mutes (2 min 20 s, 4 min 7 s, 3 s), all in
  `meta.json`. One bookmark (button, no type) in `marks.jsonl`.
- [x] Live delay median 1.1 s, p90 1.6 s (Test #4 on this laptop: 1.3 s / 2.4 s), backlog
  never above 2, max lag 3.0 s. One line took 11.3 s; which one is not recorded.
- [x] (#14) **Final pass on the laptop: 301 s for 1577 s of audio (0.19×)**, against 0.6× for
  Test #4 before WP2 and WP13. Diarization is still 280 s of the 301 s; fingerprinting 19 s.
  A 1-hour meeting would now take about 11.5 minutes. The 0.1× target is still not met.
- [x] **Live labels kept the three remote people apart.** Each big live label is one person
  in the final file: 182 of 187 s, 131 of 141 s and 275 of 275 s. Three more labels hold
  13 s between them. Compare Test #1, where every remote line had one label.
- [x] **The final file has the right number of remote speakers (3)**, each named from tags
  (`speakers.json`, `from_tags: true`, no conflicts).
- [x] **Naming on a real call (#9):** two live labels were named in the first 75 s; Bryce then
  named 36 lines one by one, on purpose, to test line naming (most of them Remote C, whose
  live label was already clean). Two lines were named, then renamed; the last name won.
  35 of 36 named lines are under that name in the final file.

### Open

- [x] (#26, closed: the picker says so) **A named line under 0.5 s went to another speaker.**
  A 0.3 s line named Remote A is under Remote C in the final file. The picker did not say this
  can happen.
- [ ] (#6, #36) **Echo dedupe dropped 2 mic lines although headphones were on** (Test #4, same
  laptop and headphones: none). Either sound leaked into the webcam mic or the laptop's user
  repeated a remote line. Not checked: the dropped lines are not written anywhere. While
  recording, a drop makes "Play" ask about headphones first.
- [ ] Short lines are still common: 106 of 329 live lines and 59 of 234 final lines are under
  1 s. 28 live lines have confidence under 0.4.
- [ ] (#21) The app log is on the laptop and was not checked.
- [ ] Next meeting: check that the vocabulary term added after Stop is used.

## From Test #7: a real work call on the target laptop with the second transcription on (2026-11-11)

Setup: the packaged app on the target laptop, same devices as Test #6, base.en live, Keep audio
on, and the second transcription after Stop **on** (small.en). 56 min 38 s; 702 live lines, 594
final lines. Nine remote people plus the laptop's user; here they are Remote A–I by final talk
time. Bryce entered attendees before the call, named speakers and lines during it, added a
vocabulary term from a line during the call, and placed two bookmarks. Copy in `lab\src\` (the
2026-11-11 09:33 folder; `lab\` is not in git). No names, titles, terms or bookmark labels from
this call go into the repo or the issues.

### Worked

- [x] Clean Stop, `DONE`. Two mutes (13 min 40 s and 1.4 s) in `meta.json`; no mic line inside
  either. Live delay median 1.1 s, p90 1.7 s, max 10.6 s; backlog never above 3, max lag 4.4 s.
- [x] (#11) **Two bookmarks, both made from a line**: built-in "action" type, each with a label,
  at the line's start (`via: line`, `seq` set). `mark_kinds` is `{}`, correct for a built-in type.
- [x] (#9) **Naming before and during the call:** 8 attendees, 10 live labels named (three of them
  the same person, joined into one final speaker of 351 s), 20 lines named one by one. All 20
  named lines are under that name in the final file, including two 0.4 s lines (compare #26).
  Nine final remote speakers, all named from tags, no conflicts; the ninth was not an attendee
  and was named from lines only.
- [x] **Line names fixed a live label that held two people.** The live label named Remote E also
  held 37 s of Remote D (14 lines). Bryce named 11 of them Remote D; the final pass put all 14
  under Remote D, so 3 unnamed lines followed the voice, not the speaker tag. This is the
  outcome the #20 decision ("fix with a line-level name") relied on.
- [x] **A term added during the call reached earlier lines.** Two vocabulary changes (confirmed
  by Bryce): a person's name at 4:11 (live uses it from 4:44 on) and a team-tool acronym at
  16:50, added from the line at 16:36 (live uses it from 35:15 on). The final file has the name
  in 14 lines (live 11; three of the new ones are before 4:11) and the acronym in 9 (live 6).
  The acronym's three final-only lines, 14:49, 16:36 and 28:15, **were all really said**
  (Bryce), so none of them is invented; the live pass missed the one at 28:15 although the term
  was already in use. Attendee names also rose: the 8 names are in 56 final lines against 43.
- [x] (#13) **The speaker count is right:** nine remote people (Bryce). Remote I, named from a
  live label 21 s in, has three final lines (8 s), and all three are Remote I's.

### Open

- [ ] (#14, #32) **The final pass took 34 min on the laptop for 57 min of audio (0.61×).** The
  second transcription is 1528 s (0.45×), diarization 507 s (0.15×; Test #6 0.18×),
  fingerprinting 22 s. Stop was at 10:29; the final file was written at 11:03. A 1-hour
  meeting with the stage on means about 36 minutes of waiting. The stage changed 372 lines and
  kept the live text on 21.
- [x] (#20, #25) **Remote H's update is split: 21.6 s (5 lines, 16:46–17:33) is under Remote B.**
  Bryce confirmed they are Remote H's. Only Remote H's first line (16:08, 8 s) is under Remote H,
  and only because Bryce named that line. Live, all six lines had the label Bryce had named
  Remote B at 12:35.

  **Cause (diagnosed on the copy, `lab\cache_t7`, clustering replayed from cached fingerprints):
  both.** Without names, the clustering puts Remote B (142 s) and Remote H in one cluster, so
  Remote H would disappear. With line names only, Remote H's named line keeps all of Remote H's
  pieces apart from Remote B: right. Adding the speaker-level name is what moves them. Each of
  Remote H's pieces overlaps a line under the live label named Remote B, and it passes the 0.40
  tag gate against Remote B's centroid (0.60, 0.54, 0.41, 0.41). It is far more like Remote H's
  named line (0.89, 0.84, 0.57, 0.68). The centroid includes those pieces, and Remote B's own
  pieces score a median of 0.59 against it, so no gate value separates them. Differently named
  pieces never share a cluster, so the speaker name wins and the line name protects only its
  own line.
  **Fixed in `tags.piece_names`:** a speaker-level name does not apply to a piece that is more
  like another name's line-named pieces than like its own name's centroid. Two tests in
  `tests/test_tags.py`; the clustering one was red on the old code. Replayed from cached
  fingerprints, old rule against new:
  - **Test #7:** 5 pieces change (28 s). Remote H's 4 go back to Remote H. One 7 s line at 21:45
    moves from Remote E to Remote D; it is in the middle of Remote D's answer, so it is probably
    right too (not confirmed).
  - **Tests #5 and #6 with their real names:** nothing changes. Test #6 has 36 named lines.
  - **The WP11 named runs on Tests #3 and #4:** nothing changes. None of them pairs a
    speaker-level name with a line name for another person.

  So #20's "fix with a line-level name" now holds when the line-named person shares a live
  label with someone else's speaker-level name. Not run: a full final pass with the stage on.
- [ ] (#32) Word error is unknown; no reference transcript. Both lines that read oddly with the
  acronym in them still have wrong words around it.
- [ ] One vocabulary term never appears in either file, though a related word does (10:43).
  Not known whether it was said.
- [ ] (#6, #36) Echo dedupe dropped 1 mic line (headphones on); not written anywhere.
- [ ] Short lines: 195 of 702 live lines and 125 of 594 final lines are under 1 s; 35 live lines
  have confidence under 0.4 (18 final).
- [ ] (#21) The app log is on the laptop and was not checked.

## From Test #8: a real work call on the laptop's speakers, with a headset plugged in mid-call (2026-11-11)

Setup: the packaged app on the target laptop, base.en live, Keep audio on, second transcription
after Stop on (small.en). It started on the laptop's built-in mic array and speakers, with no
headset. 16:52 into the call Bryce plugged in a headset: Windows made it the default mic and
output, and the call app followed. 26 min 51 s; 181 live lines (7 mic), 160 final lines (6 mic).
Six remote people plus the laptop's user, Remote A–F by final talk time (confirmed by Bryce).
Bryce named the six live labels during the call, and muted the app three times, all on purpose.
Three vocabulary terms were entered before the call and not changed. Bryce's name is in 4 lines
in both files; the other two terms are in neither, and whether they were said is not known.
No bookmarks, no clips played. Copy in `lab\src\` (the 2026-11-11 11:35 folder; `lab\` is not in
git). No names, titles or terms from this call go into the repo or the issues.

### Worked

- [x] Clean Stop, `DONE`. `gaps` holds the three mutes (6 min 34 s, 3 min 15 s, 7 min 16 s) and
  one 0.4 s `default audio device changed` gap on each stream at the switch.
- [x] (#6) **Plugging in a headset mid-call:** the capture re-opened both streams within 0.4 s
  and followed the new default. The headset's connect sound is in the call audio 5 s later, the
  call's audio returns on the same stream at 19:09, and the next mic line (19:20) was recorded
  through the headset. The call audio is silent from 16:53 to 19:09. Boosted and transcribed, it
  holds no speech: the laptop's user was talking then, muted in both the call and the app (Bryce).
- [x] (#6, #36) **Speakers with no headset: no echo.** With the app's mic open from 6:34 to 16:04,
  the remote people talked for 485 s through the laptop's speakers. The mic array stays at its
  noise floor (±1 LSB) all through it; only the laptop's user's own speech is above it. Echo
  dedupe dropped nothing, and no remote speech is under "You". This holds for the laptop's mic
  array, which seems to cancel speaker sound itself; a plain USB mic or webcam mic may not.
- [x] (#13) **The speaker count is right:** 6 remote speakers in the final file, plus the laptop's
  user, 7 in all (Bryce).
- [x] **Live labels kept every person apart.** Each live label is one final speaker for at least
  99% of its time: A 417.6 of 420.0 s, B 304.8 of 306.3, C 291.2 of 292.8, D 52.0 of 52.0,
  E 44.6 of 44.9, F 24.1 of 24.1. There was no extra label to fold.
- [x] (#9) Naming: the six live labels were named between 0:26 and 14:30; the final file names all
  six from tags, no conflicts. No line names.
- [x] Short lines are far fewer than in earlier tests: 11 of 181 live lines (6%) and 8 of 160 final
  lines (5%) are under 1 s, against 28% and 21% in Test #7. One line in each file has confidence
  under 0.4.
- [x] (#14, #32) **Final pass: 695 s for 1611 s of audio (0.43×)**, against 0.61× in Test #7. The
  second transcription took 422 s (0.26×; Test #7 0.45×) and changed 120 lines, keeping the live
  text on 16. Diarization took 247 s (0.15×, the same as Test #7) and fingerprinting 25 s. Stop
  was at 12:01:53 and the final file was written at 12:13:28, 11.6 minutes later.

### Open

- [ ] (#8) **The live view was slower than in Tests #6 and #7:** delay median 1.5 s, p90 3.6 s,
  max 8.3 s, max lag 6.8 s (Tests #6 and #7: median 1.1 s, p90 1.6–1.7 s). The backlog never went
  above 2. The cause is not known.
- [ ] (#37) `meta.json` `devices` still names the starting mic array and speakers after the
  switch, so the folder doesn't say the last 10 minutes came from the headset.
- [ ] (#21) The app log is on the laptop and was not checked.
