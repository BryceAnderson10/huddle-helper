"""Shared mic toggled while recording (#53): the line-start rule, one live grouping across stretches, Stop's
clean-up, the window's check box, dividers and mute tooltip. Fakes only."""

import tkinter as tk
from types import SimpleNamespace

import numpy as np
import pytest

from huddle_helper import app, config, meeting as mt
from huddle_helper.app import AppState
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.gui import logic
from huddle_helper.recorder import Components, Recorder, Status
from huddle_helper.transcript_model import DividerRow, TranscriptModel
from huddle_helper.vad import EnergyScorer
from huddle_helper.writer import read_events

from .conftest import ToneEmbedder, ToneTranscriber, silence, tone
from .helpers import make_meeting, play_stub
from .test_gui_logic import model_for, muted_state, recording_state

A, B = 300, 1300  # two voices on the mic
# Mic lines start at about 0.5, 3.5, 6.5, 9.5 and 12.5 s: A, B, A, B, A.
MIC = np.concatenate([silence(0.5), tone(A, 2.0), silence(1), tone(B, 2.0), silence(1), tone(A, 2.0),
                      silence(1), tone(B, 2.0), silence(1), tone(A, 2.0), silence(1)])


class HeldCapture(FileCapture):
    """A file capture that starts feeding only on ``release()``, so toggles can be placed first."""

    def start(self, t0):
        self._held = t0

    def release(self):
        FileCapture.start(self, self._held)


def record(tmp_path, toggles, during=None, shared_mic=False):
    """Record MIC with the shared mic box toggled at ``toggles`` ``[(on, at_ms), ...]`` on the meeting clock."""
    s = config.Settings(meetings_dir=str(tmp_path / "meetings"), keep_audio=True, diarize_system=False)
    holder = {}

    def factory(**kw):
        holder["cap"] = HeldCapture(kw["on_audio"], {"mic": MIC, "system": None})
        return holder["cap"]

    comps = Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(),
                       scorer_factory=EnergyScorer, capture_factory=factory)
    rec = Recorder(s, comps)
    folder = rec.start("Unit Test", shared_mic=shared_mic)
    for on, at in toggles:
        assert rec.set_shared_mic(on, at_ms=at) is on
    if during is not None:
        during(folder, rec)
    holder["cap"].release()
    holder["cap"].finished.wait()
    rec.stop()
    return folder


def mic_lines(folder):
    return [(e["start_ms"], e["speaker"]) for e in read_events(folder.live_path)
            if e["type"] == "segment" and e["source"] == "mic"]


def test_ticking_mid_meeting_separates_later_lines_and_the_same_voice_keeps_its_label(tmp_path):
    seen = {}
    folder = record(tmp_path, [(True, 3000), (False, 6000), (True, 9000)],
                    during=lambda f, rec: seen.update(f.read_meta()))
    assert seen["shared_mic"] == [[3000, 6000], [9000, None]] and seen["settings"]["diarize_mic"] is True
    lines = mic_lines(folder)
    assert [sp for _, sp in lines] == ["You", "MIC_SPEAKER_01", "You", "MIC_SPEAKER_01", "MIC_SPEAKER_02"]
    # voice B in the first stretch and again in the second: one label (the grouping is kept across stretches)
    meta = folder.read_meta()
    (a1, b1), (a2, b2) = meta["shared_mic"]
    assert (a1, b1, a2) == (3000, 6000, 9000) and isinstance(b2, int) and b2 >= 14_000  # closed at Stop
    assert meta["settings"]["diarize_mic"] is True


def test_a_line_keeps_the_mode_it_started_in_and_the_file_is_never_rewritten(tmp_path):
    # on at 4 s (while B's line from 3.5 s is in progress), off at 10 s (during B's line from 9.5 s)
    folder = record(tmp_path, [(True, 4000), (False, 10_000)])
    assert [sp for _, sp in mic_lines(folder)] == ["You", "You", "MIC_SPEAKER_01", "MIC_SPEAKER_02", "You"]
    events = read_events(folder.live_path)
    assert [e["type"] for e in events] == ["meeting_start"] + ["segment"] * 5 + ["meeting_end"]
    seqs = [e["seq"] for e in events if e["type"] == "segment"]
    assert seqs == sorted(set(seqs))  # one event per line, never a second version of one


def test_stop_drops_stretches_without_a_mic_line_and_closes_the_open_one(tmp_path):
    folder = record(tmp_path, [(True, 4000), (False, 10_000), (True, 11_800), (False, 12_200), (True, 15_000)])
    meta = folder.read_meta()
    assert meta["shared_mic"] == [[4000, 10_000]]
    assert all(end is not None for _, end in meta["shared_mic"])


