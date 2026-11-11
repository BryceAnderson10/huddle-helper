"""WP9d: the Storage panel (L32), "Add to vocabulary…" (L37), the vocabulary file (L38), short speakers (L40)."""

import gc
import os
import shutil
import time
import tkinter as tk
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest

from huddle_helper import config, housekeeping, library, meeting as mt, storage, vocab
from huddle_helper.gui import logic
from huddle_helper.transcript_model import TranscriptModel

from .helpers import STARTED_AT, make_meeting

KB = 1024
DAY = 86400.0
LINES = [("mic", 0, 1000, "You", "hello."), ("system", 2000, 3000, "SPEAKER_01", "hi.")]


@pytest.fixture()
def root():
    try:
        r = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display for Tk: {exc}")
    r.withdraw()
    yield r
    r.destroy()
    del r
    gc.collect()


def meeting_with_audio(base, title, audio_kb, *, age_days=None, hours=0, final=True):
    """A finished meeting with ``audio_kb`` KB of audio; DONE is made ``age_days`` old."""
    f = make_meeting(base, LINES, final=LINES if final else None, title=title,
                     started_at=STARTED_AT + timedelta(hours=hours))
    if audio_kb:
        f.audio_dir.mkdir(exist_ok=True)
        (f.audio_dir / "mic.flac").write_bytes(b"x" * (audio_kb * KB))
    if age_days is not None and f.done_path.exists():
        t = time.time() - age_days * DAY
        os.utime(f.done_path, (t, t))
    return f


def measured_scan(base):
    cache = library.ScanCache()
    library.scan(base, cache)
    assert cache.wait_for_sizes()
    return library.scan(base, cache)


def folder_bytes(path):
    return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)


# -- L32: sizes ---------------------------------------------------------------------------------
def test_usage_splits_audio_from_the_rest_and_lists_the_largest_audio_first(tmp_path):
    meeting_with_audio(tmp_path, "small", 10, hours=0)
    meeting_with_audio(tmp_path, "big", 300, hours=1)
    meeting_with_audio(tmp_path, "none", 0, hours=2)
    meeting_with_audio(tmp_path, "mid", 50, hours=3)
    infos = measured_scan(tmp_path)
    u = storage.usage(infos)
    assert u.measured and u.meetings == 4 and u.with_audio == 3
    assert u.audio_bytes == 360 * KB
    assert u.total_bytes == folder_bytes(tmp_path)  # same as an independent os.walk
    assert u.other_bytes == u.total_bytes - u.audio_bytes > 0
    assert [i.title for i in u.largest] == ["big", "mid", "small"]
    assert len(storage.usage(infos, top=2).largest) == 2
    head, split = storage.usage_lines(u)
    assert head.startswith("4 meetings, ") and "Kept audio 360 KB (3 meetings)" in split and "transcripts and the rest" in split


def test_usage_before_the_sizes_are_measured_says_so(tmp_path):
    meeting_with_audio(tmp_path, "a", 10)
    infos = [replace(i, audio_bytes=None, total_bytes=None) for i in library.scan(tmp_path)]
    u = storage.usage(infos)
    assert not u.measured
    assert storage.usage_lines(u) == ("1 meeting · measuring sizes…", "")
    assert storage.usage_lines(storage.usage([])) == ("No meetings yet.", "")


def test_setting_text():
    assert storage.setting_text(0) == "Automatic deletion of kept audio is off."
    assert "30 days" in storage.setting_text(30) and "1 day " in storage.setting_text(1)


