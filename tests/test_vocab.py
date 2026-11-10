"""Custom vocabulary: term parsing, budget, prompt, segment stop/trim, recorder and app wiring."""

import threading
from types import SimpleNamespace

import numpy as np

from huddle_helper import app as app_mod, config, transcribe, vocab
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.recorder import Components, Recorder
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone


# -- vocab.py ---------------------------------------------------------------------------

def test_parse_terms():
    text = "  KoVa \n\n# a comment\nqdm\nQDM\nC#\r\nzone code\n   \nKOVA\n"
    assert vocab.parse_terms(text) == ["KoVa", "qdm", "C#", "zone code"]
    assert vocab.parse_terms("") == []


def test_build_prompt():
    assert vocab.build_prompt([]) is None
    assert vocab.build_prompt(["A"]) == "This call is about A."
    assert vocab.build_prompt(["A", "B"]) == "This call is about A and B."
    assert vocab.build_prompt(["A", "B", "C", "D"]) == "This call is about A, B, C and D."
    assert vocab.build_prompt([" A ", "", "B"]) == "This call is about A and B."


def words(text):  # a simple tokenizer for tests: one token per word
    return len(text.split())


def test_fit_budget_meeting_terms_first_and_dedupe():
    used, dropped = vocab.fit_budget(["Zed"], ["alpha", "zed", "Beta"], words, budget=100)
    assert used == ["Zed", "alpha", "Beta"] and dropped == []
    assert vocab.fit_budget([], [], words) == ([], [])


def test_fit_budget_counts_the_sentence_and_cuts_at_first_overflow():
    # one token per word: "This call is about A." = 5, "... A and B." = 7, "... A, B and C." = 8, +D = 9
    used, dropped = vocab.fit_budget([], ["A", "B", "C", "D", "E"], words, budget=8)
    assert used == ["A", "B", "C"] and dropped == ["D", "E"]
    assert words(vocab.build_prompt(used)) <= 8
    # meeting terms survive when Settings terms overflow
    used, dropped = vocab.fit_budget(["M1", "M2"], ["S1", "S2", "S3"], words, budget=7)
    assert used == ["M1", "M2"] and dropped == ["S1", "S2", "S3"]
    # a first term that is too long on its own leaves nothing
    assert vocab.fit_budget([], ["a b c d e f"], words, budget=6) == ([], ["a b c d e f"])


def test_plan_and_summary():
    p = vocab.plan([], ["A", "B", "C", "D", "E"], words)
    assert p.exact and p.total == 5
    p = vocab.VocabPlan(["A", "B"], ["C"], exact=False)
    count, dropped = vocab.summary_lines(p)
    assert count == "about 2 of 3 terms used" and "C" in dropped
    assert vocab.summary_lines(vocab.VocabPlan(["A"], [], True)) == ("1 of 1 terms used", "")
    assert vocab.summary_lines(vocab.VocabPlan([], [], True))[0] == "No terms yet"
    assert vocab.estimate_tokens("") == 0 and vocab.estimate_tokens("x") == 1


def test_settings_vocabulary_round_trip(tmp_path):
    s = config.Settings(vocabulary=["KoVa", "zone code"])
    path = tmp_path / "s.json"
    config.save(s, path)
    assert config.load(path).vocabulary == ["KoVa", "zone code"]
    assert config.Settings.from_dict({"vocabulary": "nonsense"}).vocabulary == []
    assert config.Settings().vocabulary == [] and config.Settings().vocabulary is not config.Settings().vocabulary


# -- transcribe.read_segments -----------------------------------------------------------------

def seg(start, end, text="word", alp=-0.3, cr=1.2, nsp=0.01):
    return SimpleNamespace(start=start, end=end, text=text, avg_logprob=alp, compression_ratio=cr,
                           no_speech_prob=nsp, words=None)


def lazy(segs, log):
    for g in segs:
        log.append(g)
        yield g


