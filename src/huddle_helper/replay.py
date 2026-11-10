"""``huddle-helper tray --replay <meeting folder>`` (dev aid): run a kept recording through the tray
app's real recorder, as if it were a call, so the Transcript window can be exercised against real
audio without a meeting.

The audio of ``<folder>\\audio\\mic.flac`` and ``system.flac`` is fed in real time through
``FileCapture``. The recording lands in a new, normal meeting folder under the configured meetings
folder (override it with ``--out``); the replayed folder is only read, and replaying into a place
inside it is refused. No Tk here: ``next_step`` is the whole decision logic of the unattended mode
(``--auto``), driven by a timer in ``tray.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .audio.filesource import FileCapture

SOURCES = ("mic", "system")


def replay_files(folder: Path) -> dict[str, Path]:
    """The stream files of a kept meeting. Raises FileNotFoundError when it has no audio."""
    files = {src: folder / "audio" / f"{src}.flac" for src in SOURCES if (folder / "audio" / f"{src}.flac").exists()}
    if not files:
        raise FileNotFoundError(f"No audio\\mic.flac or audio\\system.flac in {folder}")
    return files


def check_output_dir(replay_folder: Path, meetings_dir: Path) -> None:
    """Refuse to record inside the folder being replayed."""
    src, out = replay_folder.resolve(), meetings_dir.resolve()
    if out == src or src in out.parents:
        raise ValueError(f"The meetings folder {out} is inside the replayed meeting {src}; "
                         "choose another with --out.")


@dataclass
class Replay:
    folder: Path
    files: dict[str, Path]
    title: str = "replay"
    auto: bool = False  # start now, stop when the audio ends, quit after the final pass
    keep_open: bool = False  # with auto: leave the app (and the window) running at the end
    start_muted: bool = False  # start with the mic muted (L24), to exercise that path unattended
    started: bool = False
    stopped: bool = False
    capture: FileCapture | None = field(default=None, repr=False)

    @classmethod
    def create(cls, folder: Path, meetings_dir: Path, *, title: str = "replay", auto: bool = False,
               keep_open: bool = False, start_muted: bool = False) -> "Replay":
        folder = Path(folder)
        check_output_dir(folder, meetings_dir)
        return cls(folder, replay_files(folder), title, auto, keep_open, start_muted)

    def capture_factory(self, **kw) -> FileCapture:
        """Stands in for ``WasapiCapture`` (``Components.capture_factory``)."""
        self.capture = FileCapture(kw["on_audio"], dict(self.files), realtime=True)
        return self.capture

    @property
    def audio_finished(self) -> bool:
        return self.capture is not None and self.capture.finished.is_set()


def next_step(*, phase: str, started: bool, stopped: bool, audio_finished: bool, finalizing: bool,
              finalize_queue: int) -> str | None:
    """What the unattended replay does next: "start", "stop", "quit" or None (wait).

    * start once the app is ready;
    * stop once the file capture has delivered all audio;
    * quit once the app is back to ready after stopping and the after-Stop queue is empty.
    """
    if not started:
        return "start" if phase == "ready" else None
    if not stopped:
        return "stop" if audio_finished and phase == "recording" else None
    if phase == "ready" and not finalizing and finalize_queue <= 0:
        return "quit"
    return None
