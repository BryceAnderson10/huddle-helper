# Process-loopback capture (clips never captured)

The system stream stays on device loopback. Huddle Helper does not switch to WASAPI process
loopback to keep its own clips out of the recording (L17 layer 1, WP10).

## Why this is out of scope

Process loopback would stop a clip the app plays (click to hear, Play from here) from being
captured at all. Today the clip is captured and then removed: the playback-window rules (L17
layers 2 and 3) drop it in the transcribe loop before it can become a transcript line or a
speaker's voice. The build was held until user testing showed whether that is enough, and
whether device loopback copes with a device switch. Both held up on the target laptop:

- **No clip leaks reported.** Click to hear and Play from here were used on real calls, with and
  without a headset (#8, #10). No clip turned up as a line or a voice. A short run that played a
  clip while recording dropped no line and added none.
- **A device switch is handled.** In a real call a headset was plugged in mid-call; Windows made it
  the default, the capture re-opened both streams within 0.4 s, `meta.json` logged one gap per
  stream, and the far-end audio carried on (#6).

Against that, process loopback adds a second capture path to the one part of the app that must
never lose audio:

- about 300–400 lines: a new `audio\procloop.py` with hand-declared COM interfaces, and changes to
  `capture.py` and the device watcher;
- device loopback must stay as the fallback (Windows builds below 20348, activation failures), so
  both paths need testing from then on;
- untested ground: Bluetooth headsets switching profile mid-call, the packaged exe, and tree
  exclusion that reached only direct child processes on the test build.

The call hearing a clip through the laptop's speakers is not a capture problem, and process
loopback would not fix it; the headphone warning covers that.

## If this comes back

Reopen this if a real call shows a played clip as a transcript line or a speaker's voice, or a
device switch losing far-end audio. The spike's findings, measurements and the build outline are
in `docs/WP10_SPIKE.md` (sections 8–10). `tools/process_loopback_spike.py` still runs; its
pre-build checks are in section 10.

## Prior requests

- #19: "Decide after user testing: build process-loopback capture (WP10)?"
