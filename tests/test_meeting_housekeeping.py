import os
import time

from huddle_helper.diarize import profile
from huddle_helper import config, housekeeping, meeting as mt
from huddle_helper.writer import JsonlWriter, meeting_start_event, read_events


def test_slug_and_ids():
    assert mt.slugify("Weekly Sync / Q4!") == "weekly-sync-q4"
    assert mt.slugify("!!!") == "meeting"
    started = mt.datetime(2026, 11, 9, 11, 0)
    assert mt.make_meeting_id(started, "Weekly Sync") == "2026-11-09_1100_weekly-sync"


def test_create_has_meta_and_no_staging_left(tmp_path):
    started = mt.now_local()
    a = mt.MeetingFolder.create(tmp_path, "Weekly sync", started, {"devices": {"mic": "x"}})
    b = mt.MeetingFolder.create(tmp_path, "Weekly sync", started, {})
    assert b.meeting_id == a.meeting_id + "-2"
    meta = a.read_meta()
    assert meta["status"] == "recording" and meta["schema"] == 1 and meta["devices"] == {"mic": "x"}
    assert sorted(os.listdir(tmp_path)) == sorted([a.meeting_id, b.meeting_id])
    assert a.audio_dir.is_dir()
    a.add_gap("system", 100, 400, "default audio device changed")
    assert a.read_meta()["gaps"][0]["end_ms"] == 400


def test_recover_crashed_meeting(tmp_path):
    folder = mt.MeetingFolder.create(tmp_path, "crash", mt.now_local(), {})
    with JsonlWriter(folder.live_path) as w:
        w.write(meeting_start_event(folder.meeting_id, "2026-11-09T11:00:00-06:00"))
        w.write({"type": "segment", "seq": 1, "start_ms": 0, "end_ms": 900, "source": "mic",
                 "speaker": "You", "text": "hi", "confidence": 0.9})
    with open(folder.live_path, "a", encoding="utf-8") as fh:
        fh.write('{"type":"segment","se')  # died mid-write

    finalized = []
    recovered = housekeeping.recover(tmp_path, finalized.append)
    assert recovered == [folder.meeting_id] and finalized[0].meeting_id == folder.meeting_id
    events = read_events(folder.live_path)
    assert [e["type"] for e in events] == ["meeting_start", "segment", "meeting_end"]
    assert events[-1]["interrupted"] is True and events[-1]["segments"] == 1
    meta = folder.read_meta()
    assert meta["status"] == "interrupted" and meta["clean_exit"] is False
    assert folder.live_path.read_text(encoding="utf-8").endswith("\n")


def test_recover_skips_live_and_done(tmp_path):
    live = mt.MeetingFolder.create(tmp_path, "live", mt.now_local(), {})
    done = mt.MeetingFolder.create(tmp_path, "done", mt.now_local(), {})
    done.mark_done()
    assert housekeeping.recover(tmp_path, None, skip={live.meeting_id}) == []


def test_purge_old_audio(tmp_path):
    old = mt.MeetingFolder.create(tmp_path, "old", mt.now_local(), {})
    (old.audio_dir / "mic.flac").write_bytes(b"x")
    old.mark_done()
    past = time.time() - 10 * 86400
    os.utime(old.done_path, (past, past))
    new = mt.MeetingFolder.create(tmp_path, "new", mt.now_local(), {})
    new.mark_done()
    assert housekeeping.purge_old_audio(tmp_path, 0) == []
    assert housekeeping.purge_old_audio(tmp_path, 7) == [old.meeting_id]
    assert not old.audio_dir.exists() and new.audio_dir.exists()


