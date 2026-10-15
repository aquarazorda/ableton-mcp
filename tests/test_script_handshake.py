"""Tests for the Remote Script version handshake.

A Remote Script newer than the version this package expects only adds
commands, so it must count as up to date and pass the require_capability
gates. An older script must still be flagged. Live is replaced by a fake
send_command, so these run anywhere.
"""

import pytest

import MCP_Server.script_handshake as handshake_mod

EXPECTED = "1.18.0"


@pytest.fixture(autouse=True)
def pinned_expected_version(monkeypatch):
    # Pin the expectation so these tests survive version bumps.
    monkeypatch.setattr(handshake_mod, "EXPECTED_REMOTE_SCRIPT_VERSION", EXPECTED)


def run_handshake(script_version, capabilities=None):
    result = {"script_version": script_version, "capabilities": capabilities}
    return handshake_mod.handshake(lambda command: result)


@pytest.mark.parametrize("version", ["1.18.0", "1.18.1", "1.19.0", "2.0.0", "1.18.0.1"])
def test_expected_or_newer_script_is_up_to_date(version):
    info = run_handshake(version)

    assert info["up_to_date"] is True
    assert handshake_mod.script_version_ok() is True


@pytest.mark.parametrize("version", ["1.17.9", "1.7.1", "0.9"])
def test_older_script_is_not_up_to_date(version):
    info = run_handshake(version)

    assert info["up_to_date"] is False
    assert handshake_mod.script_version_ok() is False


def test_short_version_is_padded():
    assert run_handshake("1.18")["up_to_date"] is True
    assert run_handshake("1.17")["up_to_date"] is False


def test_non_numeric_version_must_match_exactly():
    assert run_handshake("1.19.0-dev")["up_to_date"] is False


def test_newer_script_without_capability_list_passes_gate():
    # No capability list, so the gate falls back to the version check.
    run_handshake("1.19.0")

    assert handshake_mod.require_capability("create_locator") is None


def test_older_script_without_capability_list_fails_gate():
    run_handshake("1.7.1")

    error = handshake_mod.require_capability("create_locator")
    assert error is not None
    assert "create_locator" in error


def test_capability_list_still_decides_when_present():
    run_handshake("1.19.0", capabilities=["get_clip_notes"])

    assert handshake_mod.require_capability("get_clip_notes") is None
    assert handshake_mod.require_capability("create_locator") is not None