def test_stop_with_no_mic_line_in_any_stretch_leaves_none(tmp_path):
    folder = record(tmp_path, [(True, 15_000)])
    meta = folder.read_meta()
    assert meta["shared_mic"] == [] and meta["settings"]["diarize_mic"] is False
    assert {sp for _, sp in mic_lines(folder)} == {"You"}


def test_unticking_a_meeting_started_shared_and_ticking_an_open_one_again_is_a_no_op(tmp_path):
    def during(folder, rec):
        assert rec.status.shared_mic  # ticked in the Start dialog: the live status says so
        assert rec.set_shared_mic(True, at_ms=1000) is True  # already open: nothing changes
        assert folder.read_meta()["shared_mic"] == [[0, None]]
        assert rec.set_shared_mic(False, at_ms=6000) is False
        assert not rec.status.shared_mic
        assert rec.set_shared_mic(False, at_ms=7000) is False  # already closed: nothing changes
        assert folder.read_meta()["shared_mic"] == [[0, 6000]]

    folder = record(tmp_path, [], during=during, shared_mic=True)
    assert [sp for _, sp in mic_lines(folder)] == ["MIC_SPEAKER_01", "MIC_SPEAKER_02", "You", "You", "You"]
    assert folder.read_meta()["shared_mic"] == [[0, 6000]]


def test_set_shared_mic_does_nothing_when_not_recording(tmp_path):
    rec = Recorder(config.Settings(meetings_dir=str(tmp_path)), Components(transcriber=ToneTranscriber()))
    assert rec.set_shared_mic(True) is False and not rec.shared_mic.any and not rec.status.shared_mic


# -- controller ------------------------------------------------------------------------------------
def test_controller_toggles_the_recorder(tmp_path):
    ctl = app.AppController(overrides={"meetings_dir": str(tmp_path)})
    assert ctl.set_shared_mic(True) is False  # nothing recording
    calls = []
    ctl._recorder = SimpleNamespace(set_shared_mic=lambda on: calls.append(on) or on, recording=True)
    ctl.state.phase = "recording"
    assert ctl.set_shared_mic(True) is True and calls == [True]


