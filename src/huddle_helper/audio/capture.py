"""WASAPI capture of the microphone and system playback (F1, F2).

The microphone is a normal WASAPI capture stream. Meeting audio is WASAPI
loopback on the playback device, which needs no virtual driver. Both are
downmixed to mono, resampled to 16 kHz and aligned to the meeting clock.

If a selected device follows the Windows default and the default changes
mid-meeting (for example a headset is plugged in), or a stream dies, both
streams are re-opened and the gap is reported so it can be logged in
meta.json.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from . import SAMPLE_RATE
from .aligner import StreamAligner

log = logging.getLogger(__name__)

OnAudio = Callable[[str, np.ndarray], None]
OnGap = Callable[[str, int, int, str], None]
OnError = Callable[[str], None]

LOOPBACK_SUFFIX = " [Loopback]"


@dataclass
class DeviceInfo:
    index: int
    name: str
    channels: int
    rate: int


def _pyaudio():
    import pyaudiowpatch as pyaudio

    return pyaudio


def list_devices() -> dict[str, list[str]]:
    """Names of WASAPI input devices and playback devices, for settings (F11)."""
    pyaudio = _pyaudio()
    p = pyaudio.PyAudio()
    try:
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        inputs, outputs = [], []
        for i in range(p.get_device_count()):
            d = p.get_device_info_by_index(i)
            if d["hostApi"] != wasapi["index"] or d.get("isLoopbackDevice"):
                continue
            if d["maxInputChannels"] > 0:
                inputs.append(d["name"])
            if d["maxOutputChannels"] > 0:
                outputs.append(d["name"])
        return {"inputs": inputs, "outputs": outputs}
    finally:
        p.terminate()


def _resolve_devices(p, pyaudio, mic_name: str | None, output_name: str | None):
    wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    devices = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
    devices = [d for d in devices if d["hostApi"] == wasapi["index"]]

    mic = None
    if mic_name:
        mic = next((d for d in devices if not d.get("isLoopbackDevice") and d["name"] == mic_name
                    and d["maxInputChannels"] > 0), None)
        if mic is None:
            log.warning("Microphone %r not found; using the default microphone", mic_name)
    if mic is None and wasapi["defaultInputDevice"] >= 0:
        mic = p.get_device_info_by_index(wasapi["defaultInputDevice"])

    loop = None
    if output_name:
        loop = next((d for d in devices if d.get("isLoopbackDevice")
                     and d["name"] == output_name + LOOPBACK_SUFFIX), None)
        if loop is None:
            log.warning("Playback device %r not found; using the default", output_name)
    if loop is None:
        try:
            loop = p.get_default_wasapi_loopback()
        except (OSError, LookupError):
            loop = None

    def info(d):
        if d is None:
            return None
        return DeviceInfo(int(d["index"]), d["name"], int(d["maxInputChannels"]), int(d["defaultSampleRate"]))

    return info(mic), info(loop)


def _default_endpoint_ids() -> tuple[str | None, str | None]:
    """IDs of the Windows default microphone and speakers (via Core Audio)."""
    from .endpoints import default_endpoint_ids

    return default_endpoint_ids()


class _Stream:
    """One open PortAudio stream feeding an aligner."""

    def __init__(self, source: str, device: DeviceInfo, aligner: StreamAligner, lock: threading.Lock):
        import soxr

        self.source = source
        self.device = device
        self._aligner = aligner
        self._lock = lock
        self._resampler = soxr.ResampleStream(device.rate, SAMPLE_RATE, 1, dtype="float32")
        self.last_data = time.perf_counter()
        self.stream = None

    def callback(self, in_data, frame_count, time_info, status):
        pyaudio = _pyaudio()
        now = time.perf_counter()
        try:
            pcm = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
            if self.device.channels > 1:
                pcm = pcm.reshape(-1, self.device.channels).mean(axis=1)
            out = self._resampler.resample_chunk(pcm)
            with self._lock:
                self._aligner.push(now, out)
            self.last_data = now
        except Exception:  # noqa: BLE001 - never raise into PortAudio
            log.exception("Error handling %s audio", self.source)
        return (None, pyaudio.paContinue)


class WasapiCapture:
    """Captures mic and loopback; delivers aligned 16 kHz mono via ``on_audio``."""

    sources = ("mic", "system")

    def __init__(self, on_audio: OnAudio, mic_device: str | None = None, output_device: str | None = None,
                 on_gap: OnGap | None = None, on_error: OnError | None = None,
                 poll_interval_s: float = 2.0, stall_timeout_s: float = 3.0):
        self._on_audio = on_audio
        self._on_gap = on_gap or (lambda *a: None)
        self._on_error = on_error or (lambda msg: None)
        self._mic_name = mic_device
        self._out_name = output_device
        self._poll = poll_interval_s
        self._stall = stall_timeout_s
        self._lock = threading.Lock()
        self._pa = None
        self._streams: dict[str, _Stream] = {}
        self._aligners: dict[str, StreamAligner] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.t0 = 0.0
        self.device_names: dict[str, str | None] = {"mic": None, "system": None}
        self._missing_since: dict[str, int] = {}
        self._missing_retry = 15.0
        self._retry_at = 0.0

    # -- lifecycle ------------------------------------------------------
    def start(self, t0: float) -> None:
        self.t0 = t0
        for src in self.sources:
            self._aligners[src] = StreamAligner(t0, lambda x, s=src: self._on_audio(s, x))
        self._open()
        for src in self.sources:
            if src not in self._streams:
                self._missing_since[src] = 0
        self._retry_at = time.perf_counter() + self._missing_retry
        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._ticker, name="capture-tick", daemon=True),
            threading.Thread(target=self._watcher, name="device-watch", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._close()
        # Pad both streams to the stop time so they end together.
        now = time.perf_counter()
        with self._lock:
            for aligner in self._aligners.values():
                aligner.finish(now)

    # -- open / close ---------------------------------------------------
    def _open(self) -> None:
        pyaudio = _pyaudio()
        self._pa = pyaudio.PyAudio()
        mic, loop = _resolve_devices(self._pa, pyaudio, self._mic_name, self._out_name)
        for src, dev in (("mic", mic), ("system", loop)):
            if dev is None:
                self._on_error(f"No {'microphone' if src == 'mic' else 'playback device'} found")
                self.device_names[src] = None
                continue
            s = _Stream(src, dev, self._aligners[src], self._lock)
            try:
                s.stream = self._pa.open(
                    format=pyaudio.paInt16, channels=dev.channels, rate=dev.rate, input=True,
                    input_device_index=dev.index, frames_per_buffer=dev.rate // 50,
                    stream_callback=s.callback,
                )
                s.stream.start_stream()
            except OSError as exc:
                log.error("Could not open %s device %r: %s", src, dev.name, exc)
                self._on_error(f"Could not open {dev.name}: {exc}")
                continue
            self._streams[src] = s
            self.device_names[src] = dev.name.removesuffix(LOOPBACK_SUFFIX)
            log.info("Capturing %s from %s (%d Hz, %d ch)", src, dev.name, dev.rate, dev.channels)

    def _close(self) -> None:
        for s in self._streams.values():
            try:
                if s.stream is not None:
                    s.stream.stop_stream()
                    s.stream.close()
            except OSError:
                pass
        self._streams.clear()
        if self._pa is not None:
            self._pa.terminate()
            self._pa = None

    def _now_ms(self) -> int:
        return int((time.perf_counter() - self.t0) * 1000)

    def _rebind(self, reason: str) -> None:
        log.info("Re-opening audio devices: %s", reason)
        gap_start = self._now_ms()
        was_open = set(self._streams)
        self._close()
        try:
            self._open()
        except Exception as exc:  # noqa: BLE001
            log.exception("Re-opening devices failed")
            self._on_error(f"Audio devices unavailable: {exc}")
        gap_end = self._now_ms()
        for src in self.sources:
            if src in was_open:
                if src not in self._streams:
                    self._missing_since[src] = gap_start
                    continue
                self._on_gap(src, gap_start, gap_end, reason)
            elif src in self._streams:
                # A device that was missing came back.
                since = self._missing_since.pop(src, 0)
                self._on_gap(src, since, gap_end, "device unavailable")

    # -- background threads ---------------------------------------------
    def _ticker(self) -> None:
        while not self._stop.wait(0.1):
            now = time.perf_counter()
            with self._lock:
                for aligner in self._aligners.values():
                    aligner.tick(now)

    def _watcher(self) -> None:
        try:
            import comtypes

            comtypes.CoInitialize()
        except Exception:  # noqa: BLE001
            comtypes = None
        follow_default = self._mic_name is None or self._out_name is None
        last_ids = _default_endpoint_ids() if follow_default else (None, None)
        try:
            while not self._stop.wait(self._poll):
                reason = None
                if follow_default:
                    ids = _default_endpoint_ids()
                    if ids != last_ids and all(ids):
                        reason = "default audio device changed"
                        last_ids = ids
                now = time.perf_counter()
                mic = self._streams.get("mic")
                if reason is None and mic is not None:
                    # A live mic always delivers packets; loopback legitimately goes quiet.
                    if now - mic.last_data > self._stall:
                        reason = "microphone stopped delivering audio"
                for s in list(self._streams.values()):
                    if reason is None and s.stream is not None and not s.stream.is_active():
                        reason = f"{s.source} stream stopped"
                if reason is None and len(self._streams) < len(self.sources) and now >= self._retry_at:
                    reason = "retrying missing device"
                    self._retry_at = now + self._missing_retry
                if reason:
                    self._rebind(reason)
        finally:
            if comtypes is not None:
                comtypes.CoUninitialize()
