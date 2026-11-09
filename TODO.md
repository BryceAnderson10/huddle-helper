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
- [ ] **Mic segment timing is squeezed:** 5 words in 0.56 s (57048–57608 ms). The start and
  end come from Whisper word timestamps clamped to the utterance; one of them is probably
  wrong. Code: `recorder.py` `_transcribe_loop`.
  → Not seen in Test #3: all 6 mic lines have normal word lengths (60–500 ms). Likely the
  quiet-audio problem behind Test #2's hallucination; close if Test #4 is also clean.

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
- [ ] **Mic timing.** The two mic lines here had sensible timings (2.1 s and 2.5 s). The
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
- [ ] **One spurious live speaker remains on the Test #3 replay:** line 4 ("Maybe it's
  technically complex.", 1.6 s of speech). The live pass measures the utterance including its
  VAD padding and trailing silence, so it counted as over 2 s. *Idea:* measure from the first
  to the last word. Low priority; the final file has it right.

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

- [ ] **Remote D is merged into Remote B (`SPEAKER_03`).** Final result: 3 speakers —
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

- [ ] **A sentence end cut off onto another speaker.** "Can we get a copy of that or is it just
  a dump of the" (`SPEAKER_03`) / "GCP table." (`SPEAKER_01`); "I'm kind of just getting…"
  (`01`) / "validation from the FMX people here." (`02`); "…we can always get the address from
  joining you." (`02`) / "Another table." (`01`). The short second piece is too short to
  fingerprint reliably, so it takes a neighbour's label, which can be the wrong neighbour.
  `join_fragments` only joins the same speaker. *Idea:* when the previous line has no closing
  punctuation, the gap is under ~1 s and the next line is very short (≤ 3 words), attach it
  to the previous line whatever its label.
- [ ] **Same-speaker fragments not joined:** "…all we would have to do is build out the…" /
  "Quodium side in the ETO process." Whisper's trailing "…" counts as closing punctuation.
  *Idea:* don't treat "…" as a sentence end in `join_fragments`. (Also "default to the" / "The
  zone code address" was not joined because its gap is 2.6 s; leave that as is.)
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
  *Ideas:* labels now come from `cluster_voices`, so sherpa's offline diarization is only used
  for split points. That pass embeds the audio twice more: inside sherpa and again in
  `merge_small_speakers`. Run the embedders with more than 1 thread (`SherpaEmbedder`
  defaults to `num_threads=1`), skip `merge_small_speakers` (it no longer affects labels), or
  find split points from per-word voice changes instead. Measure each stage before choosing.

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

### Microphone

- [ ] **The Kiyo Pro webcam mic is quiet:** speech peak median 0.046 (warning limit 0.05), so
  the quiet-mic warning should have shown once. Confirm with the user that it appeared. Every
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
