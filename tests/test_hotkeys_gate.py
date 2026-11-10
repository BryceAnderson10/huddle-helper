"""Hotkey combos and registration, and the mic gate on sample counts."""

import sys
import threading

import numpy as np
import pytest

from huddle_helper import config, hotkeys
from huddle_helper.audio.gate import MicGate

RATE = 16000


# -- combos ----------------------------------------------------------------------------
def test_parse_defaults_and_canonical_form():
    assert hotkeys.parse("Ctrl+Alt+B") == hotkeys.Combo(hotkeys.MOD_CONTROL | hotkeys.MOD_ALT, ord("B"), "Ctrl+Alt+B")
    c = hotkeys.parse(" shift + WIN + control + f5 ")
    assert c.text == "Ctrl+Shift+Win+F5" and c.mods == 2 | 4 | 8 and c.vk == 0x74
    assert hotkeys.parse("alt+1").vk == ord("1") and hotkeys.parse("Ctrl+Esc").text == "Ctrl+Esc"
    assert hotkeys.parse("Ctrl+pgup").text == "Ctrl+PageUp" and hotkeys.parse("Ctrl+F24").vk == 0x87
    for name, text in hotkeys.DEFAULTS.items():
        assert hotkeys.parse(text).text == text


@pytest.mark.parametrize("bad", ["B", "Ctrl", "Ctrl+", "Ctrl+Alt", "Ctrl+Ctrl+B", "Ctrl+Foo+B", "Ctrl+F25",
                                 "Ctrl+BB", "", "+B", "Ctrl+Alt+Shift+Win"])
def test_parse_rejects_with_a_reason(bad):
    with pytest.raises(ValueError) as e:
        hotkeys.parse(bad)
    assert str(e.value)


def test_normalize_and_duplicates():
    assert hotkeys.normalize("") == "" and hotkeys.normalize("  ") == "" and hotkeys.normalize("alt+ctrl+q") == "Ctrl+Alt+Q"
    with pytest.raises(ValueError):
        hotkeys.normalize("Q")
    dup = hotkeys.find_duplicates({"a": "Ctrl+Alt+B", "b": "alt+ctrl+b", "c": "", "d": "", "e": "junk"})
    assert dup == {"b": "a"}


def test_settings_hotkeys_default_round_trip_and_older_files(tmp_path):
    s = config.Settings()
    assert s.hotkeys == hotkeys.DEFAULTS and s.hotkeys is not hotkeys.DEFAULTS
    s.hotkeys["question"] = ""
    path = tmp_path / "s.json"
    config.save(s, path)
    assert config.load(path).hotkeys["question"] == ""
    # an older settings file has no key; a partial or odd one keeps defaults for what it lacks
    assert config.Settings.from_dict({"keep_audio": True}).hotkeys == hotkeys.DEFAULTS
    got = config.Settings.from_dict({"hotkeys": {"mute": "Ctrl+Alt+U", "nonsense": "x", "action": 5}}).hotkeys
    assert got == {**hotkeys.DEFAULTS, "mute": "Ctrl+Alt+U"}
    assert config.Settings.from_dict({"hotkeys": "bad"}).hotkeys == hotkeys.DEFAULTS


# -- AltGr ---------------------------------------------------------------------------------------------
def test_altgr_check_warns_and_suggests_shift():
    german = {"Ctrl+Alt+Q": "@", "Ctrl+Alt+M": "µ"}
    chars = lambda combo: german.get(combo.text)  # noqa: E731 - the layout, as a lookup
    q = hotkeys.check_combo("Ctrl+Alt+Q", chars)
    assert q.altgr == "@" and q.suggestion == "Ctrl+Alt+Shift+Q"
    assert "AltGr" in q.warning and "@" in q.warning and "Ctrl+Alt+Shift+Q" in q.warning
    assert hotkeys.check_combo("Ctrl+Alt+B", chars).altgr is None and hotkeys.check_combo("Ctrl+Alt+B", chars).warning == ""
    # the Shift form is also a character: warn, but suggest nothing
    both = hotkeys.check_combo("Ctrl+Alt+Q", lambda c: "@")
    assert both.altgr == "@" and both.suggestion == "" and "Try" not in both.warning
    # off and invalid
    assert hotkeys.check_combo("").off and hotkeys.check_combo("Q").error and not hotkeys.check_combo("Q").valid
    # a layout lookup that blows up must not break the dialog
    def boom(_):
        raise OSError("no layout")
    assert hotkeys.check_combo("Ctrl+Alt+Q", boom).altgr is None


@pytest.mark.skipif(sys.platform != "win32", reason="ToUnicodeEx")
def test_altgr_char_on_this_layout_is_none_or_a_printable_character():
    # US layouts type nothing for Ctrl+Alt+letter; AltGr layouts type a character. Both are fine.
    for combo in hotkeys.DEFAULTS.values():
        ch = hotkeys.altgr_char(combo)
        assert ch is None or (len(ch) == 1 and ch.isprintable())
    assert hotkeys.altgr_char("Ctrl+Shift+B") is None  # not Ctrl+Alt: never AltGr


