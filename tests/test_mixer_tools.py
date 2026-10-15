"""Server-side tests for the 1.19 tool surface: what each tool sends to the
Remote Script, and that it refuses parameters an older script would ignore.

Runs the real tool functions against a fake connection. No Ableton.
"""

import json

import pytest

import MCP_Server.script_handshake as handshake_mod
import MCP_Server.server as server
from test_clip_notes import _no_consent_prompt, _silence_telemetry, call, fake_conn  # noqa: F401


@pytest.fixture(autouse=True)
def _fresh_handshake(monkeypatch):
    monkeypatch.setattr(handshake_mod, "_script_info", None)


def load_script(capabilities):
    handshake_mod.handshake(lambda command: {
        "script_version": "9.9.9", "capabilities": capabilities})


@pytest.fixture
def script_1_19():
    load_script(["set_device_parameter", "get_device_parameters", "set_mixer_value",
                 "set_track_state", "track_type", "chain_path", "display_value"])


@pytest.mark.parametrize("kwargs", [
    {"track_type": "return"},
    {"chain_path": [1, 0]},
    {"display_value": "180 Hz"},
])
def test_set_device_parameter_refuses_what_an_old_script_would_ignore(fake_conn, kwargs):
    """A 1.18 script drops track_type and chain_path, so the edit would land
    on the regular track or the rack itself instead of failing."""
    load_script(["set_device_parameter"])
    conn = fake_conn()

    out = call(server.set_device_parameter, None, track_index=0, device_index=0,
               parameter_index=0, value=0.5, **kwargs)

    assert "missing capability" in out
    assert conn.sent == []


def test_set_device_parameter_sends_target_path_and_display(fake_conn, script_1_19):
    conn = fake_conn({"device_name": "Echo", "name": "Feedback", "old_display": "20 %",
                      "display": "45 %", "value": 0.45})

    out = call(server.set_device_parameter, None, track_index=1, device_index=0,
               parameter_index=3, display_value="45 %", track_type="return",
               chain_path=[1, 1])

    assert conn.sent == [("set_device_parameter", {
        "track_index": 1, "device_index": 0, "parameter_index": 3,
        "display_value": "45 %", "track_type": "return", "chain_path": [1, 1]})]
    assert "20 % → 45 %" in out


def test_get_device_parameters_is_compact_and_skips_nested_chains(fake_conn, script_1_19):
    conn = fake_conn({"device": {
        "name": "909 Core Kit",
        "parameters": [{"index": 0, "name": "Macro 1", "value": 0.123456, "min": 0.0,
                        "max": 127.0, "display": "0.1", "is_enabled": True,
                        "is_quantized": False}],
        "chains": [{"index": 0, "chain_name": "Kick", "devices": ["Simpler"]}],
        "drum_pads": [{"note": 36, "name": "Kick", "chain_index": 0}],
    }})

    out = call(server.get_device_parameters, None, track_index=0, device_index=0)

    assert conn.sent[0][1]["include_chains"] is False
    device = json.loads(out)["device"]
    assert device["parameters"] == [[0, "Macro 1", 0.1235, 0.0, 127.0, "0.1"]]
    assert device["drum_pads"][0]["note"] == 36
    assert "\n" not in out


def test_set_mixer_sends_levels_as_display_values(fake_conn, script_1_19):
    conn = fake_conn({"old_display": "0.0 dB", "display": "-6.0 dB"})

    call(server.set_mixer, None, track_index=2, volume_db=-6.0, pan=-0.25,
         sends_db={"B": -80, "0": -12.5}, mute=True)

    assert conn.sent == [
        ("set_mixer_value", {"track_index": 2, "target": "volume", "display_value": "-6.0 dB"}),
        ("set_mixer_value", {"track_index": 2, "target": "panning", "value": -0.25}),
        ("set_mixer_value", {"track_index": 2, "target": "send", "send_index": 1,
                             "display_value": "-inf dB"}),
        ("set_mixer_value", {"track_index": 2, "target": "send", "send_index": 0,
                             "display_value": "-12.5 dB"}),
        ("set_track_state", {"track_index": 2, "mute": True}),
    ]


class _SlowSocket:
    """Replies only after the caller's timeout has been checked."""

    def __init__(self):
        self.timeouts = []

    def sendall(self, data):
        pass

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv(self, size):
        if self.timeouts[-1] < 60:
            import socket
            raise socket.timeout()
        return b'{"status": "success", "result": {"results": []}}'


@pytest.mark.parametrize("command", ["search_browser", "create_audio_clip"])
def test_long_running_commands_keep_their_socket_timeout(command):
    """receive_full_response reset every timeout to 15 s, so building the
    browser index (or importing a long sample) failed with 'No data
    received' while Live was still working."""
    sock = _SlowSocket()
    conn = server.AbletonConnection(host="localhost", port=0)
    conn.sock = sock

    assert conn.send_command(command, {}) == {"results": []}
    assert min(sock.timeouts) >= 60


# ------------------------------------------------------------- 1.20 tools

@pytest.mark.parametrize("kwargs", [
    {"mixer_target": "send B"},
    {"device_index": 0, "parameter_index": 1, "chain_path": [1, 0]},
    {"device_index": 0, "parameter_index": 1, "display_points": True},
])
def test_clip_automation_refuses_targets_an_old_script_would_misplace(fake_conn, kwargs):
    """A 1.19 script ignores mixer_target and chain_path, so the envelope
    would land on device 0's parameter 0 (or the rack) instead."""
    load_script(["create_automation"])
    conn = fake_conn()
    points = [{"time": 0, "value": 0.0}, {"time": 4, "value": 1.0}]
    if kwargs.pop("display_points", False):
        points = [{"time": 0, "display": "200 Hz"}, {"time": 4, "display": "2 kHz"}]

    out = call(server.create_clip_automation, None, track_index=0, clip_index=0,
               points=points, **kwargs)

    assert "missing capability" in out
    assert conn.sent == []


def test_clip_automation_sends_a_mixer_send_target(fake_conn):
    load_script(["create_automation", "automation_targets"])
    conn = fake_conn({"inserted_steps": 17, "parameter": "B"})
    points = [{"time": 0, "display": "-inf dB"}, {"time": 4, "display": "-6 dB"}]

    call(server.create_clip_automation, None, track_index=3, clip_index=1,
         points=points, mixer_target="Send B")

    assert conn.sent == [("create_automation", {
        "mixer_target": "send", "send_index": 1, "track_index": 3, "clip_index": 1,
        "points": points})]


def test_set_device_sidechain_sends_the_source_track(fake_conn):
    load_script(["set_device_sidechain"])
    conn = fake_conn({"device_name": "Compressor", "source": "6-Kick",
                      "channel": "Post FX", "enabled": True,
                      "available_sources": ["No Input", "6-Kick"]})

    out = call(server.set_device_sidechain, None, track_index=1, device_index=0,
               source_track=0, source_track_type="return", channel="Post FX")

    assert conn.sent == [("set_device_sidechain", {
        "track_index": 1, "device_index": 0, "enable": True,
        "source_track_index": 0, "source_track_type": "return",
        "channel_name": "Post FX"})]
    assert out.startswith("Compressor sidechain: 6-Kick / Post FX (on)")
