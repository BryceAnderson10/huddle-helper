# Implementation plan: speaker labels and transcript quality

Nov 10, 2026 · for the development agent · Status: ready to start

This plan turns the quality experiments of Nov 10 into ordered, checkable work. The
measurements behind every number here are in [TODO.md](../TODO.md), sections "From the
quality experiments", "Speakers: other fingerprint models" and "What public benchmarks say".

**What it changes.** Two things, and nothing else:

1. The **voice fingerprint model** (speaker embedding), used by the live labels and the
   final pass. The shipped model cannot hold one cutoff across meetings; four others can.
2. The **text of the final transcript**, by transcribing the kept audio a second time after
   Stop with a stronger model. Live transcription is unchanged.

**Which document wins.** The ground rules of
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) section 1 apply unchanged (work on copies
in `lab\`, nothing from a real meeting in git, schema stays 1, no network at runtime, one
work package per branch, rebuild the exe). [OUTPUT_SPEC.md](OUTPUT_SPEC.md) is updated in
the same commit as any contract change. Work packages continue that plan's numbering.

## 1. Why these two

| Finding | Evidence |
| --- | --- |
| The shipped fingerprint model works only at a cutoff of 0.70–0.71 | Tests #3–#5, 542 s of confirmed speech; pair error 11.4% |
| Four other models work over a span of 0.3 | Pair error 0.2–0.5%, 0 s wrong, same speakers from all four |
| Changing the clustering does not help | Normalisation, long pieces first, centroid linkage, turn joining: all rejected or marginal |
| small.en after Stop removes the known mishears | Test #4: 8 → 0 with the vocabulary, 2.5× the transcription time |
| Parakeet may do better still | Public meeting-audio word error 11.2% against 17.9% (small.en) and 21.1% (base.en); untested here |

**The evidence is thin.** Four recordings, three of them a video played as the call, and
speaker counts for Tests #2 and #4 that nobody has confirmed. Every acceptance check below
therefore asks for a wide margin, not a pass at one setting, and WP12 widens the evidence
before anything ships.

## 2. Order of work

| WP | What | Size | Depends on |
| --- | --- | --- | --- |
| 12 | Evidence: confirmed speakers, a reference transcript, lab tools, the wespeaker check | S | — |
| 13 | Swap the fingerprint model and re-fit every cutoff | M | 12 |
| 14 | Lab only: Parakeet against small.en for the after-Stop transcript | S–M | 12 |
| 15 | Final pass: transcribe again after Stop | M–L | 14 |

WP13 and WP14 can run side by side: WP14 changes no product code. WP15 comes after WP13
because both edit `finalize.py` and WP13's change there is small.

**Not in this plan** (each needs its own decision): voice profiles kept across meetings
(stores voiceprints); a stronger model for live transcription; a different diarization
system (pyannote community-1, DiariZen); the attendee list as a cap on speaker count.
Revisit the cap after WP13: it may no longer be needed.

## 3. What exists today (facts this plan relies on)

- **One fingerprint model, three users.** `models.EMBEDDING_NAME` /
  `EMBEDDING_URL` (wespeaker ResNet34-LM, 26 MB) feeds `SherpaEmbedder` (live, 1 thread, one
  call per line, `recorder.py`), `finalize.embed_pieces` (final labels) and sherpa's offline
  diarizer (`SherpaOfflineDiarizer`, split points only).
- **Seven cutoffs sit on that model's score scale:**

  | Cutoff | Value | Where |
  | --- | --- | --- |
  | `live_speaker_threshold` | 0.75 | `config.py` → `OnlineClusterer` |
  | `adopt_threshold` | 0.6 | `diarize.py` `OnlineClusterer` |
  | `final_voice_threshold` | 0.70 | `config.py` → `cluster_voices` |
  | `fold_small_clusters` `min_similarity` | 0.70 | `diarize.py` |
  | `final_cluster_threshold` | 0.6 | `config.py` → sherpa's clustering |
  | `merge_small_speakers` `min_similarity` | 0.75 | `diarize.py` (off in the final pass since WP2) |
  | `SPEAKER_TAG_GATE` | 0.70 | `tags.py` |

- **Saved settings pin the old values.** The Settings dialog saves every field, so a new
  default needs a `SETTINGS_VERSION` bump and a `_MIGRATIONS` entry (now at version 4).
- **The final pass never transcribes.** `finalize_meeting` reads the live segments and
  `audio\words.jsonl`, and reads audio only for the diarized sources (the mic's is not read
  when `diarize_mic` is off). Final text is the live text, re-split and re-joined.
- **Fingerprints are never stored.** The final pass computes them again from the audio, so a
  meeting recorded with one model can be finished with another. `meta.json` already
  records `diarization_embedding`.
- **Models are located by `models._locate`** (bundled copy, else the user folder), downloaded
  by `models.install_all`, and copied into the exe by `packaging\build.ps1`. Downloads are not
  checked against a checksum today.
- **The vocabulary of a meeting is in `meta.json`** (`vocabulary`,
  `vocabulary_changed_at_ms`). `WhisperTranscriber.transcribe(audio, prompt)` applies it,
  with the stop and trim rules only when a prompt is given.
- **Lab tools:** `tools/final_lab.py` (`embed`, `sweep`, `matrix`, `score`, `diff`,
  `finalize`), `tools/speaker_models_embed.py`, `tools/speaker_models_score.py`,
  `tools/vocab_spike.py`. Truth files: `tests/data/truth/test-3.json`, `test-4.json`,
  `test-5.json`. Candidate models: `lab\models\speaker` (nine, checksums verified).

## 4. Design

### 4.1 Cutoffs travel with the model

The cutoffs are properties of a model's score scale, not user preferences, and today they are
spread over three files. Put them in one place:

```python
# diarize.py
@dataclass(frozen=True)
class VoiceProfile:
    model: str              # file stem, as in models.EMBEDDING_NAME
    live_match: float       # OnlineClusterer threshold
    live_adopt: float       # OnlineClusterer adopt_threshold
    final_match: float      # cluster_voices threshold
    fold_min: float         # fold_small_clusters min_similarity
    tag_gate: float         # tags.SPEAKER_TAG_GATE
    split_cluster: float    # sherpa FastClustering threshold (split points)

PROFILES: dict[str, VoiceProfile] = {...}   # one entry per supported model
def profile(model: str | None = None) -> VoiceProfile: ...
```

- `Settings.live_speaker_threshold`, `final_voice_threshold` and `final_cluster_threshold`
  become `float | None`, default `None` meaning "the profile's value". A number still
  overrides (the lab and tests use this). Migration 5 drops all three saved keys.
- `OnlineClusterer`, `cluster_voices`, `fold_small_clusters` and `tags` keep their
  parameters; callers pass the profile's values. No function reads a global.
- Keep the old model's profile in `PROFILES` with today's values, so the lab can still run
  it and the tests that use its measured similarities stay meaningful.

### 4.2 Picking a cutoff

Take the **centre of the range that passes everything**, never its edge, and record the
range beside the number in the profile's comment. A model whose passing range is narrower
than 0.15 on any cutoff is not shipped.

### 4.3 The second transcription

A new stage in `finalize_meeting`, before `relabel`, named "Improving the transcript" in
the progress text.

```python
# retranscribe.py (new, UI-free)
def retranscribe(segments, words, audio_by_source, transcriber, prompt_for, progress) -> RetxResult
# RetxResult: segments (same order, same seq, same start/end unless words say otherwise),
#             words (same keys as load_words), stats (lines changed, kept, seconds)
```

- **Span.** Each live line's audio from 200 ms before its start to 300 ms after its end
  (the lab's setting), clamped so it never reaches into the neighbouring line of the same
  source by more than the gap between them.
- **Line identity is kept.** One live line in, one line out, same `seq`, so speaker tags,
  bookmarks and click-to-hear keep pointing at the right line. Word times come back relative
  to the span and are shifted to meeting time, as `recorder.py` does today.
- **Keep the live line when the new one is suspect.** Use the live text and words if the
  new text is empty; if its word count is under half or over twice the live line's; or if
  `drop_hallucinated` removed words. Seen in the lab: an invented "See ya." on a line end
  and "Huddle Helper" → "Lidle Helper"; the first is caught by the trim below, the second
  is not catchable and is accepted.
- **Trim line ends.** Apply `read_segments`' later-segment rules to every second-pass call,
  with or without a vocabulary, and drop words that start after the line's own end plus the
  300 ms.
- **Vocabulary.** `prompt_for(start_ms)` returns the meeting's terms in force at that time
  (`meta.json` `vocabulary`), through `vocab.build_prompt`. If WP14 picks a model that
  cannot take a prompt, see WP14 step 5.
- **Both sources.** Read the mic's audio too. A source with no kept audio keeps its live
  lines.
- **Never lose the meeting.** Any failure in this stage logs, keeps the live text and
  carries on; `final_stats` records `retranscribe: "failed"`.
- **Threads.** The `final_threads` rule (half the cores up to 4, 1 while recording).

**Settings.** `final_transcribe: bool` and `final_model: str`, in the Settings dialog as
"Improve the transcript after the meeting (slower)" with the model beside it. Turning it
on installs the model if missing, like `whisper_model` does today. Additive keys, no
version bump.

**Contract (additive).** `meta.json` gains `final_transcriber` (model name, or absent) and
`final_stats.stages.retranscribe`; `final_stats` gains `retx_lines_changed` and
`retx_lines_kept_live`. `transcript.jsonl` and `audio\words.jsonl` are never rewritten.
OUTPUT_SPEC gains one sentence: final text may differ from live text for the same `seq`.

## 5. Work packages

### WP12 Evidence and lab foundations

Nothing in the product changes.

**Build**

1. **Confirmed speakers** (Needs Bryce 1–3). Write `tests/data/truth/test-2.json`; extend
   `test-4.json` with the groups the new models split out of the 534 s speaker; mark Test
   #5's group Q checked or correct it. Until then those groups stay under `"unchecked"`.
2. **A reference transcript** (Needs Bryce 4): the first 5 minutes of Test #4 and all of
   Test #5, corrected by ear, in `lab\ref\` (never in git). Add `tools/wer.py`: word error
   rate of any `transcript*.jsonl` or `lab\retx\*.json` against it, text normalised
   (case, punctuation, numbers as words). This replaces counting known terms.
3. **`final_lab.py embed --embedding <file.onnx>`** so the production path
   (`split_pieces`, `embed_pieces`) runs with any model, and fold the two
   `speaker_models_*.py` scripts into `final_lab.py` (`models-embed`, `models-score`).
   `score` takes `--fold-min`.
4. **Live replay in the lab:** `final_lab.py live <cache> --threshold --adopt` runs
   `OnlineClusterer` over the cached per-line fingerprints in time order and prints the
   number of live speakers and the share of speech whose live label agrees with the truth
   groups. Needs per-line (not per-piece) fingerprints: cache them in `embed`.
5. **The wespeaker check** (half a day at most). The large wespeaker models and both
   English CAM++ models score far below their published results here. Run sherpa's own
   sample clips (`fangjun-*.wav`, `leijun-*.wav` in the same release) through ResNet34-LM,
   ResNet293-LM and CAM++-LM: same-speaker pairs must outscore different-speaker pairs.
   Then score the plain `wespeaker_en_voxceleb_resnet34.onnx` on our recordings, and the
   LM models on pieces of 6 s or more only. Write what was found into TODO.md. This does
   not block WP13; it tells us whether the shipped model was ever running as intended.

**Tests.** `tools/wer.py` on synthetic text; the live replay on synthetic vectors.

**Done when.** `final_lab.py models-score` reproduces the TODO table; the truth files hold
whatever Bryce has confirmed; the check in step 5 is written up.

### WP13 Swap the fingerprint model

Closes TODO: *The main remote voice is split in two* (Test #5), *`SPEAKER_01` may still be
several people* (Test #4), *Check whether 4 speakers is right*, and the thin-margin note
under WP1.

**Build**

1. **Choose between the two finalists** in the lab: 3D-Speaker ERes2Net English (26 MB,
   same size as today's) and NeMo TitaNet small (40 MB; the only one with published results
   on meetings and telephone audio).

   | Measure | How |
   | --- | --- |
   | Final labels | `models-score`: wrong seconds and width of the passing range |
   | Live labels | WP12 step 4: agreement with truth, number of live speakers |
   | Short lines | Pair error on pieces under 2 s only |
   | Live cost | Seconds per line at 1 thread on the dev PC and the laptop |
   | Licence | Read each model card; record the licence and any attribution duty |

   Default to ERes2Net if the two tie. A model that needs attribution gets a line in the
   README and the About text.
2. **`VoiceProfile`** as in 4.1, with profiles for the old model and the chosen one.
   Fit all six cutoffs by 4.2. Starting points from the lab, final labels only:
   ERes2Net passes from 0.26 to 0.60, TitaNet small from 0.25 to 0.58.
3. **`models.py`:** new `EMBEDDING_NAME` / `EMBEDDING_URL`; verify the download against a
   SHA-256 kept beside the URL (add the same check for the segmentation archive). The old
   model file is left where it is; nothing deletes it.
4. **Settings migration 5** (4.1). `meta.json` `settings` keeps recording the values in
   force, plus the profile's model name.
5. **Split points.** Sherpa's offline diarizer also uses the model. Re-fit
   `split_cluster` so the number of pieces per recording stays within 10% of today's, then
   confirm with `final_lab diff` that changed line boundaries are improvements or neutral.
6. **Re-check the patches built for the old model**, without removing any: the gated fold,
   the weak-speaker adoption, the 2 s rule for founding a live speaker. For each, record in
   TODO.md whether it still changes any result on the kept recordings. Removal is a later,
   separate decision.

**Acceptance**

| Recording | Must hold |
| --- | --- |
| Tests #3, #4, #5 | 0 s of confirmed speech with the wrong speaker at the shipped cutoff and at ±0.08 around it |
| Test #2, Test #4's large speaker | Match what Bryce confirmed; if unconfirmed, report the counts and do not block |
| TTS sample | Exactly two remote speakers (`pytest -m models`) |
| Live replay, Tests #2–#5 | Live labels agree with the final speakers for at least 95% of speech; no more than one extra live speaker per meeting |
| With names | The WP8 and WP11 runs (Test #3 and #4 with two names) give the same named speakers |
| Speed | Final pass no slower than today; live p90 delay on a Test #4 replay no worse than 2.4 s |

**Tests.** Profile lookup and overrides; migration 5; checksum failure leaves no file
behind; the measured-similarity tests re-recorded for the new model beside the old ones
(letters and numbers only, as today).

**Done when.** The table holds, the exe is rebuilt with the new model bundled and starts a
recording, and TODO.md has the before and after for all four recordings.

**If it fails.** If live labels regress but final labels pass, ship the new model for the
final pass only (the two passes share no state) and keep the old one live.

### WP14 Lab: Parakeet against small.en

Nothing in the product changes. Decides the model WP15 ships.

**Build**

1. Extend the re-transcription script into `tools/retx_lab.py run|report` with a second
   back end: sherpa-onnx's offline recognizer with `sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8`
   (in `lab\models\asr`, checksum verified; CC-BY-4.0).
2. Run both on all four recordings, same spans: base.en beam 1 (control), small.en beam 5
   with and without the vocabulary, Parakeet.
3. **Measure:** word error against the WP12 reference; expected terms and known mishears
   (Test #4); lines empty, shortened or lengthened by half; invented text on line ends;
   time as a multiple of speech length at `final_threads` on the dev PC **and the laptop**;
   peak memory; whether word times are usable (median and worst offset from Whisper's on
   lines where the text agrees).
4. Try Parakeet on longer spans too (neighbouring lines of one source joined up to 20 s):
   it was trained on long audio and may lose accuracy on 1–2 s lines.
5. **Vocabulary for Parakeet.** Find out from the sherpa-onnx docs and by test whether
   hotwords work with this model. If not, measure a fallback: keep small.en with the prompt
   only for lines where base.en and Parakeet disagree near a vocabulary term. Do not build a
   find-and-replace of known mishears: it would rewrite correct words.

**Decision rule.** Parakeet ships if it beats small.en with the vocabulary on word error by
at least 15% relative, is no worse on the Test #4 terms, and costs no more than 0.5× the
speech length on the laptop. Otherwise small.en beam 5 ships. Write the table and the
decision into TODO.md.

### WP15 Final pass: transcribe again after Stop

Closes TODO: *base.en mishears names and jargon*, *Domain jargon is misheard throughout*.

**Build**

1. `retranscribe.py` as in 4.3, with a `FinalTranscriber` protocol so the Whisper and the
   sherpa back ends are interchangeable; the fake transcriber in `tests/conftest.py` gains
   a second-pass variant.
2. `finalize_meeting`: read audio for both sources; run the stage when
   `final_transcribe` is on and the model is installed; pass its segments and words to
   `relabel` and `join_fragments` unchanged. `app.py` and `cli.py` pass a
   `transcriber_factory`, as they pass `embedder_factory` today. `huddle-helper finalize`
   gains `--final-model` and `--no-final-transcribe`.
3. Settings, dialog row and model install (4.3). `models.py` learns the Parakeet files if
   WP14 picked it.
4. Progress: "Improving the transcript" with a fraction, before "Identifying speakers".
   Retry from the Meetings view runs the stage again.
5. OUTPUT_SPEC and the README's code map.

**Acceptance**

| Check | Must hold |
| --- | --- |
| Off | Final files byte-identical to before, on Tests #2–#5 |
| Test #4, on, with its vocabulary | Word error and term counts within the WP14 lab result; no line lost; every `seq` present once |
| Speakers | `final_lab score` unchanged from WP13 (new words must not move speakers) |
| Tags and bookmarks | The WP8 runs give the same named speakers; a line-level tag still lands on its line |
| Failure | With the model folder renamed, the pass completes on live text and says so in `final_stats` |
| Time | Whole final pass at most 0.6× the meeting's length on the dev PC; measured on the laptop and written down |
| While recording | A final pass started during another recording keeps that recording's p90 delay under 5 s |

**Tests.** The keep-live rules (empty, half, double, hallucination); the end trim; span
clamping against a close neighbour; word-time shift; vocabulary in force at a line's time;
failure fallback; stats fields; the off path.

**Done when.** The table holds, the exe is rebuilt, a real meeting has been finished with
it on the laptop, and TODO.md has the numbers.

**Default.** Ship off. Turn the default on in a follow-up commit once the laptop time is
known and accepted (Needs Bryce 6).

## 6. Needs Bryce

Items 1–4 feed WP12 and sharpen every later check; none blocks starting.

| # | What | For |
| --- | --- | --- |
| 1 | How many people speak in Test #2's clip, and roughly who says what | WP12, WP13 |
| 2 | Test #4: are six remote speakers right; who spoke lines 1–17, 117 and 121 | WP12, WP13 |
| 3 | Test #5: is the 71 s line the second person | WP12 |
| 4 | Correct by ear the first 5 minutes of Test #4 and all of Test #5 (about 30 minutes of work) | WP12, WP14 |
| 5 | Two or three new kept recordings of real calls with known attendees, ideally five or more people | WP13 acceptance on audio the cutoffs were not fitted to |
| 6 | On the laptop: live delay after WP13; final-pass time with the second transcription; accept or reject it as the default | WP13, WP15 |
| 7 | Accept the chosen models' licences and attribution wording | WP13, WP15 |

## 7. Risks to watch while building

- **Fitting to four recordings.** The new model's wide range is the protection; item 5
  above is the test. Re-run `models-score` on every new kept recording.
- **Shipped below the §4.2 rule (accepted for user testing, 2026-11-11, #1).** ERes2Net's
  `final_match` keeps the same speakers only from 0.34 to 0.47 (0.13 wide, under 0.15), and
  the ±0.08 acceptance margin fails at 0.48. Accepted because the old model passed only
  0.70–0.71 and the new one is better on every measure (live wrong labels 84 s → 8 s, confirmed
  final speech wrong 1.4 s → 0 s). Watch for: one voice split in two, or two people merged, on
  new recordings. Widening the top end is #23; the lower end rests on unconfirmed speaker
  counts (#13); the other cutoffs' ranges are #33.
- **Three of the four recordings are a video played as the call.** Real call audio with
  packet loss and crosstalk may behave differently; Test #4 is the only real one.
- **Live cost of the new model.** It runs once per line at 1 thread beside Whisper. Measure
  before fitting cutoffs.
- **Lines under 0.5 s still have no fingerprint** and take a neighbour's speaker. The model
  swap does not change that.
- **Parakeet without a vocabulary** could lose the jargon gains WP3 bought. WP14's rule
  guards this.
- **Download and bundle size.** Parakeet is several hundred MB; it downloads on enabling
  and is not bundled by default.
- **The second transcription can make a correct line worse.** The keep-live rules catch
  gross cases only. The reference transcript measures the net effect; if a recording gets
  worse overall, the stage stays off by default.
