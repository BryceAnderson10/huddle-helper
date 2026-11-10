"""One meeting as the Transcript window follows it (plan 4.4, "Tailing"). No Tk imports.

``MeetingSession`` owns a ``TranscriptModel`` plus the ``JsonlTail`` readers that keep it current
while the recorder and the after-Stop pass write the meeting's files. The window calls

    ops = session.poll()          every 250 ms: new transcript / tag / mark lines (capped per call)
    slow = session.poll_slow()    every 1 s: meta.json and speakers.json if they changed; DONE

and replays the returned ops on the view. Nothing here runs on a recorder thread, and only the
files the downstream app reads are touched (read-only).

A meeting that already has ``DONE`` opens on its final file; otherwise on the live file
(provisional), and ``poll_slow`` swaps to the final file the moment ``DONE`` appears. The swap drains
the live tail first so the changed-line comparison (L3) sees every live line.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .meeting import MeetingFolder
from .tail import JsonlTail
from .transcript_model import Op, TranscriptModel, _read_conflicts

log = logging.getLogger(__name__)

MAX_EVENTS_PER_TICK = 200


@dataclass
class SlowPoll:
    ops: list[Op] = field(default_factory=list)
    swapped: bool = False  # the view switched from the live to the final transcript
    changed: frozenset[tuple[str, int]] = frozenset()  # LineRow.key values to highlight after a swap


def _mtime(path) -> float | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class MeetingSession:
    def __init__(self, folder: MeetingFolder):
        self.folder = folder
        self.model = TranscriptModel(folder)
        self._meta_mtime: float | None = None
        self._speakers_mtime: float | None = None
        self._tags = JsonlTail(folder.tags_path)
        self._marks = JsonlTail(folder.marks_path)
        self._live = JsonlTail(folder.live_path)
        self._final = JsonlTail(folder.final_path)
        self._read_meta()
        if folder.is_done and folder.final_path.exists():
            self.model.final = True
            self._read_speakers()

    # -- state ------------------------------------------------------------
    @property
    def meeting_id(self) -> str:
        return self.folder.meeting_id

    @property
    def is_final(self) -> bool:
        return self.model.final

    @property
    def has_more(self) -> bool:
        """The last poll hit its cap: poll again soon."""
        return (self._final if self.model.final else self._live).has_more

    # -- fast poll ----------------------------------------------------------
    def poll(self, max_events: int = MAX_EVENTS_PER_TICK) -> list[Op]:
        ops: list[Op] = []
        tail = self._final if self.model.final else self._live
        got = tail.poll(max_events)
        if got.reset:
            ops += self.model.reset_transcript()
        ops += self.model.apply_transcript(got.events)
        tags = self._tags.poll(max_events)
        if tags.reset:  # tags are rebuilt from scratch: rare, so just reload them all
            self._tags.restart()
            tags = self._tags.poll()
        if tags.events:
            ops += self.model.apply_tags(tags.events)
        marks = self._marks.poll(max_events)
        if marks.events:
            ops += self.model.apply_marks(marks.events)
        return ops

    # -- slow poll ----------------------------------------------------------
    def poll_slow(self) -> SlowPoll:
        out = SlowPoll()
        if not self.model.final and self.folder.is_done and self.folder.final_path.exists():
            self._swap(out)
        self._read_meta(out.ops)
        if self.model.final and not out.swapped:
            self._read_speakers(out.ops)
        return out

    def _swap(self, out: SlowPoll) -> None:
        # Everything the live file holds, so the comparison sees all of it.
        while True:
            got = self._live.poll(10_000)
            if got.reset:
                self.model.reset_transcript()
            if not got.events:
                break
            self.model.apply_transcript(got.events)
        try:
            res = self.model.switch_to_final()
        except (OSError, ValueError):
            log.exception("Could not read the final transcript of %s", self.meeting_id)
            return
        self._final = JsonlTail(self.folder.final_path)
        self._final.poll()  # already in the model: start at the end
        self._speakers_mtime = _mtime(self.folder.speakers_path)
        out.ops += res.ops
        out.swapped = True
        out.changed = res.changed

    def _read_meta(self, ops: list[Op] | None = None) -> bool:
        mtime = _mtime(self.folder.meta_path)
        if mtime is None or mtime == self._meta_mtime:
            return False
        try:
            meta = self.folder.read_meta()
        except (OSError, ValueError):
            return False  # being replaced right now: try again next time
        self._meta_mtime = mtime
        new = self.model.set_meta(meta)
        if ops is not None:
            ops += new
        return True

    def _read_speakers(self, ops: list[Op] | None = None) -> bool:
        mtime = _mtime(self.folder.speakers_path)
        if mtime == self._speakers_mtime:
            return False
        self._speakers_mtime = mtime
        new = self.model.set_speakers(self.folder.read_speakers(), _read_conflicts(self.folder))
        if ops is not None:
            ops += new
        return True