# -- L32: the previewed set -----------------------------------------------------------------------
def test_the_plan_is_finished_meetings_with_audio_older_than_n_days_oldest_first(tmp_path):
    meeting_with_audio(tmp_path, "old-40", 100, age_days=40, hours=0)
    meeting_with_audio(tmp_path, "old-100", 200, age_days=100, hours=1)
    meeting_with_audio(tmp_path, "fresh-5", 50, age_days=5, hours=2)
    meeting_with_audio(tmp_path, "old-no-audio", 0, age_days=90, hours=3)
    meeting_with_audio(tmp_path, "old-unfinished", 70, final=False, hours=4)  # no DONE: not finished
    infos = measured_scan(tmp_path)
    plan = storage.plan_purge(infos, 30)
    assert [i.title for i in plan.items] == ["old-100", "old-40"] and plan.complete
    assert plan.total_bytes == 300 * KB and plan.ids == [i.id for i in plan.items]
    assert round(plan.items[0].age_days) == 100
    assert storage.purge_summary(plan) == "2 meetings, 300 KB: kept audio older than 30 days."
    assert [i.title for i in storage.plan_purge(infos, 60).items] == ["old-100"]
    assert storage.plan_purge(infos, 200).empty
    assert storage.purge_summary(storage.plan_purge(infos, 200)) == "No kept audio is older than 200 days."
    assert storage.plan_purge(infos, 0).empty and storage.plan_purge(infos, -3).empty  # 0 = off, as auto-purge


def test_busy_meetings_are_never_in_the_plan(tmp_path):
    a = meeting_with_audio(tmp_path, "recording-now", 10, age_days=50, hours=0)
    b = meeting_with_audio(tmp_path, "queued", 10, age_days=50, hours=1)
    c = meeting_with_audio(tmp_path, "processing", 10, age_days=50, hours=2)
    d = meeting_with_audio(tmp_path, "retry-waiting", 10, age_days=50, hours=3)
    meeting_with_audio(tmp_path, "idle", 10, age_days=50, hours=4)
    live = library.LiveState(recording=a.meeting_id, queued=frozenset({b.meeting_id}), finalizing=c.meeting_id,
                             waiting=frozenset({d.meeting_id}))
    plan = storage.plan_purge(measured_scan(tmp_path), 30, live)
    assert [i.title for i in plan.items] == ["idle"]


def test_a_plan_with_an_unmeasured_size_is_not_complete(tmp_path):
    meeting_with_audio(tmp_path, "a", 10, age_days=50)
    infos = [replace(i, audio_bytes=None, total_bytes=None) for i in library.scan(tmp_path)]
    plan = storage.plan_purge(infos, 30)
    assert len(plan.items) == 1 and not plan.complete
    assert storage.purge_summary(plan).startswith("1 meeting, at least ")


def test_the_plan_selects_what_automatic_purge_would(tmp_path):
    """Same rule as housekeeping.purge_old_audio, so the preview is what auto-purge itself would remove."""
    for k, age in enumerate((2, 29, 31, 45, 400)):
        meeting_with_audio(tmp_path, f"m{k}", 5, age_days=age, hours=k)
    meeting_with_audio(tmp_path, "unfinished", 5, final=False, hours=9)
    planned = set(storage.plan_purge(measured_scan(tmp_path), 30).ids)
    purged = set(housekeeping.purge_old_audio(tmp_path, 30))
    assert planned == purged and len(planned) == 3


def test_deleting_the_previewed_set_removes_only_that_audio(tmp_path):
    keep_new = meeting_with_audio(tmp_path, "new", 20, age_days=1, hours=0)
    old1 = meeting_with_audio(tmp_path, "old1", 40, age_days=60, hours=1)
    old2 = meeting_with_audio(tmp_path, "old2", 60, age_days=90, hours=2)
    before = {f.meeting_id: folder_bytes(f.path) for f in (keep_new, old1, old2)}
    meta_before = {f.meeting_id: f.meta_path.stat().st_size for f in (keep_new, old1, old2)}
    transcripts = {f.meeting_id: f.final_path.read_bytes() for f in (keep_new, old1, old2)}
    plan = storage.plan_purge(measured_scan(tmp_path), 30)
    assert set(plan.ids) == {old1.meeting_id, old2.meeting_id}
    binned = []

    def fake_recycler(paths):  # stands in for the Recycle Bin: nothing of the real one is touched
        for p in paths:
            binned.append(p)
            shutil.rmtree(p)

    for mid in plan.ids:
        assert library.delete_audio(tmp_path, mid, is_busy=lambda _m: False, recycler=fake_recycler,
                                    has_bin=lambda _p: True) == "recycled"
    assert sorted(p.parent.name for p in binned) == sorted(plan.ids) and all(p.name == "audio" for p in binned)
    assert keep_new.audio_dir.is_dir() and not old1.audio_dir.exists() and not old2.audio_dir.exists()
    for f in (keep_new, old1, old2):
        assert f.final_path.read_bytes() == transcripts[f.meeting_id] and f.meta_path.exists() and f.done_path.exists()
    assert before[keep_new.meeting_id] == folder_bytes(keep_new.path)
    for f, kb in ((old1, 40), (old2, 60)):  # everything but the audio is still there (meta.json grows a little)
        assert folder_bytes(f.path) == before[f.meeting_id] - kb * KB + (f.meta_path.stat().st_size - meta_before[f.meeting_id])
    assert old1.read_meta()["audio_kept"] is False
    assert storage.plan_purge(measured_scan(tmp_path), 30).empty  # nothing left to offer


