"""WP10 spike: WASAPI process loopback (exclude our own process tree) through comtypes.

Standalone: comtypes + ctypes + numpy (+ pyaudiowpatch for the comparison device loopback and
for playing the test tones, exactly as the product does). Not used by the product.

    python tools/process_loopback_spike.py formats
    python tools/process_loopback_spike.py capture --seconds 10
    python tools/process_loopback_spike.py tones
    python tools/process_loopback_spike.py soak --minutes 20

Privacy: captured audio is never written anywhere. Blocks are reduced to numbers (RMS, band
magnitudes at the test frequencies, packet statistics) and discarded. Tones are <= 0.02 of full
scale and <= 3 s. Nothing here changes volume, mute, default devices or any setting.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import HRESULT, POINTER, byref, c_long, c_ubyte, c_uint32, c_uint64, c_void_p, sizeof
from ctypes.wintypes import DWORD, HANDLE, LPCWSTR, WORD

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
LAB = os.path.join(os.path.dirname(HERE), "lab", "wp10")

MAX_AMP = 0.02
MAX_TONE_S = 3.0

# --------------------------------------------------------------------------- COM declarations
import comtypes  # noqa: E402
from comtypes import COMMETHOD, GUID, IUnknown  # noqa: E402

VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")

AUDCLNT_SHAREMODE_SHARED = 0
FLAG_LOOPBACK = 0x00020000
FLAG_EVENTCALLBACK = 0x00040000
FLAG_AUTOCONVERTPCM = 0x80000000
FLAG_SRC_DEFAULT_QUALITY = 0x08000000
BUF_DISCONTINUITY, BUF_SILENT, BUF_TIMESTAMP_ERROR = 1, 2, 4

PROCESS_LOOPBACK_MODE_INCLUDE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE = 1
ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
VT_BLOB = 65


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", WORD), ("nChannels", WORD), ("nSamplesPerSec", DWORD),
                ("nAvgBytesPerSec", DWORD), ("nBlockAlign", WORD), ("wBitsPerSample", WORD),
                ("cbSize", WORD)]


class ACTIVATION_PARAMS(ctypes.Structure):  # AUDIOCLIENT_ACTIVATION_PARAMS
    _fields_ = [("ActivationType", DWORD), ("TargetProcessId", DWORD), ("ProcessLoopbackMode", DWORD)]


class PROPVARIANT_BLOB(ctypes.Structure):  # PROPVARIANT, VT_BLOB arm (24 bytes on x64)
    _fields_ = [("vt", WORD), ("r1", WORD), ("r2", WORD), ("r3", WORD),
                ("cbSize", ctypes.c_ulong), ("pBlobData", c_void_p)]


def make_format(tag: int, channels: int, rate: int, bits: int) -> WAVEFORMATEX:
    f = WAVEFORMATEX()
    f.wFormatTag, f.nChannels, f.nSamplesPerSec, f.wBitsPerSample = tag, channels, rate, bits
    f.nBlockAlign = channels * bits // 8
    f.nAvgBytesPerSec = rate * f.nBlockAlign
    f.cbSize = 0
    return f


class IAgileObject(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = ()


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(c_long), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation")),
    )


class IAudioClient(IUnknown):
    _iid_ = IID_IAudioClient
    _methods_ = (
        COMMETHOD([], HRESULT, "Initialize", (["in"], DWORD, "ShareMode"), (["in"], DWORD, "StreamFlags"),
                  (["in"], ctypes.c_longlong, "hnsBufferDuration"), (["in"], ctypes.c_longlong, "hnsPeriodicity"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"), (["in"], POINTER(GUID), "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(ctypes.c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], DWORD, "ShareMode"),
                  (["in"], POINTER(WAVEFORMATEX), "pFormat"),
                  (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (["out"], POINTER(ctypes.c_longlong), "phnsDefault"),
                  (["out"], POINTER(ctypes.c_longlong), "phnsMinimum")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (["in"], HANDLE, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService", (["in"], POINTER(GUID), "riid"),
                  (["out"], POINTER(c_void_p), "ppv")),
    )


class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                  (["out"], POINTER(c_uint32), "pNumFramesToRead"), (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
    )


class _Handler(comtypes.COMObject):
    """Completion handler. Must also answer QueryInterface(IAgileObject), or activation fails."""

    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()
        self.hr = None
        self.unk = None
        self.thread_id = None

    def ActivateCompleted(self, *args):  # args: (this?, operation)
        try:
            self.thread_id = threading.get_native_id()
            op = args[-1]
            hr, unk = op.GetActivateResult()
            self.hr, self.unk = hr, unk
        except Exception as exc:  # noqa: BLE001
            self.hr = f"callback failed: {exc!r}"
        finally:
            self.done.set()
        return 0


def hr_hex(e) -> str:
    code = getattr(e, "hresult", None)
    if code is None and getattr(e, "args", None) and isinstance(e.args[0], int):
        code = e.args[0]
    return f"0x{(code or 0) & 0xFFFFFFFF:08X}" if code is not None else repr(e)


def activate(pid: int, mode: int = PROCESS_LOOPBACK_MODE_EXCLUDE, timeout: float = 5.0):
    """Returns (IAudioClient, info dict). Raises RuntimeError with the HRESULT on failure."""
    params = ACTIVATION_PARAMS(ACTIVATION_TYPE_PROCESS_LOOPBACK, pid, mode)
    pv = PROPVARIANT_BLOB()
    pv.vt = VT_BLOB
    pv.cbSize = sizeof(params)
    pv.pBlobData = ctypes.cast(ctypes.pointer(params), c_void_p)
    handler = _Handler()
    handler_ptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    fn = ctypes.windll.Mmdevapi.ActivateAudioInterfaceAsync
    fn.restype = ctypes.c_long
    fn.argtypes = [LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT_BLOB),
                   POINTER(IActivateAudioInterfaceCompletionHandler),
                   POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    t0 = time.perf_counter()
    hr = fn(VAD_PROCESS_LOOPBACK, byref(IID_IAudioClient), byref(pv), handler_ptr, byref(op))
    if hr < 0:
        raise RuntimeError(f"ActivateAudioInterfaceAsync failed 0x{hr & 0xFFFFFFFF:08X}")
    if not handler.done.wait(timeout):
        raise RuntimeError("activation completion handler never called")
    info = {"activate_ms": round((time.perf_counter() - t0) * 1000, 1),
            "callback_on_other_thread": handler.thread_id != threading.get_native_id()}
    if not isinstance(handler.hr, int):
        raise RuntimeError(str(handler.hr))
    if handler.hr < 0:
        raise RuntimeError(f"activation result 0x{handler.hr & 0xFFFFFFFF:08X}")
    client = handler.unk.QueryInterface(IAudioClient)
    return client, info


# --------------------------------------------------------------------------- capture
FORMATS = {
    "f32_48000_2": (3, 2, 48000, 32),
    "i16_48000_2": (1, 2, 48000, 16),
    "i16_44100_2": (1, 2, 44100, 16),
    "f32_48000_1": (3, 1, 48000, 32),
    "i16_16000_1": (1, 1, 16000, 16),
    "f32_16000_1": (3, 1, 16000, 32),
    "i16_8000_1": (1, 1, 8000, 16),
}


class ProcLoop:
    """One process-loopback capture client, event driven."""

    def __init__(self, pid: int | None = None, mode: int = PROCESS_LOOPBACK_MODE_EXCLUDE,
                 fmt: str = "f32_48000_2", flags: int = FLAG_LOOPBACK | FLAG_EVENTCALLBACK,
                 period_100ns: int = 200000):
        self.pid = os.getpid() if pid is None else pid
        self.fmt_name = fmt
        self.fmt = make_format(*FORMATS[fmt])
        self.client, self.info = activate(self.pid, mode)
        self.client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, period_100ns, 0, byref(self.fmt), None)
        self.event = ctypes.windll.kernel32.CreateEventW(None, False, False, None)
        if flags & FLAG_EVENTCALLBACK:
            self.client.SetEventHandle(self.event)
        self.event_driven = bool(flags & FLAG_EVENTCALLBACK)
        ptr = self.client.GetService(byref(IAudioCaptureClient._iid_))
        self.capture = ctypes.cast(ptr, POINTER(IAudioCaptureClient))
        self.rate = self.fmt.nSamplesPerSec
        self.channels = self.fmt.nChannels
        self.is_float = self.fmt.wFormatTag == 3
        self.bits = self.fmt.wBitsPerSample
        self.buffer_frames = self.client.GetBufferSize()

    def to_mono(self, raw: bytes) -> np.ndarray:
        if self.is_float:
            a = np.frombuffer(raw, dtype=np.float32)
        elif self.bits == 16:
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            raise ValueError("unsupported")
        if self.channels > 1:
            a = a.reshape(-1, self.channels).mean(axis=1)
        return a

    def pump(self, stop: threading.Event, on_packet, wait_ms: int = 100) -> None:
        """Run until ``stop``. on_packet(t_perf, frames, flags, devpos, qpc, mono_or_None, waited_s)."""
        k32 = ctypes.windll.kernel32
        self.client.Start()
        last = time.perf_counter()
        try:
            while not stop.is_set():
                r = k32.WaitForSingleObject(self.event, wait_ms) if self.event_driven else (time.sleep(0.01) or 0)
                now = time.perf_counter()
                if r != 0:
                    on_packet(now, -1, 0, 0, 0, None, now - last)  # wait timeout marker
                    continue
                while True:
                    n = self.capture.GetNextPacketSize()
                    if n == 0:
                        break
                    data, frames, flags, devpos, qpc = self.capture.GetBuffer()
                    mono = None
                    if frames and not flags & BUF_SILENT:
                        addr = ctypes.cast(data, c_void_p).value
                        mono = self.to_mono(ctypes.string_at(addr, frames * self.fmt.nBlockAlign))
                    self.capture.ReleaseBuffer(frames)
                    t = time.perf_counter()
                    on_packet(t, frames, flags, devpos, qpc, mono, t - last)
                    last = t
        finally:
            try:
                self.client.Stop()
            except Exception:  # noqa: BLE001
                pass


class PacketStats:
    """Numbers only."""

    def __init__(self, rate: int):
        self.rate = rate
        self.packets = 0
        self.frames = 0
        self.timeouts = 0
        self.flag_counts = {"discontinuity": 0, "silent": 0, "timestamp_error": 0}
        self.size_hist: dict[int, int] = {}
        self.qpc_deltas_ms: list[float] = []
        self.arrival_deltas_ms: list[float] = []
        self.frames_per_packet_ms = 10.0
        self.devpos_nonzero = 0  # process loopback reports device position 0 every time
        self.first_qpc = self.last_qpc = None
        self.first_dev = None
        self.next_dev = None
        self.first_perf = self.last_perf = None
        self.peak = 0.0
        self.silent_packets_nonzero = 0

    def add(self, t, frames, flags, devpos, qpc, mono):
        if frames < 0:
            self.timeouts += 1
            return
        self.packets += 1
        self.frames += frames
        self.size_hist[frames] = self.size_hist.get(frames, 0) + 1
        for bit, name in ((BUF_DISCONTINUITY, "discontinuity"), (BUF_SILENT, "silent"),
                          (BUF_TIMESTAMP_ERROR, "timestamp_error")):
            if flags & bit:
                self.flag_counts[name] += 1
        if self.first_qpc is None:
            self.first_qpc, self.first_dev, self.first_perf = qpc, devpos, t
        else:
            self.qpc_deltas_ms.append((qpc - self.last_qpc) / 1e4)
            self.arrival_deltas_ms.append((t - self.last_perf) * 1e3)
            if devpos != 0:
                self.devpos_nonzero += 1
        self.last_qpc, self.last_perf, self.last_frames = qpc, t, frames
        self.next_dev = devpos + frames
        if mono is not None and len(mono):
            self.peak = max(self.peak, float(np.abs(mono).max()))

    def summary(self) -> dict:
        wall = (self.last_perf - self.first_perf) if self.first_perf is not None and self.last_perf else 0.0
        out = {"packets": self.packets, "frames": self.frames, "wait_timeouts": self.timeouts,
               "flags": self.flag_counts, "devpos_nonzero_packets": self.devpos_nonzero,
               "top_packet_sizes": sorted(self.size_hist.items(), key=lambda kv: -kv[1])[:5],
               "peak": round(self.peak, 5)}
        if self.packets > 1 and wall > 0:
            audio_s = self.frames / self.rate
            qpc_s = (self.last_qpc - self.first_qpc) / 1e7 + (self.last_frames / self.rate)
            dev_s = (self.frames - 0) / self.rate  # frames delivered, as a timeline
            out.update({
                "wall_s": round(wall, 3), "audio_s": round(audio_s, 3),
                "audio_minus_wall_ms": round((audio_s - wall) * 1000, 1),
                "frames_timeline_s": round(dev_s, 3), "qpc_span_s": round(qpc_s, 3),
                "drift_frames_vs_qpc_ppm": round((dev_s - qpc_s) / max(qpc_s, 1e-9) * 1e6, 1),
                "qpc_delta_ms": _pcts(self.qpc_deltas_ms),
                "qpc_deltas_over_15ms": sum(1 for d in self.qpc_deltas_ms if d > 15),
                "qpc_excess_ms_total": round(sum(d - self.frames_per_packet_ms for d in self.qpc_deltas_ms if d > 15), 1), "arrival_delta_ms": _pcts(self.arrival_deltas_ms),
            })
        return out


def _pcts(v: list[float]) -> dict:
    if not v:
        return {}
    a = np.asarray(v)
    return {"p50": round(float(np.percentile(a, 50)), 2), "p99": round(float(np.percentile(a, 99)), 2),
            "max": round(float(a.max()), 2), "min": round(float(a.min()), 2)}


class BandMeter:
    """Reduces mono blocks to (time, rms, magnitude at each test frequency) per ~100 ms; keeps no audio."""

    def __init__(self, rate: int, freqs, window_s: float = 0.1):
        self.rate, self.freqs = rate, list(freqs)
        self.n = int(rate * window_s)
        self.buf: list[np.ndarray] = []
        self.have = 0
        self.rows: list[tuple] = []  # (t_end, rms, mags...)
        self._win = np.hanning(self.n).astype(np.float32)
        k = np.arange(self.n, dtype=np.float64)
        self._basis = [np.exp(-2j * np.pi * f * k / rate).astype(np.complex64) for f in self.freqs]

    def feed(self, t_now: float, mono: np.ndarray) -> None:
        self.buf.append(mono)
        self.have += len(mono)
        if self.have < self.n:
            return
        a = np.concatenate(self.buf)
        self.buf, self.have = [], 0
        pos = 0
        while len(a) - pos >= self.n:
            blk = a[pos:pos + self.n]
            pos += self.n
            t_end = t_now - (len(a) - pos) / self.rate
            mags = [float(2 * abs(np.dot(blk * self._win, b)) / self._win.sum()) for b in self._basis]
            self.rows.append((t_end, float(np.sqrt(np.mean(blk * blk))), *mags))
        if pos < len(a):
            self.buf, self.have = [a[pos:]], len(a) - pos


def db(x: float) -> float:
    return round(20 * np.log10(max(x, 1e-9)), 1)


# --------------------------------------------------------------------------- tones
def play_tone(freq: float, secs: float, amp: float, stamp_file: str | None = None) -> tuple[float, float]:
    """Play a short quiet sine on the default output the way PortAudioOutput does. Returns (start, end)."""
    amp = min(abs(amp), MAX_AMP)
    secs = min(secs, MAX_TONE_S)
    import pyaudiowpatch as pyaudio

    pa = pyaudio.PyAudio()
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        dev = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        rate, ch = int(dev["defaultSampleRate"]), max(1, int(dev["maxOutputChannels"]))
        n = int(rate * secs)
        t = np.arange(n) / rate
        env = np.minimum(1.0, np.minimum(t, secs - t) / 0.05)  # 50 ms fades, no clicks
        mono = amp * env * np.sin(2 * np.pi * freq * t)
        pcm = np.repeat((mono * 32767).astype(np.int16)[:, None], ch, axis=1).tobytes()
        stream = pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, output=True,
                         output_device_index=int(dev["index"]))
        t_start = time.perf_counter()
        stream.write(pcm)
        time.sleep(0.15)
        t_end = time.perf_counter()
        stream.stop_stream()
        stream.close()
    finally:
        pa.terminate()
    if stamp_file:
        with open(stamp_file, "w") as f:
            json.dump({"start": t_start, "end": t_end, "freq": freq, "pid": os.getpid(), "ppid": os.getppid()}, f)
    return t_start, t_end


def launch_outside_tree(args: list[str]) -> dict:
    """Start a process whose parent is NOT us: Win32_Process.Create makes WmiPrvSE the parent."""
    cmd = " ".join(f'"{a}"' for a in args)
    ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
          f"@{{CommandLine='{cmd}'; CurrentDirectory='{os.path.dirname(HERE)}'}}; "
          "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($r.ProcessId)\"; "
          "'{0} {1} {2}' -f $r.ReturnValue, $r.ProcessId, $p.ParentProcessId")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
    rv, pid, ppid = (out.stdout.split() + ["?", "?", "?"])[:3]
    return {"return_value": rv, "pid": pid, "parent_pid": ppid, "our_pid": os.getpid()}


def wait_stamp(path: str, timeout: float) -> dict | None:
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    return json.load(f)
            except (ValueError, OSError):
                pass
        time.sleep(0.1)
    return None


# --------------------------------------------------------------------------- device loopback (as the product)
class DeviceLoop:
    def __init__(self, freqs):
        import pyaudiowpatch as pyaudio

        self.pa = pyaudio.PyAudio()
        d = self.pa.get_default_wasapi_loopback()
        self.name, self.rate, self.ch = d["name"], int(d["defaultSampleRate"]), int(d["maxInputChannels"])
        self.meter = BandMeter(self.rate, freqs) if freqs else None
        self.callbacks = 0
        self.frames = 0
        self.peak = 0.0
        self.t_last = None
        self.gaps_over_200ms = 0
        self.max_gap_s = 0.0
        self.first = None
        self._pyaudio = pyaudio
        self.stream = self.pa.open(format=pyaudio.paInt16, channels=self.ch, rate=self.rate, input=True,
                                   input_device_index=int(d["index"]), frames_per_buffer=self.rate // 50,
                                   stream_callback=self._cb)

    def _cb(self, data, n, ti, status):
        t = time.perf_counter()
        a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
        if self.ch > 1:
            a = a.reshape(-1, self.ch).mean(axis=1)
        self.callbacks += 1
        self.frames += n
        self.peak = max(self.peak, float(np.abs(a).max()) if len(a) else 0.0)
        if self.first is None:
            self.first = t
        if self.t_last is not None:
            g = t - self.t_last
            self.max_gap_s = max(self.max_gap_s, g)
            if g > 0.2:
                self.gaps_over_200ms += 1
        self.t_last = t
        if self.meter:
            self.meter.feed(t, a)
        return (None, self._pyaudio.paContinue)

    def close(self):
        self.stream.stop_stream()
        self.stream.close()
        self.pa.terminate()

    def summary(self) -> dict:
        wall = (self.t_last - self.first) if self.first else 0
        return {"device": self.name, "rate": self.rate, "channels": self.ch, "callbacks": self.callbacks,
                "frames": self.frames, "audio_s": round(self.frames / self.rate, 2), "wall_s": round(wall, 2),
                "gaps_over_200ms": self.gaps_over_200ms, "max_gap_s": round(self.max_gap_s, 3),
                "peak": round(self.peak, 5)}


def _thread_init():
    try:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
    except OSError:  # main thread: comtypes already made it an STA
        pass


# --------------------------------------------------------------------------- commands
def cmd_formats(_a) -> int:
    _thread_init()
    res = {}
    # GetMixFormat / GetDevicePeriod on a process-loopback client
    client, _ = activate(os.getpid())
    for name, call in (("GetMixFormat", lambda: client.GetMixFormat()),
                       ("GetDevicePeriod", lambda: client.GetDevicePeriod()),
                       ("GetBufferSize_before_init", lambda: client.GetBufferSize())):
        try:
            r = call()
            res[name] = "ok " + (f"{r.contents.nSamplesPerSec}" if hasattr(r, "contents") else str(r))
        except comtypes.COMError as e:
            res[name] = hr_hex(e)
    print("uninitialised client:", res)
    plans = [("loopback|event", FLAG_LOOPBACK | FLAG_EVENTCALLBACK),
             ("loopback|event|autoconvert", FLAG_LOOPBACK | FLAG_EVENTCALLBACK | FLAG_AUTOCONVERTPCM | FLAG_SRC_DEFAULT_QUALITY),
             ("loopback only (poll)", FLAG_LOOPBACK)]
    for plan, flags in plans:
        for name in FORMATS:
            try:
                p = ProcLoop(fmt=name, flags=flags)
                r = f"OK buffer_frames={p.buffer_frames}"
            except comtypes.COMError as e:
                r = f"Initialize {hr_hex(e)}"
            except Exception as e:  # noqa: BLE001
                r = repr(e)
            print(f"  {plan:30s} {name:14s} {r}")
    # mode INCLUDE of our own pid also activates?
    try:
        ProcLoop(mode=PROCESS_LOOPBACK_MODE_INCLUDE)
        print("  INCLUDE own pid: OK")
    except Exception as e:  # noqa: BLE001
        print("  INCLUDE own pid:", e)
    return 0


def _run_proc(seconds: float, fmt: str, stats: PacketStats, meter_freqs=None, flags=None):
    holder = {}
    stop = threading.Event()
    ready = threading.Event()

    def worker():
        _thread_init()
        try:
            p = ProcLoop(fmt=fmt, **({"flags": flags} if flags is not None else {}))
        except Exception as e:  # noqa: BLE001
            holder["error"] = e
            ready.set()
            return
        holder["p"] = p
        stats.rate = p.rate
        holder["meter"] = BandMeter(p.rate, meter_freqs) if meter_freqs else None
        ready.set()

        def on_packet(t, frames, flags_, devpos, qpc, mono, waited):
            stats.add(t, frames, flags_, devpos, qpc, mono)
            if mono is not None and holder["meter"]:
                holder["meter"].feed(t, mono)

        p.pump(stop, on_packet)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    ready.wait(10)
    return th, stop, holder


def cmd_capture(a) -> int:
    stats = PacketStats(48000)
    th, stop, holder = _run_proc(a.seconds, a.format, stats)
    time.sleep(a.seconds)
    stop.set()
    th.join(5)
    if "error" in holder:
        print("FAILED:", holder["error"])
        return 1
    p = holder["p"]
    print("format:", a.format, "rate", p.rate, "ch", p.channels, "buffer_frames", p.buffer_frames, p.info)
    print(json.dumps(stats.summary(), indent=1))
    return 0


def _band_report(label: str, meter_rows, freqs, phases) -> dict:
    """phases: {freq: (t0, t1)}. Peak magnitude in each tone's own window and elsewhere."""
    rows = np.array(meter_rows, dtype=np.float64) if meter_rows else np.zeros((0, 2 + len(freqs)))
    out = {}
    for i, f in enumerate(freqs):
        t0, t1 = phases[f]
        inside = rows[(rows[:, 0] >= t0) & (rows[:, 0] <= t1 + 0.3)]
        outside_mask = np.ones(len(rows), dtype=bool)
        for (a0, a1) in phases.values():
            outside_mask &= ~((rows[:, 0] >= a0) & (rows[:, 0] <= a1 + 0.3))
        outside = rows[outside_mask]
        pk_in = float(inside[:, 2 + i].max()) if len(inside) else 0.0
        floor = float(np.median(outside[:, 2 + i])) if len(outside) else 0.0
        peak_out = float(outside[:, 2 + i].max()) if len(outside) else 0.0
        out[f] = {"peak_in_window_dBFS": db(pk_in), "median_elsewhere_dBFS": db(floor),
                  "peak_elsewhere_dBFS": db(peak_out), "above_floor_dB": round(db(pk_in) - db(max(peak_out, floor)), 1)}
    return out


