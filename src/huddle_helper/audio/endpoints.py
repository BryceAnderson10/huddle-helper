"""The Windows default microphone and speakers, read straight from Core Audio.

Only the two COM methods this app calls are declared in full; the vtable slots
before them are placeholders that are never called.
"""

from __future__ import annotations

import ctypes
from ctypes import HRESULT, POINTER, c_void_p
from ctypes.wintypes import DWORD

import comtypes
from comtypes import COMMETHOD, GUID, IUnknown

_CLSID_MMDEVICE_ENUMERATOR = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
_RENDER, _CAPTURE = 0, 1  # EDataFlow
_MULTIMEDIA = 1  # ERole


class _IMMDevice(IUnknown):
    _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
    _methods_ = (
        COMMETHOD([], HRESULT, "Activate"),
        COMMETHOD([], HRESULT, "OpenPropertyStore"),
        COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_void_p), "ppstrId")),
    )


class _IMMDeviceEnumerator(IUnknown):
    _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _methods_ = (
        COMMETHOD([], HRESULT, "EnumAudioEndpoints"),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint",
                  (["in"], DWORD, "dataFlow"), (["in"], DWORD, "role"),
                  (["out"], POINTER(POINTER(_IMMDevice)), "ppDevice")),
    )


def _endpoint_id(enumerator, flow: int) -> str | None:
    try:
        address = enumerator.GetDefaultAudioEndpoint(flow, _MULTIMEDIA).GetId()
    except (comtypes.COMError, OSError):  # no such device, or a COM hiccup
        return None
    if not address:
        return None
    try:
        return ctypes.wstring_at(address)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(c_void_p(address))


def default_endpoint_ids() -> tuple[str | None, str | None]:
    """IDs of the default (microphone, speakers). COM must be initialized on this thread."""
    try:
        enumerator = comtypes.CoCreateInstance(_CLSID_MMDEVICE_ENUMERATOR, _IMMDeviceEnumerator,
                                               comtypes.CLSCTX_INPROC_SERVER)
    except (comtypes.COMError, OSError):
        return None, None
    return _endpoint_id(enumerator, _CAPTURE), _endpoint_id(enumerator, _RENDER)
