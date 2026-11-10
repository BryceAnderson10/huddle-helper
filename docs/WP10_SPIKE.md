# WP10 spike: process loopback capture

Question: can the system stream be captured with WASAPI process loopback, excluding Huddle
Helper's own audio, through comtypes, so a clip the app plays never enters the recording
(L17 layer 1)? Tool: `tools/process_loopback_spike.py` (comtypes + ctypes + numpy; pyaudiowpatch
only to play the test tones and for the device-loopback comparison, as the product does).
The product's capture path is unchanged. No audio was written to disk; only numbers were kept.

Machine: Windows 11 Pro build 26300, Python 3.14.10, comtypes 1.4.17, default output
"Headset Earphone (HyperX 7.1 Audio)" at 48000 Hz, 2 channels.

## 1. Does it work through comtypes?

Yes, on the first attempt, with no native code.

- `ActivateAudioInterfaceAsync` from `Mmdevapi.dll` is called through ctypes with device path
  `VAD\Process_Loopback`, IID of `IAudioClient`, and a 24-byte `PROPVARIANT` (`VT_BLOB`, size 12)
  pointing to `AUDIOCLIENT_ACTIVATION_PARAMS` (type 1 = process loopback, target PID, mode
  `EXCLUDE_TARGET_PROCESS_TREE` = 1).
- The completion handler is a `comtypes.COMObject` implementing
  `IActivateAudioInterfaceCompletionHandler` and `IAgileObject` (the second is required by the API; it
  has no methods). comtypes handles the QueryInterface for it. The callback arrives on another
  (MTA) thread; calling `GetActivateResult` inside it works. Activation takes 1 to 3 ms.
- `IAudioClient` and `IAudioCaptureClient` are declared with `COMMETHOD` (about 60 lines). The returned
  client is used from our own capture thread (`CoInitializeEx` MTA there); no marshalling problems.
- Minimum Windows per Microsoft: build 20348 (the docs list "Windows 10 Build 20348" for
  `AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`). Only tested on build 26300.
- No HRESULT failures were seen anywhere in the final tool. (Development hit one Python-side error,
  `CoInitializeEx` on the main thread: comtypes already makes it an STA. The capture runs in its own
  thread, so nothing changes for the product.)

## 2. Formats and resampling

- `GetMixFormat` and `GetDevicePeriod` on this client return `E_NOTIMPL` (0x80004001), as documented
  for process loopback. You must pass your own format to `Initialize`.
- `Initialize(SHARED, LOOPBACK | EVENTCALLBACK, 200000 (20 ms), 0, fmt)` was accepted for every
  format tried, with and without `AUTOCONVERTPCM | SRC_DEFAULT_QUALITY`, and also without
  `EVENTCALLBACK` (polling): float32 48000 stereo/mono, int16 48000 stereo, int16 44100 stereo,
  int16 and float32 16000 mono, int16 8000 mono.
- So the engine converts for us: `int16, 16000 Hz, mono` is accepted and delivers 160-frame packets
  (10 ms). That would remove the downmix and the soxr resampler for the system stream. Not
  checked: the quality of the engine's sample-rate converter against soxr (a transcript-quality
  question: compare final-pass output on the same recording). Safe default: ask for float32 48000
  stereo (what the engine mixes in) and keep the existing downmix and soxr path.
- Packets are always 480 frames at 48 kHz (10 ms), regardless of the 20 ms buffer requested.
- Mode `INCLUDE` with our own PID also activates (not needed).

## 3. Exclusion and inclusion (tone test)

Three tones at peak 0.02 of full scale, 2.5 s each, on the default output through
pyaudiowpatch (as `PortAudioOutput` does). Numbers are the peak band magnitude (Hann-windowed,
100 ms blocks) at the tone frequency during its window, in dBFS. Elsewhere in the capture the band was at
the floor of the arithmetic (-180 dBFS, i.e. exact digital zero) in both captures.

| Tone | Played by | Device loopback (as product) | Process loopback, exclude our PID |
| --- | --- | --- | --- |
| 440 Hz | this process | -34 dBFS (present) | **-180 dBFS (absent)** |
| 660 Hz | separate process outside our tree | -34 dBFS | -34 dBFS (present) |
| 880 Hz | `subprocess.Popen` of `.venv\Scripts\python.exe` | -34 dBFS | -34 dBFS (present, see below) |

-34 dBFS is exactly 0.02, so neither path attenuated the tone (the output volume does not scale
loopback). The exclusion is complete: not a reduction, an exact zero.

"Outside our tree": started with `Invoke-CimMethod Win32_Process.Create`, which makes `WmiPrvSE.exe`
the parent (verified: parent PID 21248, ours 25804). Any launch whose parent chain does not include us
works the same way; `schtasks` or `explorer`-style launches would too.

