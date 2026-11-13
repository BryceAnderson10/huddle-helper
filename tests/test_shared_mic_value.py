"""``SharedMic`` (#60): the one reader and writer of a meeting's shared mic stretches. Pure, no fakes."""

from huddle_helper.audio import SAMPLE_RATE
from huddle_helper.shared_mic import SharedMic

fm = SharedMic.from_meta


def stretches(sm):
    return [list(st) for st in sm.stretches]


def test_reads_the_list_and_older_meetings_from_the_flag():
    assert stretches(fm({"shared_mic": [[0, None]]})) == [[0, None]]
    assert stretches(fm({"shared_mic": [], "settings": {"diarize_mic": True}})) == []  # the list wins
    assert stretches(fm({"settings": {"diarize_mic": True}})) == [[0, None]]  # older: the whole meeting
    assert stretches(fm({"settings": {"diarize_mic": False}})) == [] == stretches(fm({}))
    assert stretches(fm({"shared_mic": [[0, 5000], "junk", [1, "x"]]})) == [[0, 5000]]
    assert fm({"shared_mic": [[0, 5000]]}).any and not fm({"shared_mic": []}).any


def test_writes_the_list_the_legacy_flag_and_ended_ms_once_stopped():
    meta = {"title": "T", "settings": {"keep_audio": True}}
    on = SharedMic.at_start(True).write(meta)
    assert on["shared_mic"] == [[0, None]] and on["settings"] == {"keep_audio": True, "diarize_mic": True}
    assert "ended_ms" not in on and meta == {"title": "T", "settings": {"keep_audio": True}}  # not changed
    off = SharedMic.at_start(False).write(meta)
    assert off["shared_mic"] == [] and off["settings"]["diarize_mic"] is False
    stopped = SharedMic.at_start(True).closed_at_stop(9000, [100]).write(meta)
    assert stopped["shared_mic"] == [[0, 9000]] and stopped["ended_ms"] == 9000
    assert fm(stopped) == SharedMic.at_start(True).closed_at_stop(9000, [100])  # reads back the same


def test_toggles_open_and_close_a_stretch_and_repeats_change_nothing():
    sm = SharedMic.at_start(False)
    assert not sm.is_open
    sm = sm.toggled(True, 2000)
    assert sm.is_open and sm.toggled(True, 3000) is sm
    sm = sm.toggled(False, 5000)
    assert not sm.is_open and sm.toggled(False, 6000) is sm
    assert stretches(sm.toggled(True, 8000).toggled(False, 7000)) == [[2000, 5000], [8000, 8000]]  # never < start


def test_a_line_belongs_to_the_stretch_it_starts_in():
    sm = fm({"shared_mic": [[100, 200], [300, None]]})
    assert sm.covers(100) and not sm.covers(200) and not sm.covers(250) and sm.covers(10**9)
    assert not fm({"shared_mic": [[100, 100]]}).covers(100)


def test_stop_closes_the_open_stretch_and_drops_those_without_a_mic_line():
    sm = fm({"shared_mic": [[0, 100], [100, 200], [300, None]]}).closed_at_stop(900, [50, 350])
    assert stretches(sm) == [[0, 100], [300, 900]] and sm.ended_ms == 900 and not sm.is_open
    assert stretches(SharedMic.at_start(True).closed_at_stop(900, [])) == []


def test_dividers_mark_toggles_but_not_the_end_stop_gave():
    while_recording = fm({"shared_mic": [[1000, 3000], [5000, None]]})
    assert while_recording.dividers() == [(1000, True), (3000, False), (5000, True)]
    # ended_ms: only an end equal to it is Stop's, however long the backlog took to drain
    stopped = {"shared_mic": [[1000, 3000], [5000, 9050]], "ended_ms": 9050, "duration_s": 14.0}
    assert fm(stopped).dividers() == [(1000, True), (3000, False), (5000, True)]
    untick_near_end = {"shared_mic": [[1000, 8500]], "ended_ms": 9050, "duration_s": 9.1}
    assert fm(untick_near_end).dividers() == [(1000, True), (8500, False)]
    # older meetings without ended_ms: within 1 s of duration_s is Stop's, as before
    older = {"shared_mic": [[1000, 3000], [5000, 9050]], "duration_s": 9.1}
    assert fm(older).dividers() == [(1000, True), (3000, False), (5000, True)]
    assert fm({"shared_mic": [[1000, 8000]], "duration_s": 9.1}).dividers() == [(1000, True), (8000, False)]


def test_mic_seconds_count_stretch_audio_with_an_open_one_running_to_the_end():
    assert fm({"shared_mic": [[0, 300_000], [3_600_000, None]]}).mic_seconds(7200) == 300 + 3600
    assert fm({"shared_mic": [[0, 99_000]]}).mic_seconds(10) == 10
    assert fm({"shared_mic": [[0, None]]}).mic_seconds(None) == 0


def test_spans_clip_to_the_audio_in_meeting_order():
    n = 10 * SAMPLE_RATE
    assert fm({"shared_mic": [[8000, None], [2000, 3000]]}).spans(n) == [(32000, 48000), (128000, n)]
    assert fm({"shared_mic": [[0, 99_000]]}).spans(n) == [(0, n)]
    assert fm({"shared_mic": [[12_000, 13_000]]}).spans(n) == []


def test_the_final_pass_separates_the_mic_only_with_a_stretch():
    assert fm({"shared_mic": [[0, 100]]}).separated_streams(True) == ["mic", "system"]
    assert fm({"shared_mic": [[0, 100]]}).separated_streams(False) == ["mic"]
    assert fm({"settings": {"diarize_mic": False}}).separated_streams(True) == ["system"]
    assert fm({}).separated_streams(False) == []