# -- registration (real Win32; the combos are ones nothing uses) -------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="RegisterHotKey")
def test_registration_conflict_and_release():
    free = {"a": "Ctrl+Alt+Shift+F23", "b": "Ctrl+Alt+Shift+F22", "off": "", "bad": "nope"}
    first = hotkeys.HotkeyThread(free, lambda name: None)
    res = first.start()
    try:
        assert res["a"].ok and res["b"].ok
        assert res["off"] == hotkeys.Registration("", False, "off") and res["bad"].error and not res["bad"].ok
        assert first.failed.keys() == {"bad"}
        second = hotkeys.probe({"x": "Ctrl+Alt+Shift+F23", "y": "Ctrl+Alt+Shift+F21"})
        assert not second["x"].ok and "already in use" in second["x"].error and second["y"].ok
    finally:
        first.stop()
    assert not first._thread.is_alive()
    assert hotkeys.probe({"x": "Ctrl+Alt+Shift+F23"})["x"].ok  # released by stop()


# -- the mic gate ------------------------------------------------------------------------------------------
def chunk(n, value=0.5):
    return np.full(n, value, dtype=np.float32)


def test_open_gate_passes_the_same_array_through_untouched():
    g = MicGate()
    x = chunk(1600)
    assert g.process(x) is x and g.take_closed() == [] and g.muted_since_ms is None and not g.zeroing


def test_mute_zeroes_from_the_next_chunk_and_records_the_start_sample():
    g = MicGate()
    g.process(chunk(8000))  # 500 ms
    g.mute()
    assert g.muted and g.muted_since_ms == 500
    out = g.process(chunk(1600))
    assert out.shape == (1600,) and not out.any() and out.dtype == np.float32
    assert g.take_closed() == []  # still open


def test_unmute_keeps_zeroing_for_250_ms_and_the_period_ends_exactly_there():
    g = MicGate()
    g.process(chunk(16000))  # 1 s
    g.mute()
    g.process(chunk(16000))  # 1 s muted
    g.unmute()
    assert not g.muted and g.zeroing and g.muted_since_ms == 1000  # the period is open until the tail ends
    assert not g.process(chunk(2000)).any()  # 125 ms of tail
    out = g.process(chunk(4000))  # the tail ends 2000 samples in: zeros, then audio
    assert not out[:2000].any() and (out[2000:] == 0.5).all()
    assert g.take_closed() == [(1000, 2250)] and g.take_closed() == []
    assert g.muted_since_ms is None and (g.process(chunk(10)) == 0.5).all()


def test_the_tail_can_end_exactly_on_a_chunk_boundary():
    g = MicGate()
    g.mute()
    g.process(chunk(1000))
    g.unmute()
    assert not g.process(chunk(4000)).any()  # the whole tail in one chunk, ends at the boundary
    assert g.take_closed() == []  # not noticed yet
    assert (g.process(chunk(100)) == 0.5).all()
    assert g.take_closed() == [(0, 312)]  # 5000 samples
    assert g.muted_since_ms is None


def test_pressing_mute_again_during_the_tail_continues_the_same_period():
    g = MicGate()
    g.mute()
    g.process(chunk(16000))
    g.unmute()
    g.process(chunk(1000))
    g.mute()  # changed their mind 62 ms later
    assert g.muted and g.muted_since_ms == 0
    g.process(chunk(16000))
    g.unmute()
    g.process(chunk(8000))
    assert g.take_closed() == [(0, (16000 + 1000 + 16000) * 1000 // RATE + 250)]


def test_unmute_when_not_muted_and_double_mute_are_harmless():
    g = MicGate()
    g.unmute()
    g.mute()
    g.mute()
    g.process(chunk(100))
    assert g.muted_since_ms == 0
    g.unmute()
    g.unmute()
    g.process(chunk(8000))
    assert g.take_closed() == [(0, 256)]  # mute at 100 samples, 4000 of tail: 4100 samples


def test_started_muted_and_muted_at_stop():
    g = MicGate(muted=True)
    assert g.muted and g.muted_since_ms == 0
    for _ in range(3):
        assert not g.process(chunk(16000)).any()
    g.finish()  # Stop: the period ends with the audio, at 3 s, and the gate opens
    assert g.take_closed() == [(0, 3000)] and not g.muted and g.muted_since_ms is None
    # a stop in the middle of the unmute tail ends at the audio, not at the tail
    g = MicGate()
    g.mute()
    g.process(chunk(16000))
    g.unmute()
    g.process(chunk(800))
    g.finish()
    assert g.take_closed() == [(0, 1050)]


def test_a_zero_length_period_is_not_reported():
    g = MicGate()
    g.mute()
    g.finish()
    assert g.take_closed() == []


def test_millisecond_times_are_sample_counts():
    g = MicGate()
    g.process(chunk(16 * 1234))
    g.mute()
    assert g.muted_since_ms == 1234 and g.position_ms == 1234
    g.process(chunk(16 * 4000))
    g.unmute()
    g.process(chunk(16 * 1000))
    assert g.take_closed() == [(1234, 1234 + 4000 + 250)]


def test_process_from_two_threads_is_consistent():
    g = MicGate()
    stop = threading.Event()

    def toggler():
        while not stop.is_set():
            g.mute()
            g.unmute()

    t = threading.Thread(target=toggler)
    t.start()
    total = 0
    for _ in range(2000):
        out = g.process(chunk(160))
        assert len(out) == 160
        total += 160
    stop.set()
    t.join()
    assert g.position_ms == total * 1000 // RATE