# -- view logic ------------------------------------------------------------------------------------
def test_check_box_shows_only_while_recording_and_reflects_the_meeting(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    ticked = recording_state(mid)
    ticked.recording.shared_mic = True  # the recorder's live status, e.g. ticked in the Start dialog
    v = logic.shared_mic_view(mid, m, ticked)
    assert v.visible and v.checked and v.text == "Several people on my mic"
    m.set_meta({**m.meta, "shared_mic": [[0, None]]})  # meta.json does not decide the tick
    assert not logic.shared_mic_view(mid, m, recording_state(mid)).checked
    for phase in ("stopping", "ready"):
        st = recording_state(mid)
        st.phase = phase
        assert not logic.shared_mic_view(mid, m, st).visible
    st = recording_state(mid)
    st.finalize = SimpleNamespace(meeting_id=mid)
    st.phase = "ready"
    assert not logic.shared_mic_view(mid, m, st).visible  # final pass
    assert not logic.shared_mic_view(mid, m, None).visible  # a finished meeting


def test_mute_tooltip_mentions_the_room_only_while_a_stretch_is_open(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)
    mid = m.meeting_id
    st = muted_state(mid)
    assert "everyone in the room" not in logic.mute_button_view(mid, m, st).tooltip
    assert "Everyone in the room" not in logic.mute_bar_view(mid, m, st).text
    st.recording.shared_mic = True
    assert "everyone in the room is muted" in logic.mute_button_view(mid, m, st).tooltip
    assert "Everyone in the room" in logic.mute_bar_view(mid, m, st).text


def test_dividers_come_from_meta_and_sit_above_a_line_starting_at_the_toggle(tmp_path):
    m = model_for(tmp_path, status=mt.STATUS_RECORDING)  # lines at 0 (system) and 3000 (mic)
    m.set_meta({**m.meta, "shared_mic": [[1000, 3000], [5000, None]]})
    rows = [(type(r).__name__, r.start_ms) for r in m.rows]
    assert rows == [("LineRow", 0), ("DividerRow", 1000), ("DividerRow", 3000), ("LineRow", 3000),
                    ("DividerRow", 5000)]
    assert [logic.divider_text(d) for d in m.dividers] == [
        "Separating speakers on your mic from here", "Mic is just you from here",
        "Separating speakers on your mic from here"]
    # after Stop: the end Stop gave the open stretch (ended_ms) is not a toggle, so it has no divider
    m.set_meta({**m.meta, "shared_mic": [[1000, 3000], [5000, 9050]], "ended_ms": 9050, "duration_s": 14.0})
    assert [(d.at_ms, d.on) for d in m.dividers] == [(1000, True), (3000, False), (5000, True)]
    # an older meeting without ended_ms: within 1 s of duration_s, as before
    m.set_meta({k: v for k, v in m.meta.items() if k != "ended_ms"} | {"duration_s": 9.1})
    assert [(d.at_ms, d.on) for d in m.dividers] == [(1000, True), (3000, False), (5000, True)]
    m.set_meta({**m.meta, "shared_mic": []})
    assert m.dividers == []


def test_dividers_show_in_a_past_meetings_live_view_but_not_the_final_view_or_exports(tmp_path):
    lines = [("mic", 0, 1000, "You", "hello."), ("mic", 4000, 5000, "MIC_SPEAKER_01", "hi.")]
    folder = make_meeting(tmp_path / "a", lines)
    folder.update_meta(shared_mic=[[3000, 6000]], duration_s=20.0)
    m = TranscriptModel.load(folder)
    assert [(d.at_ms, d.on) for d in m.dividers] == [(3000, True), (6000, False)]
    assert "from here" not in logic.export_text(m, "txt", "elapsed", False)

    folder = make_meeting(tmp_path / "b", lines, final=lines)
    folder.update_meta(shared_mic=[[3000, 6000]], duration_s=20.0)
    m = TranscriptModel.load(folder)
    assert m.final and m.dividers == []
    live = make_meeting(tmp_path / "c", lines)
    live.update_meta(shared_mic=[[3000, 6000]])
    m = TranscriptModel.load(live)
    assert len(m.dividers) == 2
    m.switch_to_final(events=[], speakers={})
    assert m.dividers == [] and not any(isinstance(r, DividerRow) for r in m.rows)


# -- window ----------------------------------------------------------------------------------------
@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    r.withdraw()
    yield r
    r.destroy()


class FakeApp:
    def __init__(self, tmp_path, state, folder):
        self.state, self.folder, self.calls = state, folder, []
        self.__dict__.update(play_stub())
        self.settings = SimpleNamespace(resolved_meetings_dir=lambda: tmp_path, hotkeys={"mute": "Ctrl+Alt+M"})

    def current_folder(self):
        return self.folder

    def set_shared_mic(self, on):
        """What the recorder does: change its stretches and say so in the live status."""
        self.calls.append(on)
        self.folder.write_shared_mic(self.folder.read_shared_mic().toggled(on, 2000 if on else 7000))
        self.state.recording.shared_mic = on
        return on


def test_window_check_box_toggles_and_draws_dividers(root, tmp_path):
    from huddle_helper.gui.window import MainWindow

    folder = make_meeting(tmp_path, [("mic", 0, 1000, "You", "hello."),
                                     ("mic", 8000, 9000, "You", "later.")], status=mt.STATUS_RECORDING)
    folder.update_meta(shared_mic=[])
    state = AppState(phase="recording", last_meeting=folder.meeting_id)
    state.recording = Status(state="recording", meeting_id=folder.meeting_id, elapsed_s=10, segments=2)
    ctl = FakeApp(tmp_path, state, folder)
    w = MainWindow(root, ctl)
    w.show(folder)
    root.update()
    assert w.shared_chk.winfo_ismapped() and not w.shared_var.get()
    assert "everyone in the room" not in w.mute_tip.text
    w.shared_chk.invoke()
    root.update()
    w._refresh_all()
    assert ctl.calls == [True] and w.shared_var.get()
    assert "Separating speakers on your mic from here" in w.view.plain_text()
    assert "everyone in the room" in w.mute_tip.text
    w.shared_chk.invoke()
    root.update()
    w._refresh_all()
    assert ctl.calls == [True, False] and not w.shared_var.get()
    text = w.view.plain_text()
    assert text.index("hello.") < text.index("Separating") < text.index("Mic is just you") < text.index("later.")
    assert "from here" not in folder.live_path.read_text(encoding="utf-8")  # nothing written to the transcript

    state.phase = "stopping"
    w._refresh_all()
    root.update()
    assert not w.shared_chk.winfo_ismapped()
    w.destroy()


def test_no_tray_item_or_hotkey_for_shared_mic():
    from huddle_helper import hotkeys, tray

    assert not any("shared" in k for k in list(hotkeys.DEFAULTS) + list(hotkeys.ACTION_LABELS))
    src = open(tray.__file__, encoding="utf-8").read().lower()
    assert "several people" not in src and "shared_mic" not in src