def test_read_segments_stops_after_a_segment_near_the_end():
    a, b, c = seg(0, 3), seg(3, 5.6), seg(5.6, 9)
    log = []
    kept = transcribe.read_segments(lazy([a, b, c], log), audio_s=6.0)  # b ends 0.4 s from the end
    assert kept == [a, b] and log == [a, b]  # c was never pulled from the generator
    log = []
    kept = transcribe.read_segments(lazy([a, b, c], log), audio_s=6.3)  # 0.7 s away: not within
    assert kept == [a, b, c] and log == [a, b, c]


def test_read_segments_trims_late_junk_but_never_the_first_segment():
    first_bad = seg(0, 2, alp=-1.5, cr=3.0, nsp=0.5)
    low = seg(2, 3, alp=-0.9)
    repeated = seg(3, 4, cr=2.5)
    unsure = seg(4, 5, nsp=0.21)
    good = seg(5, 6, alp=-0.77, nsp=0.06)
    kept = transcribe.read_segments([first_bad, low, repeated, unsure, good], audio_s=60)
    assert kept == [first_bad, good]


def test_read_segments_keeps_ignored_segments_and_does_not_count_them_as_kept():
    silent = seg(0, 1, text="", alp=-2)  # empty text: skipped by the caller, not "the first segment"
    junk_first = seg(1, 2, alp=-0.95)  # first real segment: kept even though it is poor
    kept = transcribe.read_segments([silent, junk_first, seg(2, 3, alp=-0.95)], audio_s=60)
    assert kept == [silent, junk_first]


class FakeModel:
    def __init__(self, segs):
        self.segs, self.kwargs, self.pulled = segs, None, []
        self.hf_tokenizer = None

    def transcribe(self, audio, **kw):
        self.kwargs = kw
        return lazy(self.segs, self.pulled), None


def whisper_with(segs):
    w = transcribe.WhisperTranscriber.__new__(transcribe.WhisperTranscriber)
    w._beam, w._model, w.name = 1, FakeModel(segs), "fake"
    return w


def test_whisper_without_prompt_is_untouched_and_with_prompt_stops_and_trims():
    audio = (0.3 * np.ones(16000 * 4)).astype(np.float32)  # 4 s
    segs = [seg(0, 2, "Real words."), seg(2, 3.9, "Mm.", alp=-1.2), seg(3.9, 4.5, "Late.")]

    plain = whisper_with(segs)
    out = plain.transcribe(audio)
    assert "hotwords" not in plain._model.kwargs
    assert out.text == "Real words. Mm. Late." and len(plain._model.pulled) == 3  # nothing stopped or trimmed

    prompted = whisper_with(segs)
    out = prompted.transcribe(audio, prompt="This call is about A.")
    assert prompted._model.kwargs["hotwords"] == "This call is about A."
    assert "initial_prompt" not in prompted._model.kwargs
    assert out.text == "Real words."  # "Mm." trimmed
    assert len(prompted._model.pulled) == 2  # stopped after the segment ending 0.1 s from the end

    empty = whisper_with(segs)
    empty.transcribe(audio, prompt="")  # empty = no prompt
    assert "hotwords" not in empty._model.kwargs and len(empty._model.pulled) == 3


# -- recorder ---------------------------------------------------------------------------

def record(tmp_path, comps_tr, settings, terms=(), midway=None):
    holder = {}

    def factory(**kw):
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": None, "system": holder["audio"]})
        return holder["cap"]

    holder["audio"] = np.concatenate([silence(0.5), tone(1100, 2.0), silence(1), tone(1100, 2.0), silence(2)])
    rec = Recorder(settings, Components(transcriber=comps_tr, embedder=ToneEmbedder(),
                                        scorer_factory=EnergyScorer, capture_factory=factory))
    folder = rec.start("Vocab", terms)
    if midway:
        midway(rec)
    holder["cap"].finished.wait()
    rec.stop()
    return rec, folder


