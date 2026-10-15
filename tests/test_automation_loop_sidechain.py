"""Remote Script behaviour added in 1.20: device sidechain routing,
automation on mixer parameters and in the Arrangement, envelope read-back,
and Session clip loop/length.

Commands go through _process_command against a fake Live song, as in
test_live_surface.py. No Ableton, no network.
"""

import pytest

from test_live_surface import FakeDevice, FakeParam, FakeSong, FakeTrack, make_live, run


# ------------------------------------------------------------------ fakes

class FakeEnvelope(object):
    """Step envelope: a value holds from just after its step's start until
    the next. Like Live, reading exactly on a boundary gives the step
    before it."""

    def __init__(self, initial):
        self.initial = initial
        self.steps = {}

    def insert_step(self, time, length, value):
        self.steps[round(time, 6)] = value

    def value_at_time(self, time):
        before = [t for t in self.steps if t < time]
        return self.steps[max(before)] if before else self.initial


class FakeClip(object):
    """Enforces Live's rule that loop_start stays before loop_end."""

    def __init__(self, name="Clip", loop_start=0.0, loop_end=4.0, start_time=0.0,
                 end_time=None, start_marker=0.0, looping=True, takes_envelopes=True):
        self.name = name
        self._loop_start = loop_start
        self._loop_end = loop_end
        self.looping = looping
        self.start_marker = start_marker
        self.end_marker = loop_end
        self.start_time = start_time
        self.end_time = end_time if end_time is not None else start_time + loop_end
        self.takes_envelopes = takes_envelopes
        self.envelopes = {}

    @property
    def loop_start(self):
        return self._loop_start

    @loop_start.setter
    def loop_start(self, value):
        if value >= self._loop_end:
            raise RuntimeError("Invalid loop start")
        self._loop_start = value

    @property
    def loop_end(self):
        return self._loop_end

    @loop_end.setter
    def loop_end(self, value):
        if value <= self._loop_start:
            raise RuntimeError("Invalid loop end")
        self._loop_end = value

    @property
    def length(self):
        return self._loop_end - self._loop_start

    def automation_envelope(self, param):
        return self.envelopes.get(id(param))

    def create_automation_envelope(self, param):
        if not self.takes_envelopes:
            raise RuntimeError("Cannot create envelope")
        return self.envelopes.setdefault(id(param), FakeEnvelope(param.value))

    def clear_envelope(self, param):
        self.envelopes.pop(id(param), None)


class FakeSlot(object):
    def __init__(self, clip=None):
        self.clip = clip
        self.has_clip = clip is not None


def clip_track(name, session_clips=(), arrangement_clips=(), devices=()):
    track = FakeTrack(name, devices)
    track.clip_slots = [FakeSlot(c) for c in session_clips]
    track.arrangement_clips = list(arrangement_clips)
    return track


class FakeRouting(object):
    def __init__(self, display_name):
        self.display_name = display_name


class FakeCompressor(FakeDevice):
    def __init__(self, sources, channels=("Post FX", "Pre FX")):
        FakeDevice.__init__(self, "Compressor", [
            FakeParam("Threshold"), FakeParam("S/C On", 0.0, 0.0, 1.0)])
        self.available_input_routing_types = [FakeRouting(n) for n in sources]
        self.input_routing_type = self.available_input_routing_types[0]
        self.available_input_routing_channels = [FakeRouting(n) for n in channels]
        self.input_routing_channel = self.available_input_routing_channels[0]


# --------------------------------------------------------------- sidechain

def test_sidechain_from_a_track_picks_that_track_not_a_similar_name():
    """'Kick' is a substring of '1-Kick Bus', which sorts first."""
    comp = FakeCompressor(["No Input", "1-Kick Bus", "6-Kick", "A-Reverb"])
    tracks = [clip_track("Kick Bus")] + [clip_track("T%d" % i) for i in range(1, 5)]
    tracks += [clip_track("Kick"), clip_track("Bass", devices=[comp])]
    live = make_live(FakeSong(tracks))

    result = run(live, "set_device_sidechain", track_index=6, device_index=0,
                 source_track_index=5, channel_name="pre")

    assert comp.input_routing_type.display_name == "6-Kick"
    assert comp.input_routing_channel.display_name == "Pre FX"
    assert comp.parameters[1].value == 1.0
    assert result["enabled"] is True


def test_sidechain_on_a_device_without_routing_says_so():
    gate = FakeDevice("Gate", [FakeParam("S/C On")])
    live = make_live(FakeSong([clip_track("Kick"), clip_track("Pad", devices=[gate])]))

    reply = live._process_command({"type": "set_device_sidechain", "params": {
        "track_index": 1, "device_index": 0, "source_track_index": 0}})

    assert reply["status"] == "error"
    assert "exposes no sidechain routing" in reply["message"]
    assert gate.parameters[0].value == 0.0


# --------------------------------------------------------------- clip loop