def test_library_info_carries_the_done_time(tmp_path):
    f = meeting_with_audio(tmp_path, "a", 1, age_days=10)
    (info,) = library.scan(tmp_path)
    assert info.done_at == pytest.approx(f.done_path.stat().st_mtime, abs=0.01)
    g = meeting_with_audio(tmp_path, "b", 1, final=False, hours=1)
    assert [i.done_at for i in library.scan(tmp_path) if i.id == g.meeting_id] == [None]


# -- L32: the panel -----------------------------------------------------------------------------------
class FakeApp:
    def __init__(self, days=0):
        self.settings = config.Settings(auto_purge_days=days)
        self.saved = []

    def live_state(self):
        return library.LiveState()

    def update_settings(self, s):
        self.saved.append(s)
        self.settings = s


def make_panel(root, tmp_path, app=None, recycle=True):
    from huddle_helper.gui import storage_dialog as sd

    app = app or FakeApp()
    infos = measured_scan(tmp_path)
    calls = []

    def delete_ids(ids, permanent, done):
        calls.append((list(ids), permanent))
        res = library.DeleteResult()
        for mid in ids:
            how = library.delete_audio(tmp_path, mid, is_busy=lambda _m: False, permanent=permanent,
                                       recycler=lambda ps: [shutil.rmtree(p) for p in ps], has_bin=lambda _p: recycle)
            res.done.append(mid)
            res.how = how
        done(res)

    dlg = sd.StorageDialog(root, app, infos=lambda: infos, delete_ids=delete_ids, meetings_dir=lambda: tmp_path)
    return dlg, calls, app


def test_panel_shows_sizes_largest_and_the_setting(root, tmp_path):
    meeting_with_audio(tmp_path, "big", 300, age_days=3, hours=0)
    meeting_with_audio(tmp_path, "small", 10, age_days=3, hours=1)
    dlg, _, _ = make_panel(root, tmp_path, FakeApp(days=14))
    assert dlg.headline.cget("text").startswith("2 meetings, ")
    assert "Kept audio 310 KB (2 meetings)" in dlg.split.cget("text")
    assert [dlg.largest.set(i, "Meeting") for i in dlg.largest.get_children()] == ["big", "small"]
    assert dlg.largest.set(dlg.largest.get_children()[0], "Audio") == "300 KB"
    assert dlg.auto_days.get() == "14" and "14 days" in dlg.auto_note.cget("text")
    assert dlg.days.get() == "14"  # "older than" starts at the auto-delete setting
    dlg.close()


def test_panel_saves_the_auto_delete_setting_like_settings_does(root, tmp_path):
    meeting_with_audio(tmp_path, "a", 10)
    dlg, _, app = make_panel(root, tmp_path)
    assert dlg.days.get() == str(storage.DEFAULT_DAYS)
    dlg.auto_days.set("abc")
    assert "whole number" in dlg.save_auto() and app.saved == []
    dlg.auto_days.set("45")
    assert dlg.save_auto().startswith("Saved.") and app.settings.auto_purge_days == 45
    assert "45 days" in dlg.auto_note.cget("text")
    dlg.close()