### Process tree depth (surprising)

A second run (`tools/process_loopback_spike.py tree`, one 2 s 880 Hz tone each):

| Player | Relationship to us | In process loopback |
| --- | --- | --- |
| base `python.exe` started directly by us | direct child | **excluded** (-180) |
| `.venv\Scripts\python.exe` (a launcher that starts the real interpreter) | child of our child, i.e. grandchild | **present** (-34) |
| base python started by a base python we started | grandchild | **present** (-34) |

So with this build, "exclude target process tree" removed the target and its direct children but
not grandchildren. This is a measured result on one build, not a documented rule; the documentation says
only "the process and its child processes". Consequence: anything that plays audio from a
grandchild of Huddle Helper would be captured.

## 4. Timing, clock and silence (matters for the meeting clock and the VAD)

Process loopback, 10 s run: 1000 packets of 480 frames, 0 wait timeouts, no discontinuity,
silent, or timestamp-error flags; audio length minus wall clock +10 ms (one packet of start-up
offset). QPC positions step exactly 10.00 ms; packet arrival p50 10.06 ms, p99 10.9 ms, max 21.8 ms.
The device position field is 0 in every packet (unusable); the QPC position is usable.

**Silence:** with nothing playing the client keeps delivering a packet every 10 ms, containing
exact zeros, without the SILENT flag. The device loopback the product uses, in the same run, delivered
453 callbacks for 11.7 s of wall time (9.06 s of audio) with two gaps over 200 ms (longest 1.66 s):
it delivers nothing during silence, which is why `StreamAligner` pads silence on a tick.

Meaning for the product:

- The shared meeting clock gets simpler: for the system stream, samples delivered equal elapsed time, so
  `StreamAligner`'s gap padding and idle tick would rarely fire. They must stay, for device loss
  and for any pause of the client.
- The VAD sees real zeros instead of padded zeros: no change in behaviour. The stall watcher in `capture.py` already
  ignores the system stream ("loopback legitimately goes quiet"); with process loopback a stall of
  this stream becomes a real fault signal and could be watched like the mic.
- During a 13.9 s test run (with tones and process launches) one packet had a QPC step of 23 ms instead
  of 10 ms (no flag set), 13 ms excess in total, across the whole run. Whether the engine dropped audio
  there or only the stamp moved is not known; the aligner's 200 ms gap threshold would not react.
  The 20-minute run below counts these.

## 5. Soak (unattended)

One 20-minute run (13:28 to 13:48, `soak.log` under `lab/wp10/`, statistics only), process loopback
(float32 48000 stereo, exclude own PID) and a product-style device loopback side by side. The machine was
silent the whole time (nothing was playing; peak level 0.0000 in every minute), so this run is a
silence soak only. It did not cover continuous playback.

- Process loopback: 120,014 packets, all 480 frames; 57,606,720 frames = 1200.14 s of audio for 1200.13 s of
  wall clock (arrival-time difference never more than 12 ms over the run, no trend). 0 wait timeouts, 0
  discontinuity, 0 silent, 0 timestamp-error flags. QPC step exactly 10.00 ms every time (min = max), 0 steps over
  15 ms. The QPC position is therefore a counter of delivered frames, not an independent clock reading, so the
  "drift 0 ppm against QPC" is by construction. Drift against `time.perf_counter()` is the arrival-time figure
  above (under 12 ms in 20 minutes, below the measurement noise).
- Device loopback (product style): 0 callbacks in 20 minutes. Nothing at all arrives while nothing plays,
  so the system stream is entirely `StreamAligner` padding in that case.
- No wall-clock jump (no sleep or suspend) during the run.
- The 23 ms QPC step in the 13.9 s tone run (section 4) did not recur in the silent soak. It happened while
  processes were launched and tones played; not explained.

## 6. Device switch (not run; reasoning only)

Process loopback is activated on a virtual device (`VAD\Process_Loopback`), not on a render endpoint. It
captures the mixed render streams of the other processes. The expectation from the API design is that a
change of the default output does not stop or re-bind the capture, because there is no endpoint to
invalidate; streams of other applications move to the new device and stay in the capture. No
`AUDCLNT_E_DEVICE_INVALIDATED` is expected. The existing watcher would still re-bind the
mic on a default-device change, so the system stream would be re-opened needlessly unless
the product stops re-opening it. This is expected, not observed. A person must test it
(section 9).

## 7. Packaged app and PyInstaller