def test_settings_migration_resets_old_live_threshold(tmp_path):
    path = tmp_path / "s.json"
    # A v1 file as saved by the Settings dialog before Test #2's tuning.
    path.write_text('{"live_speaker_threshold": 0.6, "keep_audio": true}', encoding="utf-8")
    s = config.load(path)
    assert s.live_speaker_threshold is None and s.keep_audio is True
    # A v2 file saved after the Test #2 fixes carries min-new-speaker 1.5; v3 makes it 2.0.
    path.write_text('{"settings_version": 2, "live_speaker_threshold": 0.75, "live_min_new_speaker_s": 1.5}',
                    encoding="utf-8")
    assert config.load(path).live_min_new_speaker_s == 2.0
    # v4 moves the final voice threshold from 0.65 to 0.70 (gated fold, Test #4).
    path.write_text('{"settings_version": 3, "final_voice_threshold": 0.65}', encoding="utf-8")
    assert config.load(path).final_voice_threshold is None
    assert s.settings_version == config.SETTINGS_VERSION
    # v5 (WP13): a new fingerprint model. Numbers saved for the old one are dropped, and the cutoffs
    # come from the shipped model's profile.
    path.write_text('{"settings_version": 4, "live_speaker_threshold": 0.75, "final_voice_threshold": 0.70, '
                    '"final_cluster_threshold": 0.6, "keep_audio": true}', encoding="utf-8")
    s4 = config.load(path)
    assert (s4.live_speaker_threshold, s4.final_voice_threshold, s4.final_cluster_threshold) == (None, None, None)
    assert s4.keep_audio is True and s4.voice_profile() == profile()
    # A value saved on purpose under the current version is respected.
    s.live_speaker_threshold = 0.8
    config.save(s, path)
    assert config.load(path).live_speaker_threshold == 0.8
    assert config.load(path).voice_profile().live_match == 0.8
    assert config.load(path).voice_profile().final_match == profile().final_match


def test_settings_roundtrip_ignores_unknown_keys(tmp_path):
    path = tmp_path / "s.json"
    s = config.Settings(whisper_model="small.en", keep_audio=True)
    config.save(s, path)
    assert config.load(path) == s
    path.write_text('{"whisper_model": "tiny.en", "bogus": 1}', encoding="utf-8")
    assert config.load(path).whisper_model == "tiny.en"
    path.write_text("not json", encoding="utf-8")
    assert config.load(path) == config.Settings()


# -- WP0 foundations -----------------------------------------------------
def _in_threads(*fns):
    import threading

    threads = [threading.Thread(target=fn) for fn in fns]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_two_instances_do_not_lose_meta_updates(tmp_path):
    from tests.helpers import make_meeting

    a = make_meeting(tmp_path, [])
    b = mt.MeetingFolder(a.path)

    def work(folder, tag):
        return lambda: [folder.update_meta(**{f"{tag}{i}": i}) for i in range(50)]

    _in_threads(work(a, "a"), work(b, "b"))
    meta = a.read_meta()
    assert all(meta[f"a{i}"] == i and meta[f"b{i}"] == i for i in range(50))


def test_append_jsonl_threads_yield_whole_lines(tmp_path):
    import json

    from tests.helpers import make_meeting

    folder = make_meeting(tmp_path, [])
    other = mt.MeetingFolder(folder.path)

    def work(f, tag):
        return lambda: [mt.append_jsonl(f.marks_path, {"who": tag, "n": i, "text": "x" * 200})
                        for i in range(50)]

    _in_threads(work(folder, "a"), work(other, "b"))
    lines = folder.marks_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    parsed = [json.loads(line) for line in lines]
    assert sorted(p["n"] for p in parsed if p["who"] == "a") == list(range(50))


def test_make_meeting_is_readable(tmp_path):
    from tests.helpers import make_meeting

    folder = make_meeting(
        tmp_path, [("mic", 0, 900, "You", "Hello."), {"source": "system", "start_ms": 1000, "end_ms": 2000,
                                                        "speaker": "SPEAKER_01", "text": "Hi."}],
        final=[("mic", 0, 900, "You", "Hello.")], marks=[{"type": "mark", "ms": 5}],
        tags=[{"type": "tag", "speaker": "SPEAKER_01", "name": "Dana"}],
        gaps=[{"source": "mic", "start_ms": 1, "end_ms": 2, "reason": "rebind"}])
    events = read_events(folder.live_path)
    assert [e["type"] for e in events] == ["meeting_start", "segment", "segment", "meeting_end"]
    assert len(read_events(folder.final_path)) == 3 and folder.is_done
    assert len(read_events(folder.marks_path)) == 1 and len(read_events(folder.tags_path)) == 1
    meta = folder.read_meta()
    assert meta["status"] == "complete" and meta["segments"] == 2 and meta["gaps"][0]["reason"] == "rebind"
    assert [m.meeting_id for m in mt.iter_meetings(tmp_path)] == [folder.meeting_id]


def test_recover_stores_error(tmp_path):
    folder = mt.MeetingFolder.create(tmp_path, "Crash", mt.now_local(), {})
    JsonlWriter(folder.live_path).close()

    def boom(_folder):
        raise RuntimeError("model exploded")

    housekeeping.recover(tmp_path, boom)
    meta = folder.read_meta()
    assert meta["status"] == "failed" and meta["error"] == "model exploded"