def test_preview_lists_exactly_what_is_deleted_and_the_delete_runs_that_set(root, tmp_path):
    meeting_with_audio(tmp_path, "old-a", 40, age_days=60, hours=0)
    meeting_with_audio(tmp_path, "old-b", 60, age_days=90, hours=1)
    fresh = meeting_with_audio(tmp_path, "fresh", 20, age_days=2, hours=2)
    dlg, calls, _ = make_panel(root, tmp_path)
    assert dlg.plan is None and not dlg.pbox.winfo_ismapped()
    dlg.days.set("30")
    plan = dlg.preview()
    assert dlg.psummary.cget("text") == "2 meetings, 100 KB: kept audio older than 30 days."
    shown = [dlg.ptree.set(i, "Meeting") for i in dlg.ptree.get_children()]
    assert shown == ["old-b", "old-a"] and [i.title for i in plan.items] == shown
    assert dlg.run_btn.cget("text") == "Move audio to Recycle Bin" and str(dlg.run_btn.cget("state")) == "normal"
    # changing N drops the preview: the old list must not be deletable under a new number
    dlg.days.set("200")
    assert dlg.plan is None
    dlg.run()
    assert calls == []
    dlg.days.set("30")
    plan = dlg.preview()
    dlg.run()
    assert calls == [(plan.ids, False)]  # exactly the previewed ids, to the Recycle Bin
    assert fresh.audio_dir.is_dir()
    assert dlg.plan is None and "Moved the audio of 2 meetings to the Recycle Bin." in dlg.result.cget("text")
    dlg.close()


def test_permanent_choice_changes_the_wording_and_is_passed_on(root, tmp_path):
    meeting_with_audio(tmp_path, "old", 40, age_days=60)
    dlg, calls, _ = make_panel(root, tmp_path)
    dlg.preview()
    dlg.permanent.set(True)
    dlg._rebuild_preview()
    assert dlg.run_btn.cget("text") == "Delete audio permanently"
    dlg.run()
    assert calls[0][1] is True and "Permanently deleted" in dlg.result.cget("text")
    dlg.close()


def test_nothing_to_delete_disables_the_button_and_says_so(root, tmp_path):
    meeting_with_audio(tmp_path, "fresh", 40, age_days=1)
    dlg, calls, _ = make_panel(root, tmp_path)
    dlg.preview()
    assert dlg.psummary.cget("text") == "No kept audio is older than 30 days."
    assert str(dlg.run_btn.cget("state")) == "disabled"
    dlg.run()
    assert calls == []
    dlg.days.set("0")
    assert dlg.preview() is None  # days must be 1 or more
    dlg.close()


def test_an_incomplete_plan_cannot_be_run(root, tmp_path):
    from huddle_helper.gui import storage_dialog as sd

    meeting_with_audio(tmp_path, "old", 40, age_days=60)
    infos = [replace(i, audio_bytes=None, total_bytes=None) for i in library.scan(tmp_path)]
    ran = []
    dlg = sd.StorageDialog(root, FakeApp(), infos=lambda: infos, delete_ids=lambda *a: ran.append(a),
                           meetings_dir=lambda: tmp_path)
    dlg.preview()
    assert str(dlg.run_btn.cget("state")) == "disabled"
    dlg.run()
    assert ran == []
    dlg.close()


# -- L37: add to vocabulary ---------------------------------------------------------------------------------
def test_selected_words_are_cleaned_into_a_term():
    assert vocab.clean_term("Quodium.") == "Quodium"
    assert vocab.clean_term(' "QDM", ') == "QDM"
    assert vocab.clean_term("code\n  freeze") == "code freeze"
    assert vocab.clean_term(".NET") == ".NET" and vocab.clean_term("C#") == "C#" and vocab.clean_term("(ETL)") == "ETL"
    assert vocab.clean_term("...") == "" and vocab.clean_term(None) == ""


def test_term_checks_empty_comment_long_duplicate_and_fine():
    have = ["QDM", "zone code"]
    assert not vocab.check_new_term("  ", have).ok
    assert "comments" in vocab.check_new_term("#todo", have).message
    assert "Too long" in vocab.check_new_term("x" * 81, have).message
    dup = vocab.check_new_term("qdm", have)
    assert not dup.ok and "already" in dup.message and "QDM" in dup.message  # ignoring case
    ok = vocab.check_new_term(" Zentrix. ", have)
    assert ok.ok and ok.term == "Zentrix" and ok.level == "ok" and ok.fits
    assert vocab.add_term(have, "Zentrix") == ["QDM", "zone code", "Zentrix"]
    assert vocab.add_term(have, "qdm") is None and vocab.add_term(have, "") is None