def test_recorder_passes_prompt_and_writes_meta(tmp_path):
    tr = ToneTranscriber()
    s = config.Settings(meetings_dir=str(tmp_path / "m"), vocabulary=["KoVa", "QDM", "kova"])
    _rec, folder = record(tmp_path, tr, s, terms=["Zentrix", "qdm"])
    assert tr.prompts and set(tr.prompts) == {"This call is about Zentrix, qdm and KoVa."}
    meta = folder.read_meta()
    assert meta["vocabulary"] == ["Zentrix", "qdm", "KoVa"]
    assert meta["vocabulary_changed_at_ms"] == []


def test_recorder_without_vocabulary(tmp_path):
    tr = ToneTranscriber()
    _rec, folder = record(tmp_path, tr, config.Settings(meetings_dir=str(tmp_path / "m")))
    assert set(tr.prompts) == {None}
    assert folder.read_meta()["vocabulary"] == []


def test_recorder_budget_drops_overflow(tmp_path):
    tr = ToneTranscriber()  # no count_tokens: the estimate is used
    many = [f"Term{i:03d}" for i in range(100)]
    s = config.Settings(meetings_dir=str(tmp_path / "m"), vocabulary=many)
    _rec, folder = record(tmp_path, tr, s, terms=["Mine"])
    used = folder.read_meta()["vocabulary"]
    assert used[0] == "Mine" and 5 < len(used) < 101
    assert vocab.estimate_tokens(vocab.build_prompt(used)) <= vocab.TOKEN_BUDGET


def test_mid_meeting_change_applies_from_next_utterance_and_is_recorded(tmp_path):
    class Changing(ToneTranscriber):
        """Changes the vocabulary (twice, the second time with the same terms) during its first call."""

        rec = None
        ready = threading.Event()

        def transcribe(self, audio, prompt=None):
            self.ready.wait(10)  # the recorder exists
            first = not self.prompts
            result = super().transcribe(audio, prompt)
            if first:
                self.rec.set_vocabulary(settings_terms=["New", "Newer"])
                self.rec.set_vocabulary(settings_terms=["New", "Newer"])  # same terms: not a change
            return result

    tr = Changing()
    s = config.Settings(meetings_dir=str(tmp_path / "m"), vocabulary=["Old"])
    rec, folder = record(tmp_path, tr, s, midway=lambda r: (setattr(tr, "rec", r), tr.ready.set()))
    assert tr.prompts[0] == "This call is about Old."  # lines already in flight keep their prompt
    assert tr.prompts[1:] and set(tr.prompts[1:]) == {"This call is about New and Newer."}
    meta = folder.read_meta()
    assert meta["vocabulary"] == ["New", "Newer"]
    assert len(meta["vocabulary_changed_at_ms"]) == 1 and meta["vocabulary_changed_at_ms"][0] >= 0
    assert len([e for e in read_events(folder.live_path) if e["type"] == "segment"]) == 2


# -- app controller ---------------------------------------------------------------------

class StubRecorder:
    def __init__(self):
        self.calls = []
        self.meeting_terms = ["Mine"]

    def set_vocabulary(self, **kw):
        self.calls.append(kw)


def test_update_settings_pushes_vocabulary_into_running_recorder():
    notes = []
    ctl = app_mod.AppController(notify=notes.append)
    stub = StubRecorder()
    ctl._recorder = stub
    ctl.state.phase = "recording"
    new = config.Settings.from_dict(ctl.settings.to_dict())
    new.vocabulary = ["KoVa"]
    ctl.update_settings(new)
    assert stub.calls == [{"settings_terms": ["KoVa"]}]
    assert notes and "Vocabulary saved" in notes[-1]
    # unrelated change: nothing pushed
    other = config.Settings.from_dict(new.to_dict())
    other.keep_audio = not other.keep_audio
    ctl.update_settings(other)
    assert len(stub.calls) == 1 and "next recording" in notes[-1]
    # plan includes the running meeting's terms first
    p = ctl.plan_vocabulary(["KoVa"])
    assert p.used == ["Mine", "KoVa"] and not p.exact
