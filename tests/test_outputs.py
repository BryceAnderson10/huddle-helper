"""'Play clips on': device list and choice by name, the Settings round trip, the output stream's device pick."""

import sys
import types

import pytest

from huddle_helper import config, outputs, playback

WASAPI = 2


def info(index, name, outs=2, host=WASAPI, loop=False, rate=48_000):
    return {"index": index, "name": name, "hostApi": host, "maxOutputChannels": outs, "isLoopbackDevice": loop,
            "defaultSampleRate": rate}


INFOS = [info(0, "Speakers (Realtek)"), info(1, "Headset (USB)"), info(2, "Speakers (Realtek) [Loopback]", loop=True),
         info(3, "Microphone (USB)", outs=0), info(4, "Old MME speakers", host=0), info(5, "Headset (USB)")]


def test_read_outputs_keeps_wasapi_playback_devices_once_by_name():
    devs = outputs.read_outputs(INFOS, WASAPI)
    assert [d.name for d in devs] == ["Speakers (Realtek)", "Headset (USB)"]
    assert devs[1].index == 1  # the first of two with the same name


def test_pick_output_by_name_not_index():
    devs = outputs.read_outputs(INFOS, WASAPI)
    assert outputs.pick_output(devs, "headset (usb)", 0).device.index == 1  # case-insensitive
    # the same name after the indices changed (a device was plugged in before it)
    moved = outputs.read_outputs([info(7, "Headset (USB)"), info(8, "Speakers (Realtek)")], WASAPI)
    assert outputs.pick_output(moved, "Headset (USB)", 8).device.index == 7
    assert outputs.pick_output(devs, None, 0) == outputs.Pick(devs[0], False)


def test_a_device_that_is_gone_falls_back_to_the_default_and_says_so():
    devs = outputs.read_outputs(INFOS, WASAPI)
    p = outputs.pick_output(devs, "Bluetooth Buds", 0)
    assert p.fell_back and p.device.name == "Speakers (Realtek)"
    assert outputs.pick_output([], "x", None) == outputs.Pick(None, True)
    assert outputs.pick_output([], None, None).device is None
    assert outputs.pick_output(devs, "Bluetooth Buds", 99).device is devs[0]  # default unknown: the first


def test_settings_drop_down_values_and_what_is_stored():
    names = ["Speakers (Realtek)", "Headset (USB)"]
    dflt = "(Windows default)"
    assert outputs.choice_values(names, None, dflt) == [dflt, *names]
    assert outputs.choice_to_setting(dflt, None, dflt) is None
    assert outputs.choice_to_setting("Headset (USB)", None, dflt) == "Headset (USB)"
    # a saved device that is unplugged stays selectable and is kept when Settings is saved untouched
    vals = outputs.choice_values(names, "Buds", dflt)
    assert vals[-1] == "Buds (not connected)"
    shown = outputs.choice_from_setting("Buds", names, dflt)
    assert shown == "Buds (not connected)" and outputs.choice_to_setting(shown, "Buds", dflt) == "Buds"
    assert outputs.choice_from_setting("headset (usb)", names, dflt) == "Headset (USB)"
    assert outputs.choice_from_setting(None, names, dflt) == dflt


def test_the_setting_is_additive_and_separate_from_the_capture_device():
    assert config.Settings().clip_output_device is None
    old = config.Settings.from_dict({"settings_version": 4, "output_device": "Loop Dev"})
    assert old.clip_output_device is None and old.output_device == "Loop Dev"
    s = config.Settings.from_dict({**config.Settings(clip_output_device="Headset (USB)").to_dict()})
    assert s.clip_output_device == "Headset (USB)" and s.output_device is None


# -- the output stream picks the device by name (a fake PortAudio) ------------------------------------------------


class FakePA:
    instances = []
    fail_open = False

    def __init__(self):
        self.terminated, self.opened = False, None
        FakePA.instances.append(self)

    def get_host_api_info_by_type(self, t):
        return {"index": WASAPI, "defaultOutputDevice": 0}

    def get_device_count(self):
        return len(INFOS)

    def get_device_info_by_index(self, i):
        return INFOS[i]

    def open(self, **kw):
        if FakePA.fail_open:
            raise OSError("busy")
        self.opened = kw
        return types.SimpleNamespace(write=lambda b: None, stop_stream=lambda: None, close=lambda: None)

    def terminate(self):
        self.terminated = True


@pytest.fixture()
def fake_pa(monkeypatch):
    FakePA.instances, FakePA.fail_open = [], False
    mod = types.SimpleNamespace(PyAudio=FakePA, paWASAPI=WASAPI, paInt16=8)
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", mod)
    return FakePA


def test_port_audio_output_opens_the_named_device_and_terminates_its_instance(fake_pa):
    out = playback.PortAudioOutput("Headset (USB)")
    pa = fake_pa.instances[0]
    assert pa.opened["output_device_index"] == 1 and not out.fell_back and out.device_name == "Headset (USB)"
    out.close()
    assert pa.terminated  # nothing of PortAudio stays initialised: capture's rebind needs that


def test_port_audio_output_falls_back_to_the_default_output(fake_pa):
    out = playback.PortAudioOutput("Gone Device")
    assert out.fell_back and fake_pa.instances[0].opened["output_device_index"] == 0
    out.close()
    default = playback.PortAudioOutput(None)
    assert not default.fell_back and fake_pa.instances[1].opened["output_device_index"] == 0
    default.close()


def test_a_failed_open_still_terminates_the_instance(fake_pa):
    fake_pa.fail_open = True
    with pytest.raises(OSError):
        playback.PortAudioOutput("Headset (USB)")
    assert fake_pa.instances[0].terminated


def test_the_player_tells_the_app_that_the_chosen_device_was_missing(fake_pa):
    told = []
    player = playback.Player(0.0)
    player.device_name, player.on_fallback = "Gone Device", told.append
    player._factory().close()
    assert told == ["Gone Device"]
    player.device_name = None
    player._factory().close()
    assert told == ["Gone Device"]
