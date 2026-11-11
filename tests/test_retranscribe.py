"""The second transcription after Stop (WP15), with a scripted transcriber and synthetic text."""

import numpy as np

from huddle_helper import meeting as mt
from huddle_helper.finalize import finalize_meeting
from huddle_helper.retranscribe import MIN_SPAN_MS, POST_MS, PRE_MS, retranscribe, span, suspect
from huddle_helper.segments import Segment, Word
from huddle_helper.transcribe import Transcription, read_segments
from huddle_helper.writer import read_events

from tests.test_diarize_finalize import FakeDiarizer, _make_meeting

SR = 16000


class Scripted:
    """Answers by the length of the audio it is given (ms) or with a default; records every call."""
    name = "scripted"

    def __init__(self, default="new text", by_ms=None, fail_on=None):
        self.default, self.by_ms, self.fail_on, self.calls = default, by_ms or {}, fail_on, []

    def transcribe(self, audio, prompt=None):
        ms = len(audio) * 1000 // SR
        self.calls.append((ms, prompt))
        if self.fail_on == ms:
            raise RuntimeError("boom")
        text = self.by_ms.get(ms, self.default)
        words = [Word(100 + 200 * i, 250 + 200 * i, " " + w) for i, w in enumerate(text.split())]
        return Transcription(text, 0.8, words)


def seg(start, end, text, source="system", speaker="SPEAKER_01"):
    return Segment(source, start, end, text, 0.5, speaker)


def test_span_pads_but_never_reaches_into_a_neighbour():
    s = seg(5000, 7000, "x")
    assert span(s, None, None, 60000) == (5000 - PRE_MS, 7000 + POST_MS)
    assert span(s, 4950, 7100, 60000) == (4950, 7100)  # close neighbours: stop at their edges
    assert span(s, 5200, 6900, 60000) == (5000, 7000)  # overlapping neighbours: the line's own span
    assert span(seg(50, 900, "x"), None, None, 1000) == (0, 1000)  # clamped to the audio


def test_suspect_refuses_empty_short_and_runaway_text():
    assert suspect("one two three four", "") == "empty"
    assert suspect("one two three four five six", "one two") == "under half the words"
    assert suspect("one two three", "one two three four five six seven") == "over twice the words"
    assert suspect("Yeah.", "Yeah, okay then.") is None  # a short line may gain two words
    assert suspect("Yeah.", "Yeah okay then please.") == "over twice the words"
    assert suspect("the binder gave us this", "the vendor gave us this") is None


def test_retranscribe_keeps_line_identity_and_shifts_word_times():
    segs = [seg(1000, 3000, "old one"), seg(100, 900, "mic old", "mic", "You"), seg(6000, 8000, "old two")]
    words = {("system", 1000): [Word(1000, 3000, " old one")]}
    audio = {"system": np.zeros(SR * 10, np.float32), "mic": np.zeros(SR * 10, np.float32)}
    tr = Scripted("fresh words here")
    seen = []
    out, new_words, stats = retranscribe(segs, words, audio, tr, "This call is about X.", seen.append)
    assert [(s.source, s.start_ms, s.end_ms, s.speaker) for s in out] == [
        (s.source, s.start_ms, s.end_ms, s.speaker) for s in segs]  # same order, place and speaker
    assert [s.text for s in out] == ["fresh words here"] * 3 and out[0].confidence == 0.8
    assert all(p == "This call is about X." for _ms, p in tr.calls)
    a = 1000 - PRE_MS  # the first system line's audio starts here
    assert [w.start_ms for w in new_words[("system", 1000)]] == [a + 100, a + 300, a + 500]
    assert ("system", 6000) in new_words and ("mic", 100) in new_words
    assert words == {("system", 1000): [Word(1000, 3000, " old one")]}  # the input is not changed
    assert (stats.lines, stats.changed, stats.kept_live, stats.no_audio) == (3, 3, 0, 0)
    assert seen[-1] == 1.0


def test_retranscribe_keeps_live_text_when_refused_failed_or_without_audio():
    segs = [seg(1000, 3000, "one two three four five six"),  # the new text will be too short
            seg(10000, 14000, "this call fails"),
            seg(20000, 20100, "blip"),  # short, but long enough with its padding: taken
            seg(20100, 20150, "tick"),  # hemmed in by both neighbours: too little audio, kept
            seg(20150, 23000, "after tick"),
            seg(100, 900, "no mic audio kept", "mic", "You")]
    audio = {"system": np.zeros(SR * 30, np.float32)}
    first_ms, second_ms = 2000 + PRE_MS + POST_MS, 4000 + PRE_MS + POST_MS
    tr = Scripted(by_ms={first_ms: "one"}, fail_on=second_ms)
    live_words = {("system", 1000): [Word(1000, 3000, " one two three four five six")]}
    out, new_words, stats = retranscribe(segs, live_words, audio, tr)
    assert [s.text for s in out] == ["one two three four five six", "this call fails", "new text", "tick",
                                     "new text", "no mic audio kept"]
    assert set(new_words) == {("system", 1000), ("system", 20000), ("system", 20150)}
    assert new_words[("system", 1000)] == live_words[("system", 1000)]  # refused: its live words stay
    assert 50 + POST_MS >= MIN_SPAN_MS > 50  # "tick" would have had enough audio but for its neighbours
    assert (stats.lines, stats.changed, stats.kept_live, stats.no_audio) == (6, 2, 2, 2)  # no audio: tick, mic


