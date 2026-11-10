"""Incremental reading of a growing JSONL file (plan 4.4, "Tailing").

``JsonlTail(path).poll()`` returns the events appended since the previous call. It keeps a
byte offset, reads only the new bytes in binary, splits on ``\\n`` and holds back a trailing
partial line (a writer may be half-way through a line) until its newline arrives.

* Missing file: nothing is returned, no error. When the file appears it is read from the start.
* Shrunk or replaced file (the final pass was re-run, a file was rewritten): the offset goes
  back to 0 and ``TailPoll.reset`` is True on that poll, so the owner clears what it built
  from the old content. The events in that same poll come from the new content.
* A complete line that is not valid JSON, or not a JSON object, is skipped (logged).
* ``poll(max_events=N)`` returns at most N events and keeps the rest for the next call
  (``has_more`` says so), so opening a long meeting never freezes the UI.

Not thread-safe: use one tail per file from one thread (the Tk thread).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class TailPoll:
    events: list[dict] = field(default_factory=list)
    reset: bool = False  # the file shrank, was replaced or vanished: rebuild from these events

    def __iter__(self):
        return iter(self.events)

    def __len__(self) -> int:
        return len(self.events)

    def __bool__(self) -> bool:
        return bool(self.events) or self.reset


class JsonlTail:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._offset = 0  # bytes of the file read so far (including those held in _buf)
        self._buf = b""  # read but not yet returned: complete lines past the limit + a partial line
        self._ident: tuple[int, int] | None = None  # (st_dev, st_ino) of the file we are following
        self._seen = False  # we have read from a file at least once

    @property
    def offset(self) -> int:
        """Bytes read from the file so far."""
        return self._offset

    @property
    def has_more(self) -> bool:
        """Complete lines are waiting because the last poll hit its ``max_events`` limit."""
        return b"\n" in self._buf

    def restart(self) -> None:
        """Forget everything and read from the start on the next poll (no reset is signalled)."""
        self._offset, self._buf, self._ident, self._seen = 0, b"", None, False

    def poll(self, max_events: int | None = None) -> TailPoll:
        out = TailPoll()
        try:
            st = os.stat(self.path)
        except OSError:
            if self._seen:  # was there, now gone: treat as a rewrite in progress
                self.restart()
                out.reset = True
            return out
        ident = (st.st_dev, st.st_ino) if st.st_ino else None
        if self._seen and (st.st_size < self._offset
                           or (ident is not None and self._ident is not None and ident != self._ident)):
            self.restart()
            out.reset = True
        if st.st_size > self._offset:
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(self._offset)
                    data = fh.read(st.st_size - self._offset)
            except OSError:
                return out
            self._buf += data
            self._offset += len(data)
        self._ident, self._seen = ident, True

        pos = 0
        while max_events is None or len(out.events) < max_events:
            cut = self._buf.find(b"\n", pos)
            if cut < 0:
                break
            raw, pos = self._buf[pos:cut].strip(), cut + 1
            if not raw:
                continue
            try:
                event = json.loads(raw.decode("utf-8"))
            except ValueError:
                log.warning("Skipping unreadable line in %s", self.path)
                continue
            if isinstance(event, dict):
                out.events.append(event)
        self._buf = self._buf[pos:]
        return out
