"""Remote Script behaviour added in 1.19: reaching every track type and
nested rack devices, setting parameters by their display string, drum-pad
notes, browser search and paging, and device moves.

Commands go through _process_command, the same entry point the TCP socket
uses, against a fake Live song. No Ableton, no network.
"""

import math

import pytest

from test_remote_script_bridge import FakeApplication, FakeBrowserItem, make_instance


# ------------------------------------------------------------------ fakes

def freq_display(v):
    hz = 10.0 * 2200.0 ** v
    return "%.0f Hz" % hz if hz < 1000 else "%.2f kHz" % (hz / 1000.0)


def volume_display(v):
    # Live's fader: 0.85 is 0 dB, the bottom is -inf.
    return "-inf dB" if v <= 0 else "%.1f dB" % ((v - 0.85) * 80.0)


def pan_display(v):
    amount = int(round(abs(v) * 50))
    if amount == 0:
        return "C"
    return "%d%s" % (amount, "L" if v < 0 else "R")


class FakeParam(object):
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0, display=None,
                 value_items=None):
        self.name = name
        self.value = value
        self.min = lo
        self.max = hi
        self._display = display or (lambda v: "%.2f" % v)
        self.value_items = value_items or []
        self.is_quantized = bool(value_items)
        self.is_enabled = True

    def str_for_value(self, v):
        return self._display(v)


class FakeDevice(object):
    def __init__(self, name, parameters=(), chains=None, drum_pads=None):
        self.name = name
        self.class_name = name.replace(" ", "")
        self.parameters = list(parameters)
        self.can_have_chains = chains is not None
        self.chains = list(chains or [])
        self.can_have_drum_pads = drum_pads is not None
        if drum_pads is not None:
            self.drum_pads = drum_pads


class FakeChain(object):
    def __init__(self, name, devices=(), in_note=None):
        self.name = name
        self.devices = list(devices)
        if in_note is not None:
            self.in_note = in_note

    def delete_device(self, index):
        del self.devices[index]


class FakePad(object):
    def __init__(self, note, name, chains=()):
        self.note = note
        self.name = name
        self.chains = list(chains)


class FakeMixer(object):
    def __init__(self):
        self.volume = FakeParam("Volume", 0.85, 0.0, 1.0, volume_display)
        self.panning = FakeParam("Pan", 0.0, -1.0, 1.0, pan_display)
        self.sends = [FakeParam("A", 0.0, 0.0, 1.0, volume_display),
                      FakeParam("B", 0.0, 0.0, 1.0, volume_display)]


class FakeTrack(object):
    def __init__(self, name, devices=()):
        self.name = name
        self.devices = list(devices)
        self.mixer_device = FakeMixer()
        self.mute = False
        self.solo = False


class FakeSong(object):
    def __init__(self, tracks=(), returns=(), counts_vacated_slot=False):
        self.tracks = list(tracks)
        self.return_tracks = list(returns)
        self.master_track = FakeTrack("Master")
        self.counts_vacated_slot = counts_vacated_slot

    def move_device(self, device, target, position):
        devices = target.devices
        devices.remove(device)
        # Live doesn't document whether position counts the slot the device
        # leaves; the script must land correctly under either reading.
        if self.counts_vacated_slot:
            position -= 1 if position > 0 else 0
        devices.insert(min(position, len(devices)), device)


def make_live(song, application=None):
    import test_remote_script_bridge as bridge

    inst = make_instance(bridge._load_script_module(), application=application)
    inst._song = song
    inst.schedule_message = lambda delay, fn: fn()
    return inst


def run(inst, command, **params):
    reply = inst._process_command({"type": command, "params": params})
    assert reply["status"] == "success", reply.get("message")
    return reply["result"]


# ----------------------------------------------------------- track targets

def test_track_type_reaches_return_and_master_not_the_same_index_track():
    """Before 1.19 every command indexed song.tracks, so 'return 0' silently
    edited regular track 0."""
    track = FakeTrack("Kick", [FakeDevice("EQ", [FakeParam("Gain")])])
    ret = FakeTrack("A-Reverb", [FakeDevice("Reverb", [FakeParam("Decay")])])
    song = FakeSong([track], [ret])
    song.master_track.devices = [FakeDevice("Limiter", [FakeParam("Ceiling")])]
    live = make_live(song)

    run(live, "set_device_parameter", track_index=0, device_index=0,
        parameter_index=0, value=0.7, track_type="return")
    run(live, "set_mixer_value", track_index=0, target="volume", value=0.5,
        track_type="master")

    assert ret.devices[0].parameters[0].value == pytest.approx(0.7)
    assert track.devices[0].parameters[0].value == 0.0
    assert song.master_track.mixer_device.volume.value == pytest.approx(0.5)
    assert track.mixer_device.volume.value == pytest.approx(0.85)