def cmd_tones(a) -> int:
    os.makedirs(LAB, exist_ok=True)
    f_own, f_out, f_child = 440.0, 660.0, 880.0
    freqs = [f_own, f_out, f_child]
    amp = min(a.amp, MAX_AMP)
    secs = min(a.tone_s, MAX_TONE_S)
    stats = PacketStats(48000)
    dev = DeviceLoop(freqs)
    th, stop, holder = _run_proc(0, a.format, stats, freqs)
    if "error" in holder:
        print("process loopback FAILED:", holder["error"])
        dev.close()
        return 1
    print("process loopback:", a.format, holder["p"].info)
    print("device loopback:", dev.name, dev.rate, "Hz", dev.ch, "ch")
    phases = {}
    report = {"amp": amp, "tone_s": secs}
    time.sleep(1.5)
    # A: this process
    s, e = play_tone(f_own, secs, amp)
    phases[f_own] = (s, e)
    time.sleep(1.0)
    # B: a process outside our tree
    stamp_b = os.path.join(LAB, "tone_outside.json")
    if os.path.exists(stamp_b):
        os.remove(stamp_b)
    report["outside_launch"] = launch_outside_tree(
        [sys.executable, os.path.abspath(__file__), "play", "--freq", str(f_out), "--secs", str(secs),
         "--amp", str(amp), "--stamp", stamp_b])
    st = wait_stamp(stamp_b, 20)
    phases[f_out] = (st["start"], st["end"]) if st else (0, 0)
    report["outside_played"] = bool(st)
    time.sleep(1.0)
    # C: a child in our tree
    stamp_c = os.path.join(LAB, "tone_child.json")
    if os.path.exists(stamp_c):
        os.remove(stamp_c)
    child = subprocess.Popen([sys.executable, os.path.abspath(__file__), "play", "--freq", str(f_child),
                              "--secs", str(secs), "--amp", str(amp), "--stamp", stamp_c])
    st = wait_stamp(stamp_c, 20)
    phases[f_child] = (st["start"], st["end"]) if st else (0, 0)
    report["child_played"] = bool(st)
    child.wait(10)
    time.sleep(1.0)
    stop.set()
    th.join(5)
    dev.close()
    report["process_loopback"] = _band_report("proc", holder["meter"].rows, freqs, phases)
    report["device_loopback"] = _band_report("dev", dev.meter.rows, freqs, phases)
    report["process_packets"] = stats.summary()
    report["device_summary"] = dev.summary()
    report["freq_roles"] = {"440": "this process", "660": "outside our tree", "880": "child of this process"}
    print(json.dumps(report, indent=1))
    with open(os.path.join(LAB, "tones_result.json"), "w") as f:
        json.dump(report, f, indent=1)
    return 0


