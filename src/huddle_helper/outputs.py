"""Which output device clips play on (LIVE_VIEW L19, "Play clips on"). No Tk, no device opened here.

The choice is stored by *name* (``Settings.clip_output_device``; ``None`` = the Windows default output),
never by index: PortAudio indices change when a device is plugged in or removed. When the saved name is
not found any more the clip plays on the default output instead (``Pick.fell_back``) and nothing breaks.

This is separate from ``Settings.output_device``, which names the device whose sound is *captured* by
loopback ("Meeting audio from"). Playing a clip on a different device than the call's does not change
what is captured; playing it on the same one is what the playback windows (the drop rule) are for.

``read_outputs`` / ``pick_output`` take plain dicts, so tests use a fake PortAudio. Nothing here opens
PortAudio: Settings lists the same playback names ``capture.list_devices`` already returns, and
``playback.PortAudioOutput`` opens a short-lived ``PyAudio`` per clip or run (plan decision 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


@dataclass(frozen=True)
class OutputDevice:
    name: str
    index: int
    rate: int = 0
    channels: int = 2


@dataclass(frozen=True)
class Pick:
    device: OutputDevice | None  # None: no output device at all
    fell_back: bool = False  # a name was saved but not found: the default is used


def read_outputs(infos: Iterable[dict], wasapi_index: int) -> list[OutputDevice]:
    """The WASAPI playback devices among ``infos`` (PortAudio device-info dicts): no loopback copies
    of them, no inputs, one entry per name (the first)."""
    out, seen = [], set()
    for d in infos:
        if d.get("hostApi") != wasapi_index or d.get("isLoopbackDevice") or int(d.get("maxOutputChannels", 0)) <= 0:
            continue
        name = str(d.get("name", "")).strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(OutputDevice(name, int(d["index"]), int(d.get("defaultSampleRate", 0) or 0),
                                max(1, int(d["maxOutputChannels"]))))
    return out


def pick_output(devices: Sequence[OutputDevice], wanted: str | None, default_index: int | None) -> Pick:
    """The device to play on: the one called ``wanted`` (case-insensitive), else the default output."""
    default = next((d for d in devices if d.index == default_index), None)
    if wanted:
        key = wanted.strip().casefold()
        found = next((d for d in devices if d.name.casefold() == key), None)
        if found is not None:
            return Pick(found)
        return Pick(default or (devices[0] if devices else None), fell_back=True)
    return Pick(default or (devices[0] if devices else None))


def choice_values(names: Iterable[str], saved: str | None, default_label: str) -> list[str]:
    """The Settings drop-down: the default first, then every device; a saved device that is not plugged
    in stays in the list (marked), so opening and saving Settings does not forget it."""
    names = list(names)
    values = [default_label, *names]
    if saved and saved.casefold() not in {n.casefold() for n in names}:
        values.append(missing_label(saved))
    return values


def missing_label(name: str) -> str:
    return f"{name} (not connected)"


def choice_to_setting(value: str, saved: str | None, default_label: str) -> str | None:
    """What to store for a drop-down value: None for the default; a '(not connected)' entry keeps its name."""
    if value == default_label or not value:
        return None
    if saved and value == missing_label(saved):
        return saved
    return value


def choice_from_setting(saved: str | None, names: Iterable[str], default_label: str) -> str:
    if not saved:
        return default_label
    names = list(names)
    for n in names:
        if n.casefold() == saved.casefold():
            return n
    return missing_label(saved)