def test_unknown_track_type_is_an_error_not_a_regular_track():
    live = make_live(FakeSong([FakeTrack("Kick")]))

    reply = live._process_command({"type": "set_mixer_value", "params": {
        "track_index": 0, "target": "volume", "value": 0.1, "track_type": "returns"}})

    assert reply["status"] == "error"
    assert live._song.tracks[0].mixer_device.volume.value == pytest.approx(0.85)


# ------------------------------------------------------------- rack paths

def test_chain_path_sets_a_device_inside_a_rack():
    echo = FakeDevice("Echo", [FakeParam("Feedback")])
    rack = FakeDevice("Chord Dub Keys", [FakeParam("Macro 1")],
                      chains=[FakeChain("Dry"), FakeChain("Wet", [FakeDevice("Wavetable"), echo])])
    live = make_live(FakeSong([FakeTrack("Stab", [rack])]))

    result = run(live, "set_device_parameter", track_index=0, device_index=0,
                 parameter_index=0, value=0.6, chain_path=[1, 1])

    assert result["device_name"] == "Echo"
    assert echo.parameters[0].value == pytest.approx(0.6)
    assert rack.parameters[0].value == 0.0


# ---------------------------------------------------------- display values

@pytest.mark.parametrize("param, wanted, expected_display", [
    (FakeParam("Frequency", 0.5, display=freq_display), "180 Hz", "180 Hz"),
    (FakeParam("Frequency", 0.5, display=freq_display), "1.2 kHz", "1.20 kHz"),
    (FakeParam("Volume", 0.85, display=volume_display), "-6 dB", "-6.0 dB"),
    (FakeParam("Volume", 0.85, display=volume_display), "-inf dB", "-inf dB"),
    (FakeParam("Pan", 0.0, -1.0, 1.0, display=pan_display), "25L", "25L"),
    (FakeParam("Pan", 0.5, -1.0, 1.0, display=pan_display), "C", "C"),
    (FakeParam("Filter Type", 0.0, 0.0, 3.0,
               value_items=["Low Cut", "Low Shelf", "Bell", "High Cut"]), "Bell", "2.00"),
])
def test_display_value_lands_on_the_value_live_would_show(param, wanted, expected_display):
    live = make_live(FakeSong([FakeTrack("T", [FakeDevice("Dev", [param])])]))

    result = run(live, "set_device_parameter", track_index=0, device_index=0,
                 parameter_index=0, display_value=wanted)

    assert result["display"] == expected_display


def test_display_value_beyond_the_range_clamps_to_the_end():
    param = FakeParam("Frequency", 0.5, display=freq_display)
    live = make_live(FakeSong([FakeTrack("T", [FakeDevice("EQ", [param])])]))

    run(live, "set_device_parameter", track_index=0, device_index=0,
        parameter_index=0, display_value="90 kHz")

    assert param.value == pytest.approx(1.0)


# --------------------------------------------- device parameters payload

def _drum_rack(with_in_note):
    kick = FakeChain("Kick", [FakeDevice("Simpler", [FakeParam("Volume")] * 40)],
                     in_note=36 if with_in_note else None)
    hat = FakeChain("Open Hi Hat", [FakeDevice("Simpler", [FakeParam("Volume")] * 40)],
                    in_note=46 if with_in_note else None)
    pads = None if with_in_note else [
        FakePad(36, "Kick", [kick]), FakePad(37, "Empty"), FakePad(46, "Open Hi Hat", [hat])]
    return FakeDevice("909 Core Kit", [FakeParam("Macro 1")], chains=[kick, hat],
                      drum_pads=pads if pads is not None else [])


@pytest.mark.parametrize("with_in_note", [True, False], ids=["live12-in_note", "drum_pads"])
def test_drum_rack_reports_the_note_that_plays_each_pad(with_in_note):
    live = make_live(FakeSong([FakeTrack("Drums", [_drum_rack(with_in_note)])]))

    device = run(live, "get_device_parameters", track_index=0, device_index=0,
                 include_chains=False)["device"]

    assert device["drum_pads"] == [
        {"note": 36, "name": "Kick", "chain_index": 0},
        {"note": 46, "name": "Open Hi Hat", "chain_index": 1},
    ]


def test_rack_without_include_chains_lists_chains_but_not_nested_parameters():
    """A full drum kit came back at 300-500 KB because every pad's Simpler
    was serialised in full."""
    live = make_live(FakeSong([FakeTrack("Drums", [_drum_rack(True)])]))

    device = run(live, "get_device_parameters", track_index=0, device_index=0,
                 include_chains=False)["device"]

    assert device["chains"][1] == {
        "index": 1, "chain_name": "Open Hi Hat", "devices": ["Simpler"], "in_note": 46}
    assert [p["name"] for p in device["parameters"]] == ["Macro 1"]
    assert device["parameters"][0]["display"] == "0.00"


