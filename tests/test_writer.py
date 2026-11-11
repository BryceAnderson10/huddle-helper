import json

from huddle_helper.echo import is_echo, overlap_fraction, normalize_words
from huddle_helper.segments import Segment
from huddle_helper.writer import JsonlWriter, SegmentMerger, read_events


def seg(source, start, end, text, speaker=""):
    return Segment(source, start, end, text, 0.9, speaker)


def test_merger_waits_for_both_watermarks_and_orders_by_start():
    out = []
    m = SegmentMerger(["mic", "system"], out.append, echo_dedupe=False)
    m.add(seg("system", 3000, 4000, "b"))
    m.add(seg("mic", 1000, 2000, "a"))
    assert out == []  # no watermark has moved yet
    m.update_watermark("mic", 5000)
    assert out == []  # system could still produce something earlier
    m.update_watermark("system", 2500)
    assert [s.text for s in out] == ["a"]
    m.update_watermark("system", 9000)
    assert [s.text for s in out] == ["a", "b"]


def test_merger_holds_later_segment_behind_unfinished_earlier_one():
    out = []
    m = SegmentMerger(["mic", "system"], out.append, echo_dedupe=False)
    m.add(seg("mic", 5000, 6000, "later"))
    m.update_watermark("mic", 7000)
    m.update_watermark("system", 4000)  # system still busy with speech from 4000
    assert out == []
    m.add(seg("system", 4000, 8000, "earlier"))
    m.update_watermark("system", 9000)
    assert [s.text for s in out] == ["earlier", "later"]


def test_merger_drops_mic_echo_of_system_speech():
    out = []
    m = SegmentMerger(["mic", "system"], out.append, echo_dedupe=True)
    m.add(seg("system", 1000, 4000, "Legal still needs one more day."))
    m.add(seg("mic", 1050, 4050, "legal still needs one more day"))
    m.add(seg("mic", 6000, 7000, "Okay, thanks."))
    m.update_watermark("mic", 20000)
    m.update_watermark("system", 20000)
    assert [s.text for s in out] == ["Legal still needs one more day.", "Okay, thanks."]
    assert m.dropped_echo == 1


def test_merger_reports_each_echo_drop_with_the_system_lines_it_matched():
    out, drops = [], []
    m = SegmentMerger(["mic", "system"], out.append, echo_dedupe=True,
                      on_echo=lambda mic, match: drops.append((mic, match)))
    m.add(seg("system", 0, 3000, "can you send the report by friday"))
    m.add(seg("system", 3500, 5000, "and the slides"))
    m.add(seg("system", 9000, 10000, "much later"))
    m.add(seg("mic", 500, 3200, "send the report by friday"))
    m.add(seg("mic", 6000, 7000, "Okay, thanks."))
    m.flush_all()

    assert [s.text for s in out if s.source == "mic"] == ["Okay, thanks."]
    assert len(drops) == 1 and m.dropped_echo == 1
    mic, match = drops[0]
    assert (mic.start_ms, mic.end_ms, mic.text) == (500, 3200, "send the report by friday")
    # Both system lines inside the 1 s window, in time order; the far one is not compared.
    assert [(s.start_ms, s.text) for s in match.matched] == [
        (0, "can you send the report by friday"), (3500, "and the slides")]
    assert match.overlap == 1.0


def test_merger_reports_nothing_when_no_mic_line_is_echo():
    drops = []
    m = SegmentMerger(["mic", "system"], lambda s: None, echo_dedupe=True,
                      on_echo=lambda mic, match: drops.append(mic))
    m.add(seg("system", 0, 3000, "we ship on Friday"))
    m.add(seg("mic", 500, 2500, "completely different words"))
    m.flush_all()
    assert drops == [] and m.dropped_echo == 0


def test_mic_segment_waits_for_system_to_pass_its_end_before_echo_check():
    out = []
    m = SegmentMerger(["mic", "system"], out.append, echo_dedupe=True, echo_window_ms=1000)
    m.add(seg("mic", 1000, 3000, "hello there"))
    m.update_watermark("mic", 10000)
    m.update_watermark("system", 2000)  # past the start, not past end + window
    assert out == []
    m.add(seg("system", 1900, 3100, "hello there"))
    m.update_watermark("system", 10000)
    assert [s.source for s in out] == ["system"]


def test_flush_all_releases_everything():
    out = []
    m = SegmentMerger(["mic", "system"], out.append, echo_dedupe=False)
    m.add(seg("mic", 1, 2, "x"))
    m.flush_all()
    assert len(out) == 1


def test_echo_similarity():
    assert overlap_fraction(normalize_words("we ship Friday"), normalize_words("So we ship on Friday.")) == 1.0
    mic = seg("mic", 0, 1000, "completely different words")
    assert not is_echo(mic, [seg("system", 0, 1000, "we ship on Friday")])
    assert not is_echo(seg("mic", 10000, 11000, "we ship"), [seg("system", 0, 1000, "we ship")])


def test_jsonl_writer_and_partial_line(tmp_path):
    path = tmp_path / "t.jsonl"
    with JsonlWriter(path) as w:
        w.write({"type": "meeting_start"})
        w.write(seg("mic", 1, 2, "héllo").to_event(1))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"segment","seq":2,"te')  # crash mid-line
    events = read_events(path)
    assert [e["type"] for e in events] == ["meeting_start", "segment"]
    assert events[1]["text"] == "héllo"
    first = path.read_text(encoding="utf-8").splitlines()[1]
    assert list(json.loads(first)) == ["type", "seq", "start_ms", "end_ms", "source", "speaker", "text",
                                      "confidence"]