def test_a_term_that_would_not_fit_the_budget_is_flagged_but_can_be_added():
    have = [f"term{i:03d}" for i in range(200)]
    plan = lambda terms: vocab.plan([], terms)  # noqa: E731
    assert plan(have).dropped  # the list is already over the limit
    check = vocab.check_new_term("Brandnew", have, plan)
    assert check.ok and not check.fits and check.level == "warn" and "left out" in check.message
    assert vocab.check_new_term("Brandnew", ["a", "b"], plan).fits


def test_controller_adds_the_term_through_update_settings_and_the_recorder(tmp_path):
    from huddle_helper.app import AppController

    app = AppController()
    app.settings = config.Settings(vocabulary=["QDM"])
    pushed = []
    app._recorder = SimpleNamespace(set_vocabulary=lambda settings_terms=None, **kw: pushed.append(settings_terms),
                                    meeting_terms=[])
    app.state.phase = "recording"
    assert app.add_vocabulary_term("Quodium.").ok is True
    assert app.settings.vocabulary == ["QDM", "Quodium"]
    assert pushed == [["QDM", "Quodium"]]  # handed to the running recorder: applies from the next utterance
    assert config.load().vocabulary == ["QDM", "Quodium"]  # and saved
    assert app.add_vocabulary_term("quodium").ok is False and app.settings.vocabulary == ["QDM", "Quodium"]
    assert len(pushed) == 1  # a duplicate changes nothing


def test_the_line_menu_offers_add_to_vocabulary_and_prefills_the_selected_words(root):
    from huddle_helper.gui.transcript_view import TranscriptView

    view = TranscriptView(root)
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-01-15T09:30:00+00:00"})
    view.apply(m.apply_transcript([{"type": "segment", "seq": 1, "start_ms": 1000, "end_ms": 4000, "source": "system",
                                    "speaker": "SPEAKER_00", "text": "We keep the data in Quodium for now.",
                                    "confidence": 0.9}]), m)
    got = []
    view.on_add_vocab = lambda row, words: got.append((row.text, words))
    row = view._rows[0]
    t = view.text
    # no selection: nothing to guess, the dialog opens empty
    menu = view.build_menu(row)
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) != "separator"]
    assert "Add to vocabulary…" in labels
    assert view.selection_in_row(row) == ""
    # select "Quodium" inside the text column
    body = t.tag_nextrange("body", "1.0", "end")
    start = t.search("Quodium", body[0])
    t.tag_add("sel", start, f"{start}+7c")
    assert view.selection_in_row(row) == "Quodium"
    # a selection that also covers the time and speaker columns is cut back to the line's own text
    t.tag_remove("sel", "1.0", "end")
    t.tag_add("sel", "1.0", f"{start}+7c")
    assert view.selection_in_row(row) == "We keep the data in Quodium"
    menu = view.build_menu(row)
    idx = next(i for i in range(menu.index("end") + 1) if menu.type(i) == "command" and
               menu.entrycget(i, "label") == "Add to vocabulary…")
    menu.invoke(idx)
    assert got == [(row.text, "We keep the data in Quodium")]


def test_vocab_term_dialog_checks_as_you_type_and_adds_the_cleaned_term(root):
    from huddle_helper.gui.dialogs import VocabTermDialog

    done = []
    existing = ["QDM"]
    dlg = VocabTermDialog(root, "Quodium.", existing=lambda: existing, plan=lambda terms: vocab.plan([], terms),
                          on_done=done.append)
    assert dlg.term.get() == "Quodium" and str(dlg.add_btn.cget("state")) == "normal"
    dlg.term.set("qdm")
    assert str(dlg.add_btn.cget("state")) == "disabled" and "already" in dlg.note.cget("text")
    dlg.accept()
    assert done == [] and dlg.win.winfo_exists()
    dlg.term.set("")
    assert str(dlg.add_btn.cget("state")) == "disabled" and dlg.note.cget("text") == ""
    dlg.term.set("  QDMS. ")
    dlg.accept()
    assert done == ["QDMS"]
    blank = VocabTermDialog(root, "", existing=lambda: existing, plan=None, on_done=done.append)
    assert blank.term.get() == "" and str(blank.add_btn.cget("state")) == "disabled"
    blank.win.destroy()