def test_name_filter_keeps_only_matching_parameters():
    eq = FakeDevice("EQ Eight", [FakeParam("1 Frequency A"), FakeParam("1 Gain A"),
                                 FakeParam("1 Resonance A"), FakeParam("Output Gain")])
    live = make_live(FakeSong([FakeTrack("T", [eq])]))

    device = run(live, "get_device_parameters", track_index=0, device_index=0,
                 name_filter="freq|output")["device"]

    assert [p["name"] for p in device["parameters"]] == ["1 Frequency A", "Output Gain"]


# ------------------------------------------------------------ device moves

@pytest.mark.parametrize("counts_vacated_slot", [False, True])
@pytest.mark.parametrize("from_index, to_index", [(0, 2), (2, 0), (1, 3)])
def test_move_device_lands_at_the_requested_index(counts_vacated_slot, from_index, to_index):
    names = ["Drift", "Saturator", "EQ Eight", "Utility"]
    track = FakeTrack("Bass", [FakeDevice(n) for n in names])
    live = make_live(FakeSong([track], counts_vacated_slot=counts_vacated_slot))

    run(live, "move_device", track_index=0, device_index=from_index, new_index=to_index)

    assert [d.name for d in track.devices].index(names[from_index]) == to_index


def test_delete_device_inside_a_rack_chain():
    chain = FakeChain("Wet", [FakeDevice("Wavetable"), FakeDevice("Utility")])
    rack = FakeDevice("Keys", chains=[chain])
    live = make_live(FakeSong([FakeTrack("Stab", [rack])]))

    result = run(live, "delete_device", track_index=0, device_index=0, chain_path=[0, 1])

    assert result["deleted"] == "Utility"
    assert [d.name for d in chain.devices] == ["Wavetable"]


# ----------------------------------------------------------------- browser

class FakeSearchBrowser(object):
    def __init__(self, **roots):
        for name, root in roots.items():
            setattr(self, name, root)


def test_search_browser_ranks_name_matches_and_finds_presets_under_devices():
    drift = FakeBrowserItem("Drift", [
        FakeBrowserItem("Sub Dub Bass.adv", loadable=True, uri="u:drift-dub"),
        FakeBrowserItem("Bright Lead.adv", loadable=True, uri="u:drift-lead"),
    ], loadable=True, uri="u:drift")
    sounds = FakeBrowserItem("Sounds", [
        FakeBrowserItem("Dub Techno", [
            FakeBrowserItem("Deep Chord.adg", loadable=True, uri="u:chord"),
        ]),
        FakeBrowserItem("Dub Chord.adg", loadable=True, uri="u:dub-chord"),
    ])
    browser = FakeSearchBrowser(instruments=FakeBrowserItem("Instruments", [drift]),
                                sounds=sounds)
    live = make_live(FakeSong(), application=FakeApplication(browser=browser))

    chord = run(live, "search_browser", query="dub chord")
    bass = run(live, "search_browser", query="sub dub")

    # Exact name first; a match only through the folder path comes after.
    assert [r["uri"] for r in chord["results"]] == ["u:dub-chord", "u:chord"]
    assert [r["uri"] for r in bass["results"]] == ["u:drift-dub"]
    assert all(r["is_loadable"] for r in chord["results"])


def test_browser_path_listing_pages():
    presets = [FakeBrowserItem("Preset %03d" % i, loadable=True) for i in range(250)]
    browser = FakeSearchBrowser(sounds=FakeBrowserItem("Sounds", [FakeBrowserItem("Bass", presets)]))
    live = make_live(FakeSong(), application=FakeApplication(browser=browser))

    page = run(live, "get_browser_items_at_path", path="sounds/Bass", offset=200, limit=100)

    assert page["total"] == 250
    assert page["has_more"] is False
    assert [i["name"] for i in page["items"]][:2] == ["Preset 200", "Preset 201"]
    assert len(page["items"]) == 50


# ------------------------------------------------------------------ mixer

def test_get_mixer_reports_returns_and_master_with_display_levels():
    song = FakeSong([FakeTrack("Kick")], [FakeTrack("A-Reverb")])
    song.tracks[0].mixer_device.sends[0].value = 0.85
    live = make_live(song)

    mixer = run(live, "get_mixer")

    assert mixer["tracks"][0]["sends"][0]["display"] == "0.0 dB"
    assert mixer["returns"][0]["name"] == "A-Reverb"
    assert mixer["master"]["volume_display"] == "0.0 dB"
    assert "mute" not in mixer["master"]
