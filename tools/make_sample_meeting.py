"""Build a synthetic two-stream meeting (mic.wav + system.wav) with Windows TTS.

Remote speakers are on the system stream (two different voices), "you" are on
the mic stream. ``--echo`` leaks a quieter copy of the system audio into the
mic, like a laptop without headphones, to exercise echo dedupe.

    python tools/make_sample_meeting.py --out sample --echo
    huddle-helper simulate --mic sample/mic.wav --system sample/system.wav
"""

from __future__ import annotations

import argparse
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

RATE = 16000

SCRIPT = [
    ("mic", "Microsoft David Desktop", -1, "Okay everyone, let's start with the launch checklist for next week."),
    ("system", "Microsoft Zira Desktop", 0, "Sure. Legal still needs one more day to review the terms of service."),
    ("system", "Microsoft David Desktop", 2, "Engineering is on track. The release candidate was cut this morning and the tests are green."),
    ("mic", "Microsoft David Desktop", -1, "Great. Who is owning the customer email announcement?"),
    ("system", "Microsoft Zira Desktop", 0, "Marketing has a draft. I will send it to the group by Thursday afternoon."),
    ("system", "Microsoft David Desktop", 2, "One risk is the database migration. It took forty minutes in staging, so we should schedule a maintenance window."),
    ("mic", "Microsoft David Desktop", -1, "Let's book the window for Saturday morning then."),
    ("system", "Microsoft Zira Desktop", 0, "That works for me. I will update the status page the night before."),
    ("system", "Microsoft David Desktop", 2, "Agreed. I will run one more rehearsal of the migration on Wednesday."),
    ("mic", "Microsoft David Desktop", -1, "Perfect, thanks everyone. Same time next week."),
]

PS = r"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.SelectVoice('{voice}')
$s.Rate = {rate}
$s.SetOutputToWaveFile('{path}')
$s.Speak('{text}')
$s.Dispose()
"""


def tts(voice: str, rate: int, text: str, path: Path) -> np.ndarray:
    script = PS.format(voice=voice, rate=rate, path=str(path), text=text.replace("'", "''"))
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return soxr.resample(audio, sr, RATE).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="sample")
    ap.add_argument("--echo", action="store_true", help="leak system audio into the mic")
    ap.add_argument("--gap", type=float, default=0.8, help="seconds between lines")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    clips = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (stream, voice, rate, text) in enumerate(SCRIPT):
            clips.append((stream, tts(voice, rate, text, Path(tmp) / f"{i}.wav"), text, voice))

    total = int((sum(len(c[1]) for c in clips) / RATE + args.gap * (len(clips) + 2)) * RATE)
    tracks = {"mic": np.zeros(total, np.float32), "system": np.zeros(total, np.float32)}
    pos = int(args.gap * RATE)
    lines = []
    for stream, audio, text, voice in clips:
        tracks[stream][pos:pos + len(audio)] += audio * 0.8
        lines.append(f"{pos * 1000 // RATE:7d} ms  {stream:6s} {voice.split()[1]:5s}  {text}")
        pos += len(audio) + int(args.gap * RATE)
    if args.echo:
        delay = int(0.05 * RATE)
        tracks["mic"][delay:] += tracks["system"][:-delay] * 0.35
    rng = np.random.default_rng(0)
    for name, track in tracks.items():
        track += rng.normal(0, 0.002, len(track)).astype(np.float32)  # room noise
        sf.write(str(out / f"{name}.wav"), np.clip(track, -1, 1), RATE, subtype="PCM_16")
    (out / "script.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"Wrote {out / 'mic.wav'} and {out / 'system.wav'} ({total / RATE:.1f}s)")


if __name__ == "__main__":
    main()
