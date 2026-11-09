"""Command line entry point.

    huddle-helper                      run the tray app (default)
    huddle-helper record --title T     record from the default devices until Enter or --minutes
    huddle-helper simulate --mic a.wav --system b.wav   run audio files through the full pipeline
    huddle-helper capture-spike --minutes 30            milestone 1: raw capture check
    huddle-helper bench --audio x.wav --models base.en,small.en   milestone 2: speed check
    huddle-helper finalize <meeting folder>             re-run the after-Stop pass
    huddle-helper recover                               close out meetings left by a crash
    huddle-helper devices | download-models
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import sys
import threading
import time
from pathlib import Path

from . import __version__, config, models, paths


def setup_logging(verbose: bool = False, console: bool = True) -> None:
    paths.logs_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(paths.logs_dir() / "huddle-helper.log", maxBytes=5_000_000,
                                             backupCount=3, encoding="utf-8")
    ]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def go_offline() -> None:
    """After models are installed the app makes no network calls."""
    import os

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def _settings(args) -> config.Settings:
    s = config.load()
    if getattr(args, "out", None):
        s.meetings_dir = str(Path(args.out).resolve())
    if getattr(args, "model", None):
        s.whisper_model = args.model
    if getattr(args, "keep_audio", False):
        s.keep_audio = True
    if getattr(args, "diarize_mic", False):
        s.diarize_mic = True
    if getattr(args, "no_echo_dedupe", False):
        s.echo_dedupe = False
    return s


def _ensure_models(s: config.Settings) -> None:
    missing = models.missing_models(s.whisper_model, s.needs_diarization)
    if missing:
        print(f"Installing models: {', '.join(missing)} (one-time download)")
        models.install_all(s.whisper_model, s.needs_diarization, progress=print)


def _finalize(folder, s: config.Settings) -> None:
    from .diarize import SherpaOfflineDiarizer
    from .finalize import finalize_meeting

    def progress(stage, frac):
        print(f"\r{stage}: {frac:4.0%}   ", end="", flush=True)

    count = finalize_meeting(folder, lambda: SherpaOfflineDiarizer(threshold=s.final_cluster_threshold),
                             keep_audio=s.keep_audio, progress=progress)
    print(f"\nFinal transcript: {folder.final_path} ({count} segments)")


def _print_status(st) -> None:
    print(f"\r[{st.state}] {st.elapsed_s:6.0f}s  lines={st.segments}  backlog={st.backlog}  lag={st.lag_s:4.1f}s   ",
          end="", flush=True)


def _latency_report(rec) -> None:
    lat = sorted(rec.latencies_ms)
    if lat:
        p90 = lat[min(len(lat) - 1, int(0.9 * len(lat)))]
        print(f"Line latency after utterance end: median {lat[len(lat) // 2]} ms, p90 {p90} ms")


def cmd_record(args) -> int:
    from .recorder import Recorder, load_components

    s = _settings(args)
    _ensure_models(s)
    go_offline()
    print("Reminder: tell everyone in the meeting that you are recording.")
    rec = Recorder(s, load_components(s), on_status=_print_status, on_error=lambda m: print(f"\n! {m}"))
    folder = rec.start(args.title)
    print(f"Recording to {folder.path}")
    if args.minutes:
        try:
            time.sleep(args.minutes * 60)
        except KeyboardInterrupt:
            pass
    else:
        print("Press Enter to stop.")
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
    rec.stop()
    print()
    _latency_report(rec)
    _finalize(folder, s)
    return 0


def cmd_simulate(args) -> int:
    from .audio.filesource import FileCapture
    from .recorder import Recorder, load_components

    s = _settings(args)
    _ensure_models(s)
    go_offline()
    comps = load_components(s)
    holder = {}

    def factory(**kw):
        cap = FileCapture(kw["on_audio"], {"mic": args.mic, "system": args.system}, realtime=args.realtime)
        holder["cap"] = cap
        return cap

    comps.capture_factory = factory
    rec = Recorder(s, comps, on_status=_print_status if args.realtime else None)
    t = time.perf_counter()
    folder = rec.start(args.title)
    holder["cap"].finished.wait()
    rec.stop()
    took = time.perf_counter() - t
    audio_s = holder["cap"].length / 16000
    print(f"\nLive pass: {audio_s:.1f}s of audio in {took:.1f}s ({audio_s / took:.1f}x real time)")
    if args.realtime:
        _latency_report(rec)
    t = time.perf_counter()
    _finalize(folder, s)
    print(f"Final pass took {time.perf_counter() - t:.1f}s")
    print(f"Meeting folder: {folder.path}")
    return 0


def cmd_capture_spike(args) -> int:
    """Milestone 1: capture mic and loopback to separate WAVs and report gaps."""
    import numpy as np
    import soundfile as sf

    from .audio.capture import WasapiCapture

    out = Path(args.out or ".").resolve()
    out.mkdir(parents=True, exist_ok=True)
    files = {src: sf.SoundFile(str(out / f"spike_{src}.wav"), "w", 16000, 1, "PCM_16") for src in ("mic", "system")}
    counts = {"mic": 0, "system": 0}
    gaps = []
    lock = threading.Lock()

    def on_audio(src, x):
        with lock:
            files[src].write(np.clip(x, -1, 1))
            counts[src] += len(x)

    cap = WasapiCapture(on_audio, args.mic_device, args.output_device,
                        on_gap=lambda *g: gaps.append(g), on_error=lambda m: print(f"\n! {m}"))
    t0 = time.perf_counter()
    cap.start(t0)
    print(f"Capturing mic={cap.device_names['mic']!r} system={cap.device_names['system']!r} "
          f"for {args.minutes} min (Ctrl+C to stop early)")
    try:
        end = t0 + args.minutes * 60
        while time.perf_counter() < end:
            time.sleep(1)
            el = time.perf_counter() - t0
            print(f"\r{el:6.0f}s  mic={counts['mic'] / 16000:7.1f}s  system={counts['system'] / 16000:7.1f}s",
                  end="", flush=True)
    except KeyboardInterrupt:
        pass
    cap.stop()
    wall = time.perf_counter() - t0
    for f in files.values():
        f.close()
    print(f"\nWall clock {wall:.1f}s; mic {counts['mic'] / 16000:.1f}s; system {counts['system'] / 16000:.1f}s")
    print(f"Device gaps: {gaps or 'none'}")
    print(f"WAVs written to {out}")
    return 0


def cmd_bench(args) -> int:
    """Milestone 2: VAD-chunk one file and transcribe it with each model."""
    from .audio.buffer import read_audio
    from .transcribe import WhisperTranscriber
    from .vad import UtteranceSegmenter

    audio = read_audio(Path(args.audio))
    utts = []
    seg = UtteranceSegmenter("bench", utts.append)
    seg.feed(audio)
    seg.flush()
    dur = len(audio) / 16000
    print(f"{args.audio}: {dur:.1f}s, {len(utts)} utterances")
    for name in args.models.split(","):
        models.install_whisper(name, progress=print)
        go_offline()
        tr = WhisperTranscriber(name, beam_size=args.beam)
        t = time.perf_counter()
        slowest = 0.0
        for u in utts:
            t1 = time.perf_counter()
            tr.transcribe(u.audio)
            slowest = max(slowest, time.perf_counter() - t1)
        took = time.perf_counter() - t
        print(f"{name:10s} {took:6.1f}s  speed {dur / took:5.2f}x real time  slowest utterance {slowest:.1f}s")
    return 0


def cmd_finalize(args) -> int:
    from .meeting import MeetingFolder

    s = _settings(args)
    go_offline()
    folder = MeetingFolder(Path(args.folder))
    if folder.done_path.exists():
        folder.done_path.unlink()
    _finalize(folder, s)
    return 0


def cmd_recover(args) -> int:
    from .housekeeping import recover

    s = _settings(args)
    go_offline()
    done = recover(s.resolved_meetings_dir(), lambda f: _finalize(f, s))
    print(f"Recovered: {done or 'nothing to recover'}")
    return 0


def cmd_devices(args) -> int:
    from .audio.capture import list_devices

    print(json.dumps(list_devices(), indent=2))
    return 0


def cmd_download(args) -> int:
    s = _settings(args)
    models.install_all(s.whisper_model, True, progress=print)
    print(f"Models installed in {paths.models_dir()}")
    return 0


def cmd_tray(args) -> int:
    from .tray import run_tray

    return run_tray()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="huddle-helper", description="Local meeting recorder and transcriber")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--out", help="meetings folder (overrides settings)")
        sp.add_argument("--model", choices=models.WHISPER_MODELS, help="whisper model (overrides settings)")
        sp.add_argument("--keep-audio", action="store_true")
        sp.add_argument("--diarize-mic", action="store_true")
        sp.add_argument("--no-echo-dedupe", action="store_true")

    sp = sub.add_parser("tray", help="run the tray app")
    sp.set_defaults(func=cmd_tray)

    sp = sub.add_parser("record", help="record from live devices without the tray")
    sp.add_argument("--title", default="meeting")
    sp.add_argument("--minutes", type=float)
    common(sp)
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("simulate", help="run audio files through the pipeline")
    sp.add_argument("--mic")
    sp.add_argument("--system")
    sp.add_argument("--title", default="simulated")
    sp.add_argument("--realtime", action="store_true", help="feed audio at real-time speed")
    common(sp)
    sp.set_defaults(func=cmd_simulate)

    sp = sub.add_parser("capture-spike", help="milestone 1: capture to WAVs")
    sp.add_argument("--minutes", type=float, default=30)
    sp.add_argument("--out")
    sp.add_argument("--mic-device")
    sp.add_argument("--output-device")
    sp.set_defaults(func=cmd_capture_spike)

    sp = sub.add_parser("bench", help="milestone 2: transcription speed")
    sp.add_argument("--audio", required=True)
    sp.add_argument("--models", default="base.en,small.en")
    sp.add_argument("--beam", type=int, default=1)
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("finalize", help="re-run the after-Stop pass on a meeting folder")
    sp.add_argument("folder")
    common(sp)
    sp.set_defaults(func=cmd_finalize)

    sp = sub.add_parser("recover", help="finish meetings left by a crash")
    common(sp)
    sp.set_defaults(func=cmd_recover)

    sp = sub.add_parser("devices", help="list audio devices")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("download-models", help="install models for offline use")
    sp.add_argument("--model", choices=models.WHISPER_MODELS)
    sp.set_defaults(func=cmd_download)

    args = p.parse_args(argv)
    if args.cmd is None:
        args.func = cmd_tray
    setup_logging(args.verbose, console=args.cmd not in (None, "tray"))
    return args.func(args)
