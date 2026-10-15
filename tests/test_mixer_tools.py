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
