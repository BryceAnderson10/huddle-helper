"""Attendees (L13): parsing the Start box, the vocabulary opt-in, the meta field, the choices, the number-key
target, the undo capture and the new-voice queue. Names are invented."""

import numpy as np

from huddle_helper import attendees as att, config, meeting as mt, tags
from huddle_helper.audio.filesource import FileCapture
from huddle_helper.gui import attendee_logic as al, logic
from huddle_helper.recorder import Components, Recorder
from huddle_helper.transcript_model import TranscriptModel

from .helpers import make_meeting
from .test_vocab import EnergyScorer, ToneEmbedder, ToneTranscriber, record, silence, tone


# -- parsing ----------------------------------------------------------------------------
def test_parse_splits_lines_commas_and_semicolons_and_drops_repeats_and_blanks():
    text = "Ana Cole, Ben\n\n  cy ;ANA cole\nDee Ray <dee@example.com>\n"
    assert att.parse_attendees(text) == ["Ana Cole", "Ben", "cy", "Dee Ray"]
    assert att.parse_attendees("") == [] and att.parse_attendees(" ,\n; ") == []


def test_parse_skips_the_stream_labels_bare_emails_and_caps_length_and_count():
    assert att.parse_attendees("You, Remote, ana@example.com, Ben") == ["Ben"]
    assert att.parse_attendees("x" * 100) == ["x" * att.MAX_NAME_CHARS]
    many = ", ".join(f"P{i}" for i in range(50))
    assert len(att.parse_attendees(many)) == att.MAX_ATTENDEES
    assert att.parse_attendees("  Ana   Cole ") == ["Ana Cole"]  # inner spaces collapse


def test_from_meta_and_numbered():
    assert att.from_meta({}) == [] and att.from_meta(None) == [] and att.from_meta({"attendees": "Ana"}) == []
    assert att.from_meta({"attendees": ["Ana", 3, "ana", "Ben"]}) == ["Ana", "Ben"]
    names = [f"P{i}" for i in range(1, 12)]
    assert att.numbered(names)[0] == (1, "P1") and att.numbered(names)[-1] == (9, "P9") and len(att.numbered(names)) == 9


def test_names_join_the_vocabulary_only_when_the_box_is_ticked():
    assert att.merge_terms(["QDM"], ["Ana", "ben"], False) == ["QDM"]
    assert att.merge_terms(["QDM", "Ben"], ["Ana", "ben"], True) == ["QDM", "Ben", "Ana"]


# -- the meeting's meta ------------------------------------------------------------------
def test_recorder_writes_attendees_only_when_there_are_some(tmp_path):
    s = config.Settings(meetings_dir=str(tmp_path / "m"))
    _rec, folder = record(tmp_path, ToneTranscriber(), s)
    assert "attendees" not in folder.read_meta()  # no attendees: meta.json is what it was

    holder = {}

    def factory(**kw):
        audio = np.concatenate([silence(0.5), tone(1100, 1.0), silence(1)])
        holder["cap"] = FileCapture(kw["on_audio"], {"mic": None, "system": audio})
        return holder["cap"]

    rec = Recorder(s, Components(transcriber=ToneTranscriber(), embedder=ToneEmbedder(),
                                 scorer_factory=EnergyScorer, capture_factory=factory))
    f2 = rec.start("With names", [], attendees=["Ana", "Ben", "ana", ""])
    holder["cap"].finished.wait()
    rec.stop()
    meta = f2.read_meta()
    assert meta["attendees"] == ["Ana", "Ben"]
    assert meta["vocabulary"] == []  # names are not terms unless the dialog added them


# -- choices, target, undo --------------------------------------------------------------
LIVE = [("mic", 500, 1500, "You", "first line here."),
        ("system", 5000, 9000, "SPEAKER_01", "second line, from the call."),
        ("system", 10_000, 12_000, "SPEAKER_02", "third one."),
        ("mic", 13_000, 13_500, "You", "mine."),
        ("system", 14_000, 16_000, "SPEAKER_01", "fourth one.")]
NAMES = ["Ana", "Ben", "Cy"]


def model(tmp_path, tag_events=(), lines=LIVE):
    return TranscriptModel.load(make_meeting(tmp_path, lines, status=mt.STATUS_RECORDING, tags=list(tag_events)))


def test_pick_names_lists_unused_attendees_first_then_names_in_use(tmp_path):
    m = model(tmp_path, [tags.tag_speaker_event("SPEAKER_01", "Ben"), tags.tag_speaker_event("SPEAKER_02", "Zed")])
    assert al.pick_names(m, ["Ana", "Ben", "Cy"]) == ["Ana", "Cy", "Ben", "Zed"]
    assert al.pick_names(m, ["Ana", "ben", "Cy"], exclude="Zed") == ["Ana", "Cy", "Ben"]
    assert al.pick_names(model(tmp_path / "x", []), []) == []


def test_key_target_is_the_latest_unnamed_call_line_else_the_latest_call_line(tmp_path):
    m = model(tmp_path)
    assert al.key_target(m).label == "SPEAKER_01" and al.key_target(m).start_ms == 14_000
    m = model(tmp_path / "a", [tags.tag_speaker_event("SPEAKER_01", "Ben")])
    assert al.key_target(m).label == "SPEAKER_02"  # the latest line is Ben's; SPEAKER_02 is still unnamed
    m = model(tmp_path / "b", [tags.tag_speaker_event("SPEAKER_01", "Ben"), tags.tag_speaker_event("SPEAKER_02", "Cy")])
    assert al.key_target(m).start_ms == 14_000 and al.key_target(m).display == "Ben"
    assert al.key_target(model(tmp_path / "c", lines=[("mic", 0, 900, "You", "alone.")])) is None
    assert al.key_target(TranscriptModel()) is None