def test_same_words_with_other_punctuation_do_not_count_as_changed():
    tr = Scripted("Hello there, friend.")
    _out, _w, stats = retranscribe([seg(1000, 3000, "hello there friend")], {}, {"system": np.zeros(SR * 5, np.float32)}, tr)
    assert stats.changed == 0 and stats.kept_live == 0


def test_read_segments_without_stop_reads_on_but_still_drops_junk():
    class S:
        def __init__(self, start, end, text, alp=-0.3, cr=1.2, nsp=0.01):
            self.start, self.end, self.text = start, end, text
            self.avg_logprob, self.compression_ratio, self.no_speech_prob = alp, cr, nsp

    segs = [S(0.0, 2.9, " real one"), S(2.9, 3.4, " real last word"), S(3.4, 3.5, " See ya.", alp=-1.2, nsp=0.4)]
    assert [s.text for s in read_segments(iter(segs), 3.5)] == [" real one"]  # prompted: stops near the end
    assert [s.text for s in read_segments(iter(segs), 3.5, stop=False)] == [" real one", " real last word"]


def _final_texts(folder):
    return [(e["source"], e["start_ms"], e["text"]) for e in read_events(folder.final_path) if e.get("type") == "segment"]


def test_finalize_without_a_second_transcriber_is_unchanged(tmp_path):
    a, b = _make_meeting(tmp_path / "a"), _make_meeting(tmp_path / "b")
    finalize_meeting(a, FakeDiarizer, keep_audio=False)
    finalize_meeting(b, FakeDiarizer, keep_audio=False, transcriber_factory=None)
    assert _final_texts(a) == _final_texts(b)
    meta = a.read_meta()
    assert "final_transcriber" not in meta and "retranscribe" not in meta["final_stats"]["stages"]


def test_finalize_uses_the_second_transcription(tmp_path):
    folder = _make_meeting(tmp_path)
    folder.update_meta(vocabulary=["KoVa"])
    tr = Scripted(by_ms={})
    tr.default = "Better."
    stages = []
    finalize_meeting(folder, FakeDiarizer, keep_audio=False, progress=lambda s, f: stages.append(s),
                     transcriber_factory=lambda: tr)
    texts = _final_texts(folder)
    assert [t for src, _s, t in texts if src == "system"] == ["Better.", "Better."]
    assert [t for src, _s, t in texts if src == "mic"] == ["Hello."]  # no mic audio kept: live text
    # Every line keeps its place; its edges follow the new word times, within the padding.
    assert all(abs(got - live) <= PRE_MS for (_src, got, _t), live in zip(texts, [100, 500, 2500]))
    assert all(p == "This call is about KoVa." for _ms, p in tr.calls) and len(tr.calls) == 2
    meta = folder.read_meta()
    assert meta["final_transcriber"] == "scripted" and meta["status"] == mt.STATUS_COMPLETE
    stats = meta["final_stats"]
    assert stats["retx_lines_changed"] == 2 and stats["retx_lines_kept_live"] == 0
    assert "retranscribe" in stats["stages"]
    assert stages[0] == "Improving the transcript" and folder.done_path.exists()


def test_finalize_survives_a_failing_second_transcriber(tmp_path):
    folder = _make_meeting(tmp_path)

    def broken():
        raise FileNotFoundError("model folder renamed")

    finalize_meeting(folder, FakeDiarizer, keep_audio=False, transcriber_factory=broken)
    assert [t for _src, _s, t in _final_texts(folder)] == ["Hello.", "First.", "Second."]
    meta = folder.read_meta()
    assert meta["final_stats"]["retx_failed"] is True and "final_transcriber" not in meta
    assert folder.done_path.exists()


def test_final_transcriber_factory_is_off_by_default(monkeypatch):
    from huddle_helper import config, models, transcribe

    assert config.Settings().final_transcribe is False
    assert transcribe.final_transcriber_factory(config.Settings(), lambda: 1) is None
    monkeypatch.setattr(models, "whisper_installed", lambda name, root=None: False)
    on = config.Settings(final_transcribe=True)
    assert transcribe.final_transcriber_factory(on, lambda: 1) is None  # model missing: keep the live text