@pytest.mark.parametrize("before, params, after", [
    ((0.0, 4.0), {"loop_start": 8.0, "length": 8.0}, (8.0, 16.0)),
    ((8.0, 16.0), {"loop_start": 0.0, "loop_end": 4.0}, (0.0, 4.0)),
    ((0.0, 4.0), {"length": 16.0}, (0.0, 16.0)),
    ((0.0, 16.0), {"loop_start": 12.0}, (12.0, 16.0)),
], ids=["move-later", "move-earlier", "lengthen", "start-only"])
def test_clip_loop_lands_whatever_order_live_needs(before, params, after):
    """Writing loop_start first fails when the new start is past the old end."""
    clip = FakeClip(loop_start=before[0], loop_end=before[1])
    live = make_live(FakeSong([clip_track("Bass", [clip])]))

    result = run(live, "set_clip_loop", track_index=0, clip_index=0, **params)

    assert (clip.loop_start, clip.loop_end) == after
    assert result["length"] == after[1] - after[0]


def test_clip_loop_refuses_loop_end_and_length_together():
    clip = FakeClip()
    live = make_live(FakeSong([clip_track("Bass", [clip])]))

    reply = live._process_command({"type": "set_clip_loop", "params": {
        "track_index": 0, "clip_index": 0, "loop_end": 8.0, "length": 2.0}})

    assert reply["status"] == "error"
    assert clip.loop_end == 4.0


def test_unlooped_clip_reports_its_new_start_and_end_as_markers():
    """Unlooped, loop_start/loop_end are the clip's start and end, but Live
    can still show the old end_marker in the same tick."""
    clip = FakeClip(loop_start=0.0, loop_end=8.0, looping=False)
    live = make_live(FakeSong([clip_track("Bass", [clip])]))

    result = run(live, "set_clip_loop", track_index=0, clip_index=0,
                 loop_start=2.0, length=4.0)

    assert (result["start_marker"], result["end_marker"]) == (2.0, 6.0)


# -------------------------------------------------------------- automation

def test_session_send_automation_by_display_reads_back_to_the_last_point():
    clip = FakeClip(loop_end=4.0)
    track = clip_track("Stab", [clip])
    live = make_live(FakeSong([track]))
    send_b = track.mixer_device.sends[1]

    run(live, "create_automation", track_index=0, clip_index=0, mixer_target="send",
        send_index=1, points=[{"time": 0, "display": "-inf dB"},
                              {"time": 4, "display": "0 dB"}])
    read = run(live, "get_automation", track_index=0, clip_index=0,
               mixer_target="send", send_index=1, step=2)

    assert read["parameter"] == "B"
    # "0 dB" resolves to within a display step of the fader's 0.85.
    assert [p[1] for p in read["points"]] == pytest.approx([0.0, 0.425, 0.85], abs=1e-3)
    assert read["points"][-1][2] in ("0.0 dB", "-0.0 dB")
    assert send_b.value == 0.0


def test_arrangement_automation_splits_across_clips_and_reports_gaps():
    first = FakeClip("Intro", loop_end=16.0, start_time=0.0, looping=False)
    second = FakeClip("Drop", loop_end=20.0, start_time=32.0, end_time=48.0,
                      start_marker=4.0, looping=False)
    cutoff = FakeParam("Frequency")
    track = clip_track("Pad", arrangement_clips=[first, second],
                       devices=[FakeDevice("Auto Filter", [cutoff])])
    live = make_live(FakeSong([track]))

    result = run(live, "create_arrangement_automation", track_index=0, device_index=0,
                 parameter_index=0, points=[{"time": 8, "value": 0.0},
                                            {"time": 40, "value": 1.0}])
    read = run(live, "get_automation", track_index=0, device_index=0,
               parameter_index=0, start=8, end=40, step=8)

    assert [c["name"] for c in result["clips"]] == ["Intro", "Drop"]
    assert result["uncovered"] == [[16.0, 32.0]]
    # Song beat 32 is the Drop clip's start marker, clip beat 4.
    assert min(second.envelopes[id(cutoff)].steps) == pytest.approx(4.0)
    # Beat 16 is the Intro's end, so it isn't sampled from that clip.
    samples = [p for c in read["clips"] for p in c["points"]]
    assert [p[0] for p in samples] == [8, 32, 40]
    assert [p[1] for p in samples] == pytest.approx([0.0, 0.75, 1.0])


def test_arrangement_automation_without_a_writable_clip_is_an_error():
    locked = FakeClip("Recorded", loop_end=16.0, takes_envelopes=False)
    live = make_live(FakeSong([clip_track("Pad", arrangement_clips=[locked])]))

    reply = live._process_command({"type": "create_arrangement_automation", "params": {
        "track_index": 0, "mixer_target": "volume",
        "points": [{"time": 0, "value": 0.5}, {"time": 8, "value": 0.85}]}})

    assert reply["status"] == "error"
    assert "Cannot create envelope" in reply["message"]


def test_return_track_arrangement_automation_is_refused_not_sent_to_a_track():
    track = clip_track("Kick", arrangement_clips=[FakeClip(loop_end=16.0)])
    live = make_live(FakeSong([track], [clip_track("A-Reverb")]))

    reply = live._process_command({"type": "create_arrangement_automation", "params": {
        "track_index": 0, "track_type": "return", "mixer_target": "volume",
        "points": [{"time": 0, "value": 0.5}, {"time": 8, "value": 0.85}]}})

    assert reply["status"] == "error"
    assert track.arrangement_clips[0].envelopes == {}