# -- L38: the vocabulary file --------------------------------------------------------------------------------
def test_read_tolerates_bom_crlf_blank_lines_comments_and_duplicates(tmp_path):
    p = tmp_path / "v.txt"
    p.write_bytes("﻿# team list\r\nQDM\r\n\r\n  ETL  \r\nqdm\r\n#skip\r\nzone code\r\n".encode("utf-8"))
    assert vocab.read_terms_file(p) == ["QDM", "ETL", "zone code"]
    p.write_bytes("Café\nnaïve\n".encode("cp1252"))  # saved by an old ANSI editor
    assert vocab.read_terms_file(p) == ["Café", "naïve"]
    p.write_bytes(b"")
    assert vocab.read_terms_file(p) == []


def test_read_refuses_a_missing_binary_or_huge_file(tmp_path):
    with pytest.raises(vocab.VocabFileError, match="Could not read"):
        vocab.read_terms_file(tmp_path / "nope.txt")
    (tmp_path / "b.bin").write_bytes(b"MZ\x00\x01\x02")
    with pytest.raises(vocab.VocabFileError, match="text file"):
        vocab.read_terms_file(tmp_path / "b.bin")
    (tmp_path / "big.txt").write_bytes(b"a\n" * (vocab.MAX_FILE_BYTES // 2 + 10))
    with pytest.raises(vocab.VocabFileError, match="much smaller"):
        vocab.read_terms_file(tmp_path / "big.txt")


def test_write_is_utf8_one_term_per_line_and_round_trips(tmp_path):
    terms = ["QDM", "Zentrix", "naïve", "日本語", "zone code", ".NET"]
    p = tmp_path / "out.txt"
    assert vocab.write_terms_file(p, terms) == 6
    raw = p.read_bytes()
    assert raw.decode("utf-8") == "QDM\nZentrix\nnaïve\n日本語\nzone code\n.NET\n" and b"\r" not in raw
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert vocab.read_terms_file(p) == terms
    assert vocab.write_terms_file(p, ["a", "A", "", "b"]) == 2  # same dedupe as the box
    with pytest.raises(vocab.VocabFileError, match="Could not save"):
        vocab.write_terms_file(tmp_path / "missing-dir" / "x.txt", terms)


def test_merge_keeps_mine_and_skips_duplicates_replace_swaps_the_list():
    r = vocab.merge_import(["QDM", "ETL"], ["etl", "KoVa", "QDM", "FMS"])
    assert r.terms == ["QDM", "ETL", "KoVa", "FMS"] and r.added == 2 and r.duplicates == 2
    assert r.message == "Added 2 terms from the file. 2 already in the list."
    r = vocab.merge_import(["QDM"], ["KoVa"], replace=True)
    assert r.terms == ["KoVa"] and r.replaced and r.message == "Replaced the list with 1 term from the file."
    assert vocab.merge_import([], ["a"]).message == "Added 1 term from the file."


def settings_dialog(root, terms):
    from huddle_helper.ui import SettingsDialog

    saved = []
    dlg = SettingsDialog(root, config.Settings(vocabulary=list(terms)), {"inputs": [], "outputs": []}, saved.append,
                         plan=lambda t, m=(): vocab.plan(m, t))
    return dlg, saved


def test_settings_vocabulary_tab_imports_exports_and_round_trips(root, tmp_path):
    dlg, saved = settings_dialog(root, ["QDM", "ETL"])
    src = tmp_path / "team.txt"
    src.write_bytes("﻿KoVa\r\n# note\r\netl\r\nFMS\r\n".encode("utf-8"))
    msg = dlg.import_terms(src)  # add
    assert msg.startswith("Added 2 terms from the file. 1 already in the list.")
    assert dlg.vocab.terms() == ["QDM", "ETL", "KoVa", "FMS"]
    assert "terms used" in dlg.vocab.count.cget("text")  # the count line follows
    out = tmp_path / "export.txt"
    assert dlg.export_terms(out).startswith("Saved 4 terms")
    assert out.read_text(encoding="utf-8") == "QDM\nETL\nKoVa\nFMS\n"
    dlg.import_terms(tmp_path / "team.txt", replace=True)
    assert dlg.vocab.terms() == ["KoVa", "etl", "FMS"]  # the file's spelling
    assert saved == []  # nothing is saved until Save
    dlg2, _ = settings_dialog(root, [])
    assert dlg2.export_terms(tmp_path / "empty.txt") == "There are no terms to export." and not (tmp_path / "empty.txt").exists()
    (tmp_path / "comments.txt").write_text("# only a comment\n\n", encoding="utf-8")
    assert "no terms" in dlg2.import_terms(tmp_path / "comments.txt")
    assert "Could not read" in dlg2.import_terms(tmp_path / "gone.txt")
    dlg2.win.destroy()
    dlg.import_terms(out, replace=True)
    dlg._save()
    assert saved[0].vocabulary == ["QDM", "ETL", "KoVa", "FMS"]


# -- L40: short speakers ----------------------------------------------------------------------------------
def seg(seq, source, start, end, speaker, text="x."):
    return {"type": "segment", "seq": seq, "start_ms": start, "end_ms": end, "source": source,
            "speaker": speaker, "text": text, "confidence": 0.9}


def final_model(lines, speakers=None):
    m = TranscriptModel()
    m.set_meta({"started_at": "2026-01-15T09:30:00+00:00"})
    m.apply_transcript(lines)
    m.switch_to_final(lines, speakers or {})
    return m


def test_a_speaker_under_30_seconds_is_short_in_the_final_view_only():
    lines = [seg(1, "system", 0, 120_000, "SPEAKER_00"), seg(2, "system", 130_000, 158_000, "SPEAKER_01"),
             seg(3, "system", 160_000, 173_000, "SPEAKER_02"), seg(4, "mic", 200_000, 300_000, "You")]
    m = final_model(lines)
    assert m.final and logic.short_speakers(m) == frozenset({"SPEAKER_01", "SPEAKER_02"})  # 28 s and 13 s
    chips = {c.person_key: c for c in logic.speaker_chips(m)}
    assert chips["SPEAKER_01"].short and chips["SPEAKER_01"].text == "SPEAKER_01 · short · Who is this?"
    assert not chips["SPEAKER_00"].short and chips["SPEAKER_00"].text == "SPEAKER_00 · Who is this?"
    assert not chips["You"].short and chips["You"].text == "You"
    live = TranscriptModel()
    live.apply_transcript(lines)
    assert not live.final and logic.short_speakers(live) == frozenset()
    assert not any(c.short for c in logic.speaker_chips(live))


def test_exactly_30_seconds_is_not_short_and_lines_do_not_matter():
    lines = [seg(1, "system", 0, 100_000, "SPEAKER_00"), seg(2, "system", 100_000, 130_000, "SPEAKER_01"),
             seg(3, "system", 130_000, 131_000, "SPEAKER_02"), seg(4, "system", 131_000, 132_000, "SPEAKER_02")]
    m = final_model(lines)
    assert logic.short_speakers(m) == frozenset({"SPEAKER_02"})  # 2 s over 2 lines; 30.0 s over 1 line is not short
    one_long = final_model([seg(1, "system", 0, 100_000, "SPEAKER_00"), seg(2, "system", 100_000, 140_000, "SPEAKER_01")])
    assert logic.short_speakers(one_long) == frozenset()


def test_named_speakers_are_still_marked_and_two_labels_with_one_name_add_up():
    lines = [seg(1, "system", 0, 120_000, "SPEAKER_00"), seg(2, "system", 130_000, 150_000, "SPEAKER_01"),
             seg(3, "system", 160_000, 180_000, "SPEAKER_02")]
    m = final_model(lines, {"SPEAKER_01": "Dana"})
    assert logic.short_speakers(m) == frozenset({"Dana", "SPEAKER_02"})
    chip = next(c for c in logic.speaker_chips(m) if c.person_key == "Dana")
    assert chip.short and chip.text == "Dana · short" and not chip.unnamed
    merged = final_model(lines, {"SPEAKER_01": "Dana", "SPEAKER_02": "Dana"})  # 20 s + 20 s = 40 s: not short
    assert logic.short_speakers(merged) == frozenset()


def test_when_everyone_is_short_nobody_is_marked():
    m = final_model([seg(1, "system", 0, 10_000, "SPEAKER_00"), seg(2, "system", 10_000, 25_000, "SPEAKER_01")])
    assert logic.short_speakers(m) == frozenset() and not any(c.short for c in logic.speaker_chips(m))


def test_the_view_dims_and_marks_short_speakers_and_the_chip_bar_follows(root):
    from huddle_helper.gui import theme
    from huddle_helper.gui.speaker_bar import SpeakerBar
    from huddle_helper.gui.transcript_view import TranscriptView

    lines = [seg(1, "system", 0, 120_000, "SPEAKER_00", "long talker."), seg(2, "system", 130_000, 140_000, "SPEAKER_01",
                                                                           "brief remark.")]
    m = final_model(lines, {"SPEAKER_01": "Ben"})
    view = TranscriptView(root)
    view.set_short(logic.short_speakers(m))
    from huddle_helper.transcript_model import Reset

    view.apply([Reset()], m)
    text = view.plain_text().splitlines()
    assert "\tBen · short\t" in text[1] and "· short" not in text[0]
    t = view.text
    assert t.tag_cget("spk:Ben", "foreground") == theme.dim(view.color_of(view._rows[1]))
    assert t.tag_cget("spk:SPEAKER_00", "foreground") == view.color_of(view._rows[0])
    ranges = t.tag_ranges("short-body")
    assert len(ranges) == 2 and t.get(ranges[0], ranges[1]).strip() == "brief remark."
    assert view.row_count == 2  # shown, never hidden
    # naming the long talker "Ben" too merges the two: nobody is short any more, and the rows are redrawn
    m2 = final_model(lines, {"SPEAKER_00": "Ben", "SPEAKER_01": "Ben"})
    view.set_short(logic.short_speakers(m2))
    assert "· short" not in view.plain_text() and t.tag_ranges("short-body") == ()
    bar = SpeakerBar(root)
    bar.set_chips(logic.speaker_chips(m), lambda key: "#123456")
    assert "Ben · short" in bar.get("1.0", "end") and "SPEAKER_00 · Who is this?" in bar.get("1.0", "end")
    bar.set_chips(logic.speaker_chips(m2), lambda key: "#123456")
    assert "short" not in bar.get("1.0", "end")  # the signature includes the mark, so the bar redraws


def test_dim_moves_a_colour_towards_white():
    from huddle_helper.gui import theme

    assert theme.dim("#000000", 0.5) == "#808080" and theme.dim("#ffffff") == "#ffffff"
    assert theme.dim("#1d4ed8", 0.0) == "#1d4ed8"


def test_totals_count_audio_that_was_just_deleted_as_gone_before_it_is_measured_again(tmp_path):
    f = meeting_with_audio(tmp_path, "a", 100, age_days=50)
    meeting_with_audio(tmp_path, "b", 20, age_days=50, hours=1)
    cache = library.ScanCache()
    library.scan(tmp_path, cache)
    cache.wait_for_sizes()
    before = library.totals(library.scan(tmp_path, cache))
    assert before.audio_bytes == 120 * KB
    shutil.rmtree(f.audio_dir)  # a scratch folder of this test
    infos = library.scan(tmp_path, cache)  # the cache still returns the old sizes for "a" until the worker is done
    stale = next(i for i in infos if i.id == f.meeting_id)
    assert not stale.has_audio
    t = library.totals(infos)
    assert t.audio_bytes == 20 * KB and t.total_bytes == before.total_bytes - 100 * KB


def test_you_is_never_marked_short():
    lines = [seg(1, "system", 0, 120_000, "SPEAKER_00"), seg(2, "system", 130_000, 150_000, "SPEAKER_01"),
             seg(3, "mic", 160_000, 168_000, "You")]
    assert logic.short_speakers(final_model(lines)) == frozenset({"SPEAKER_01"})
