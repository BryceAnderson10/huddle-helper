import numpy as np

from huddle_helper.segments import Word
from huddle_helper.transcribe import drop_hallucinated, normalize_level

# Word timings Whisper produced for Test #2's quiet mic line (from audio/words.jsonl).
TEST2_LOOP = [
    (165208, 165288, " Good"), (165288, 165368, " to"), (165368, 165488, " see"), (165488, 165628, " how"),
    (165628, 165728, " does"), (165728, 165828, " it"), (165828, 165988, " handle"),
    (165988, 166448, " overlapping"), (166448, 166848, " voices?"),
    (166848, 166848, " Alright,"), (166848, 166868, " let's"), (166868, 166868, " go."),
    (166868, 166868, " Alright,"), (166868, 166968, " let's"), (166968, 166968, " go"),
    (166968, 166968, " check"), (166968, 166988, " this"), (166988, 167208, " one."),
    (167448, 167488, " Alright,"), (167488, 167488, " let's"), (167488, 167488, " go"),
    (167488, 167488, " check"), (167488, 167488, " this"), (167488, 167488, " one."),
    (167488, 167568, " Alright,"), (167568, 167568, " let's"), (167568, 167568, " go"),
    (167568, 167568, " check"), (167568, 167568, " this"), (167568, 167568, " one."),
]


def text(words):
    return "".join(w.text for w in words).strip()


def test_drops_test2_hallucination_loop():
    words = [Word(*w) for w in TEST2_LOOP]
    assert text(drop_hallucinated(words)) == "Good to see how does it handle overlapping voices?"


def test_keeps_normal_speech_and_one_repeat():
    words = [Word(0, 0, " I'm"), Word(0, 140, " really"), Word(140, 360, " just"), Word(360, 540, " testing.")]
    assert drop_hallucinated(words) == words  # a single 0 ms word is normal Whisper behaviour
    found = [Word(i * 250, i * 250 + 240, w) for i, w in enumerate([" I", " found", " it.", " I", " found", " it."])]
    assert text(drop_hallucinated(found)) == "I found it. I found it."


def test_collapses_runaway_repeats_even_at_normal_speed():
    sent = [" Let's", " go."]
    words = [Word(i * 300, i * 300 + 280, sent[i % 2]) for i in range(10)]
    assert text(drop_hallucinated(words)) == "Let's go. Let's go."


def test_normalize_level_boosts_quiet_audio_only():
    quiet = np.full(100, 0.047, np.float32)
    assert abs(float(normalize_level(quiet).max()) - 0.5) < 1e-3
    loud = np.full(100, 0.8, np.float32)
    assert float(normalize_level(loud).max()) == np.float32(0.8)
    tiny = np.full(100, 0.001, np.float32)
    assert abs(float(normalize_level(tiny).max()) - 0.03) < 1e-3  # gain capped at 30x
    assert len(normalize_level(np.zeros(0, np.float32))) == 0