- The packaged app is a PyInstaller onedir build (`packaging/huddle_helper.spec`, `HuddleHelper.exe`) and
  the source has no `subprocess` or `multiprocessing` use beyond `freeze_support()`. The process that
  plays clips (`PortAudioOutput`) is the same process that captures, so `os.getpid()` is the right target and
  nothing else in the product plays audio.
- Onedir has no bootstrap parent. (Had it been onefile, the extracting bootloader is the parent of the Python
  process; excluding the Python PID would still be right, since the audio comes from it.)
- The tree-depth result matters for helpers only: a future helper process that plays sound must be a
  direct child of the app (or its own sound would be captured), and a grandchild would not be excluded.
  Running from a venv starts the interpreter through a launcher, so a helper started from a venv-run app
  is a grandchild of the launcher; this only matters for development runs.
- comtypes is already a dependency and is collected by `packaging/huddle_helper.spec`. `Mmdevapi.dll` is a system DLL. No
  new runtime dependency and no extra PyInstaller hook were needed in the spike (the spike was only run
  from source, not from the frozen exe; not verified).
- Excluding the whole tree also excludes other audio the app might one day play (alerts); that is the
  intent.

## 8. What would change in the product

Rough size: one new module plus edits, about 300 to 400 lines and tests, 1 to 2 days of work plus checks.

- New `src/huddle_helper/audio/procloop.py`: the COM declarations, activation, event-driven capture thread
  (port of the tool, about 200 lines).
- `capture.py`: `_open` creates a process-loopback system stream when available (feeding the same
  `StreamAligner`); the mic stays on PortAudio. `_Stream.callback`'s downmix/resample is reused; `output_device`
  (the user's playback device choice) no longer selects the system stream, because process loopback hears the
  mix of all devices. The user-facing "Playback device" setting would then only matter for the fallback.
- The watcher: keep re-binding on default **mic** changes only; stop re-opening the system stream for
  output changes (if the check in section 9 confirms it survives).
- Clip playback (`playback.py`) and the playback-window text matching (L17 layers 2 and 3) stay as they
  are as the fallback, and as protection for the case in section 3 (grandchild) and for the call hearing the
  clip (that is not capture-related).
- `meta.json` should record which capture method was used per recording, so an old-style recording
  and a process-loopback one can be told apart when debugging.

## 9. Risks and fallback

- Windows builds without the API (before build 20348, e.g. older Windows 10): `ActivateAudioInterfaceAsync` on
  `VAD\Process_Loopback` fails (documented minimum; the exact HRESULT on an old build was not observed).
  Fallback: the current device loopback and playback-window removal, chosen at Start (try to activate; on any
  failure use the old path and tell the user once).
- The tree-depth behaviour above is undocumented; do not rely on it for helpers.
- System-wide effect on other processes: none. Nothing is injected; the capture is a read of the mix.
- Exclusive-mode applications bypass the engine mix and are not captured by any WASAPI loopback (same as today).
- Some protected-content streams are not capturable (same as today).
- Echo: excluding our own clips does not stop a call (Teams, Zoom) from hearing them through speakers; the
  existing warning in L17 stays.
- Skew between mic and system streams: both are stamped from the same QPC; the process-loopback QPC
  position can be used instead of arrival time, which is steadier than the PortAudio callback time.
- Process loopback is a newer, less used path than device loopback. Behaviour with Bluetooth headsets (the
  A2DP/HFP profile switch during a call changes the render device) is unknown.

## 10. Recommendation

Build it, as WP10 proper, with the device-loopback path kept as the fallback. The API works through
comtypes with no native code, excludes our own sound completely (exact zero in the capture, against full
level in device loopback), still hears other applications, delivers a steady 10 ms packet clock including
through silence, and accepts the app's own format. The cost is moderate and mostly in `capture.py`.

Check first (needs a person; none of these was run):

1. A 30-minute capture during a real call (Teams or Zoom), with the app playing a clip in the middle: the call's
   far-end audio must appear in the system stream and the clip must not. Compare drift against the mic.
2. Device switch: change the default output (speakers to headset and back) during a capture. Expect the capture
   to continue with no re-open; confirm there is no gap and no flag, and that far-end audio is still captured on
   the new device.
3. Bluetooth headset: connect during a call, and check the profile switch (A2DP to HFP).
4. Exclusive-mode apps (some conferencing clients, DAWs): check whether anything they play is missed.
5. Another Windows build (a Windows 10 22H2 or LTSC machine, if any are in use): confirm the activation
   failure code and that the fallback works.
6. The packaged `HuddleHelper.exe` (the spike ran from source only).
7. If the engine's 16 kHz mono conversion is wanted instead of soxr: a transcript-quality comparison.
8. Whether the 23 ms QPC step seen once is a dropped-audio event; the soak counts these but a real call is
   the real test.