def cmd_tree(a) -> int:
    """Which launch styles end up inside the excluded tree? One 880 Hz tone per variant."""
    os.makedirs(LAB, exist_ok=True)
    stats = PacketStats(48000)
    th, stop, holder = _run_proc(0, "f32_48000_2", stats, [880.0])
    if "error" in holder:
        print("FAILED", holder["error"])
        return 1
    base = getattr(sys, "_base_executable", sys.executable)
    site = os.path.join(os.path.dirname(os.path.dirname(sys.executable)), "Lib", "site-packages")
    out = {}
    for name, exe, env in (("venv python.exe (launcher + base python)", sys.executable, None),
                           ("base python.exe direct", base, dict(os.environ, PYTHONPATH=site)),
                           ("base python -> base python (grandchild)", base, dict(os.environ, PYTHONPATH=site, SPIKE_RELAY="1"))):
        stamp = os.path.join(LAB, "tone_tree.json")
        if os.path.exists(stamp):
            os.remove(stamp)
        time.sleep(1.0)
        t_before = time.perf_counter()
        relay = ["-c", "import subprocess,sys;sys.exit(subprocess.call([sys.executable]+sys.argv[1:]))"]             if env and env.get("SPIKE_RELAY") else []
        pr = subprocess.Popen([exe, *relay, os.path.abspath(__file__), "play", "--freq", "880", "--secs", "2",
                               "--amp", "0.02", "--stamp", stamp], env=env)
        st = wait_stamp(stamp, 20)
        pr.wait(10)
        time.sleep(0.8)
        rows = np.array(holder["meter"].rows)
        win = rows[(rows[:, 0] >= st["start"]) & (rows[:, 0] <= st["end"] + 0.3)] if st else rows[:0]
        out[name] = {"popen_pid": pr.pid, "player_pid": st and st["pid"], "player_parent": st and st["ppid"],
                     "our_pid": os.getpid(), "peak_880_dBFS_in_process_loopback": db(float(win[:, 2].max())) if len(win) else None}
    stop.set()
    th.join(5)
    print(json.dumps(out, indent=1))
    return 0


