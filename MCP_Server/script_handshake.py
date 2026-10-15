"""Remote Script version handshake and capability checks."""

from __future__ import annotations

import logging
import threading
from typing import Any

from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION

logger = logging.getLogger("AbletonMCPServer")

_lock = threading.Lock()
_script_info: dict[str, Any] | None = None
_handshake_done = False

# Commands present on Remote Scripts before get_script_info existed
_LEGACY_CAPABILITIES = frozenset({
    "get_session_info",
    "get_track_info",
    "create_midi_track",
    "create_clip",
    "add_notes_to_clip",
    "set_tempo",
    "set_track_name",
    "set_clip_name",
    "fire_clip",
    "stop_clip",
    "start_playback",
    "stop_playback",
    "load_instrument_or_effect",
    "get_browser_tree",
    "get_browser_items_at_path",
})


def _parse_version(value: Any) -> tuple[int, ...] | None:
    """Parse a dotted numeric version ("1.18.0"); None if not numeric."""
    parts = str(value or "").strip().split(".")
    if not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def version_compatible(script_version: Any) -> bool:
    """True when the loaded script is the expected version or newer.

    A newer Remote Script only adds commands, so it still serves everything
    this package calls. Non-numeric versions must match exactly.
    """
    loaded = _parse_version(script_version)
    expected = _parse_version(EXPECTED_REMOTE_SCRIPT_VERSION)
    if loaded is None or expected is None:
        return str(script_version or "") == EXPECTED_REMOTE_SCRIPT_VERSION
    width = max(len(loaded), len(expected))
    loaded += (0,) * (width - len(loaded))
    expected += (0,) * (width - len(expected))
    return loaded >= expected


def get_cached_script_info() -> dict[str, Any] | None:
    with _lock:
        return dict(_script_info) if _script_info else None


def script_has_capability(name: str) -> bool:
    info = get_cached_script_info()
    if not info or info.get("script_version") in (None, "legacy"):
        return name in _LEGACY_CAPABILITIES
    caps = info.get("capabilities")
    if isinstance(caps, list):
        return name in caps
    return script_version_ok()


def script_version_ok() -> bool:
    info = get_cached_script_info()
    if not info:
        return False
    return version_compatible(info.get("script_version"))


def handshake(send_command) -> dict[str, Any]:
    """Query Live for script info. On old scripts, get_script_info is unknown."""
    global _script_info, _handshake_done
    info: dict[str, Any] = {
        "script_version": None,
        "capabilities": [],
        "expected_version": EXPECTED_REMOTE_SCRIPT_VERSION,
        "up_to_date": False,
        "error": None,
    }
    try:
        result = send_command("get_script_info")
        if isinstance(result, dict):
            info.update(result)
            info["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
            info["up_to_date"] = version_compatible(result.get("script_version"))
    except Exception as e:
        msg = str(e)
        info["error"] = msg
        # Old Remote Scripts don't know get_script_info
        if "Unknown command" in msg or "unknown command" in msg.lower():
            info["script_version"] = "legacy"
            info["capabilities"] = []
            info["up_to_date"] = False
            logger.warning(
                "Ableton Remote Script is outdated (no get_script_info). "
                "Expected %s. Run `ableton-mcp-install-script`, then restart "
                "Live.",
                EXPECTED_REMOTE_SCRIPT_VERSION,
            )
        else:
            logger.warning("Remote Script handshake failed: %s", e)

    with _lock:
        _script_info = info
        _handshake_done = True

    if info.get("up_to_date"):
        logger.info(
            "Remote Script handshake OK (v%s, %d capabilities)",
            info.get("script_version"),
            len(info.get("capabilities") or []),
        )
    elif info.get("script_version") and info.get("script_version") != "legacy":
        logger.warning(
            "Remote Script v%s loaded, package expects v%s — run "
            "`ableton-mcp-install-script`, then restart Ableton.",
            info.get("script_version"),
            EXPECTED_REMOTE_SCRIPT_VERSION,
        )

    return info


def require_capability(name: str) -> str | None:
    """Return an error message if capability missing, else None."""
    if script_has_capability(name):
        return None
    info = get_cached_script_info() or {}
    return (
        f"Ableton Remote Script missing capability '{name}' "
        f"(loaded={info.get('script_version')!r}, "
        f"expected={EXPECTED_REMOTE_SCRIPT_VERSION}). "
        f"Run `ableton-mcp-install-script` to update the User Remote Script, "
        f"then restart Ableton Live."
    )