def test_digits_and_focus_rules():
    assert al.digit_of("3") == 3 and al.digit_of("0") is None and al.digit_of("a") is None and al.digit_of("") is None
    assert al.digit_of("3", al.CONTROL_MASK) is None and al.digit_of("3", al.ALT_MASK) is None
    assert al.digit_of("3", 0x8) == 3  # NumLock is not a modifier we care about
    for cls in ("Entry", "TEntry", "TCombobox", "Spinbox"):
        assert al.focus_blocks_keys(cls)
    assert al.focus_blocks_keys("Text", "normal") and not al.focus_blocks_keys("Text", "disabled")
    assert not al.focus_blocks_keys("Toplevel") and not al.focus_blocks_keys("TButton")


def test_strip_text_numbers_the_names_marks_used_ones_and_names_the_target(tmp_path):
    m = model(tmp_path, [tags.tag_speaker_event("SPEAKER_02", "Ben")])
    text = al.strip_text(m, ["Ana", "Ben", "Cy"])
    assert text == "Keys 1–3 name SPEAKER_01:  1 Ana   2 Ben ✓   3 Cy"
    assert al.strip_text(m, []) == ""
    assert al.strip_text(TranscriptModel(), ["Ana"]).startswith("Keys 1–1 name the next new voice:")
    ten = [f"P{i}" for i in range(1, 12)]
    assert al.strip_text(m, ten).endswith("+2 more in the name list")


def test_capture_undo_remembers_the_tags_it_will_replace(tmp_path):
    ev = [tags.tag_speaker_event("SPEAKER_01", "Ben"),
          tags.tag_line_event("system", 14_000, 16_000, "Cy", seq=5)]
    m = model(tmp_path, ev)
    u = al.capture_undo(m, "Ben")
    assert u.labels == (("SPEAKER_01", "Ben"),) and u.lines == ()
    u2 = al.capture_undo(m, "SPEAKER_02")
    assert u2.labels == (("SPEAKER_02", None),)
    assert al.confirmation_text("SPEAKER_03", "Ana") == "SPEAKER_03 is now Ana"


# -- who is this -------------------------------------------------------------------------
def test_who_prompter_hands_out_new_unnamed_call_voices_one_at_a_time(tmp_path):
    m = model(tmp_path)
    p = al.WhoPrompter()
    first = p.update(m, True, NAMES)
    assert first.label == "SPEAKER_01" and first.snippet == "second line, from the call."
    assert p.update(m, True, NAMES) is first and [v.label for v in p.queue] == ["SPEAKER_02"]  # one at a time
    p.dismiss()
    assert p.update(m, True, NAMES).label == "SPEAKER_02"
    p.dismiss()
    assert p.update(m, True, NAMES) is None
    assert p.update(m, True, NAMES) is None  # dismissed voices are not asked again


def test_who_prompter_skips_voices_named_in_the_meantime_and_ignores_you_and_final(tmp_path):
    m = model(tmp_path)
    p = al.WhoPrompter()
    m.apply_tags([tags.tag_speaker_event("SPEAKER_01", "Ben")])
    assert p.update(m, True, NAMES).label == "SPEAKER_02"  # SPEAKER_01 was named before it was asked about
    m.apply_tags([tags.tag_speaker_event("SPEAKER_02", "Cy")])
    assert p.update(m, True, NAMES) is None  # the shown one was named another way: gone
    assert al.WhoPrompter().update(m, False, NAMES) is None
    q = al.WhoPrompter()
    assert q.update(model(tmp_path / "y", lines=[("mic", 0, 900, "You", "alone.")]), True, NAMES) is None


def test_without_attendees_no_voice_is_asked_about_but_its_chip_still_asks(tmp_path):
    m = model(tmp_path)
    p = al.WhoPrompter()
    assert p.update(m, True, []) is None and p.queue == [] and p.current is None
    assert [c.display for c in logic.speaker_chips(m) if c.unnamed] == ["SPEAKER_01", "SPEAKER_02"]
    assert p.update(m, True, NAMES).label == "SPEAKER_01"  # with attendees the same meeting is asked about


def test_prompt_view_offers_unused_attendees_with_their_numbers(tmp_path):
    m = model(tmp_path, [tags.tag_speaker_event("SPEAKER_02", "Ben")])
    v = al.NewVoice("SPEAKER_01", "SPEAKER_01", "SPEAKER_01", "Can everyone hear me?")
    view = al.prompt_view(v, ["Ana", "Ben", "Cy"], al.used_names(m))
    assert view.text == "New voice SPEAKER_01 (“Can everyone hear me?”). Who is this?"
    assert [(b.label, b.action) for b in view.buttons] == [("1 Ana", "pick:1"), ("3 Cy", "pick:3"),
                                                          ("Other…", "other"), ("Not now", "dismiss")]
    bare = al.prompt_view(al.NewVoice("S", "S", "S", ""), [], set())
    assert bare.text == "New voice S. Who is this?" and [b.action for b in bare.buttons] == ["other", "dismiss"]
    many = al.prompt_view(v, [f"P{i}" for i in range(1, 10)], set())
    assert len(many.buttons) == al.PROMPT_BUTTONS + 2
    assert [b.action for b in al.undo_view("x").buttons] == ["undo", "dismiss"]