def cmd_play(a) -> int:
    play_tone(a.freq, a.secs, a.amp, a.stamp)
    return 0


def cmd_soak(a) -> int:
    os.makedirs(LAB, exist_ok=True)
    logp = os.path.join(LAB, "soak.log")
    log = open(logp, "a", buffering=1)

    def say(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        log.write(line + "\n")

    stats = PacketStats(48000)
    dev = DeviceLoop(None)
    th, stop, holder = _run_proc(0, a.format, stats)
    if "error" in holder:
        say(f"FAILED {holder['error']}")
        return 1
    t0 = time.perf_counter()
    w0 = time.time()
    say(f"soak start {a.minutes} min, format {a.format}")
    prev = (0, 0, 0)
    end = t0 + a.minutes * 60
    next_log = t0 + 60
    while time.perf_counter() < end:
        time.sleep(0.5)
        now = time.perf_counter()
        if now >= next_log:
            next_log += 60
            s = stats
            wall = now - t0
            audio = s.frames / s.rate
            say(f"t={wall:7.1f}s proc: pk={s.packets} fr={s.frames} audio-wall={audio - wall:+.3f}s "
                f"silent={s.flag_counts['silent']} disc={s.flag_counts['discontinuity']} "
                f"tserr={s.flag_counts['timestamp_error']} devnz={s.devpos_nonzero} timeouts={s.timeouts} "
                f"peak={s.peak:.4f} | dev: cb={dev.callbacks} fr={dev.frames} "
                f"audio-wall={dev.frames / dev.rate - (now - t0):+.3f}s gaps>200ms={dev.gaps_over_200ms} "
                f"maxgap={dev.max_gap_s:.2f}s peak={dev.peak:.4f} | wall-clock jump={(time.time() - w0) - wall:+.2f}s")
            s.peak = 0.0
            dev.peak = 0.0
    stop.set()
    th.join(5)
    dev.close()
    final = {"process": stats.summary(), "device": dev.summary()}
    say("FINAL " + json.dumps(final))
    log.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("formats").set_defaults(fn=cmd_formats)
    c = sub.add_parser("capture")
    c.add_argument("--seconds", type=float, default=10)
    c.add_argument("--format", default="f32_48000_2", choices=list(FORMATS))
    c.set_defaults(fn=cmd_capture)
    t = sub.add_parser("tones")
    t.add_argument("--amp", type=float, default=0.02)
    t.add_argument("--tone-s", type=float, default=2.5)
    t.add_argument("--format", default="f32_48000_2", choices=list(FORMATS))
    t.set_defaults(fn=cmd_tones)
    s = sub.add_parser("soak")
    s.add_argument("--minutes", type=float, default=20)
    s.add_argument("--format", default="f32_48000_2", choices=list(FORMATS))
    s.set_defaults(fn=cmd_soak)
    sub.add_parser("tree").set_defaults(fn=cmd_tree)
    p = sub.add_parser("play")  # internal: helper process that plays one tone
    p.add_argument("--freq", type=float, required=True)
    p.add_argument("--secs", type=float, default=2.5)
    p.add_argument("--amp", type=float, default=0.02)
    p.add_argument("--stamp")
    p.set_defaults(fn=cmd_play)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
