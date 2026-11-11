# Hearing audio older than 10 minutes while recording

While a meeting is recording, click to hear (and Play from here, Hear this voice) only reaches
the last 10 minutes. An older line shows "Older than the last 10 minutes. Available after
Stop." This stays as it is.

## Why this is out of scope

During a call, the reason to replay a line is to catch something just said: a name, a number,
a word the transcript got wrong. The in-memory buffer (`playback.RING_SECONDS`, 600 s per
stream) covers that. Anything older is one Stop away: after Stop every line plays from the kept
audio files.

Reaching further back while recording means reading the `audio\*.flac` files the recorder is
still writing. The WP7 spike showed a second read-only handle works on Windows, but it brings
edge cases into the one part of the app that must never lose audio:

- the file reports its length as unknown while open, so the reader has to take it from the
  meeting clock;
- reads near the writer's position can raise `LibsndfileError` and must be caught;
- the alternative, 5-minute chunk files, changes the documented `audio\` layout and about 8
  places that read it.

That is a lasting cost in the capture path for a small gain in a rare case.

## If this comes back

The spike's findings are in TODO.md under "Decisions waiting" (WP4–WP7). If it is ever built,
the recommended route was the second read-only handle, used only for lines older than the
in-memory window, with no chunk files.

## Prior requests

- #17: "Decide: hearing audio older than 10 minutes while recording"
