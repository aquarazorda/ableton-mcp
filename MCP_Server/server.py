# ableton_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import socket
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any, List, Union

from .telemetry import record_startup
from .telemetry_decorator import telemetry_tool, rich_telemetry_tool
from .dataset import dataset_enabled, get_recorder
from .dataset.trajectory_decorator import trajectory_tool

ABLETON_HOST = os.environ.get("ABLETON_HOST", "localhost")
ABLETON_PORT = int(os.environ.get("ABLETON_PORT", "9877"))

# Configure logging
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("AbletonMCPServer")

@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket = None

    # One socket, several threads: tool calls run on the MCP worker while the
    # passive poller drains events on its own thread. Serialise so their
    # sendall/recv pairs cannot interleave.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def connect(self) -> bool:
        """Connect to the Ableton Remote Script socket server"""
        if self.sock:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(5.0)
            self.sock.connect((self.host, self.port))
            self.sock.settimeout(None)
            logger.info(f"Connected to Ableton at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Ableton at {self.host}:{self.port}: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Ableton Remote Script"""
        with self._lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception as e:
                    logger.error(f"Error disconnecting from Ableton: {str(e)}")
                finally:
                    self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks.

        Uses whatever timeout the caller set on ``sock``: long-running
        commands (audio import, meter sampling, browser indexing) need more
        than the default, so this must not reset it.
        """
        chunks = []

        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        if not chunks:
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        """Send a command to Ableton and return the response.

        Serialised on ``self._lock``: the request and its response are one
        indivisible exchange on a shared socket. Holding the lock across the
        whole round-trip (including reconnect) is what keeps a concurrent
        caller from reading someone else's reply.
        """
        with self._lock:
            return self._send_command_locked(command_type, params)

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Ableton")

        command = {
            "type": command_type,
            "params": params or {}
        }
        
        # Check if this is a state-modifying command
        is_modifying_command = command_type in [
            "create_midi_track", "create_audio_track", "set_track_name",
            "create_clip", "create_audio_clip", "add_notes_to_clip", "set_clip_name",
            "delete_clip",
            "clear_notes_from_clip",
            "set_tempo", "fire_clip", "stop_clip", "set_device_parameter",
            "start_playback", "stop_playback", "load_instrument_or_effect",
            # Arrangement view commands
            "switch_to_arrangement_view", "set_current_song_time",
            "duplicate_session_clip_to_arrangement",
            "create_locator",
            "load_browser_item", "set_mixer_value", "set_track_state",
            "delete_device", "move_device", "delete_track", "set_track_routing",
            "create_automation", "update_notes", "remove_notes_range",
            "set_clip_launch_settings", "set_clip_groove", "create_scene",
            "delete_scene", "fire_scene", "undo", "redo",
            "set_clip_loop", "create_arrangement_automation", "set_device_sidechain",
        ]

        # Commands whose work on Live's main thread can take noticeably longer
        # than the default modifying-command budget (e.g. importing/decoding a
        # large audio file). Give them a wider socket timeout so we don't time
        # out before the Remote Script's own queue does.
        long_running_commands = {
            "create_audio_clip": 65.0,
            # Samples meters for up to 10 s before replying.
            "get_meters": 20.0,
            # The first search of a browser root walks and caches it.
            "search_browser": 90.0,
        }
        
        try:
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set timeout based on command type
            if command_type in long_running_commands:
                timeout = long_running_commands[command_type]
            else:
                timeout = 15.0 if is_modifying_command else 10.0
            self.sock.settimeout(timeout)

            # Receive the response
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")

            # Parse the response
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")

            if response.get("status") == "error":
                logger.error(f"Ableton error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Ableton"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Ableton")
            self.sock = None
            raise Exception("Timeout waiting for Ableton response")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Ableton lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Ableton: {str(e)}")
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            self.sock = None
            raise Exception(f"Invalid response from Ableton: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Ableton: {str(e)}")
            self.sock = None
            raise Exception(f"Communication error with Ableton: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    try:
        logger.info("AbletonMCP server starting up")

        # Record startup event for telemetry
        try:
            record_startup()
        except Exception as e:
            logger.debug(f"Failed to record startup telemetry: {e}")

        script_info = None
        try:
            ableton = get_ableton_connection()
            logger.info("Successfully connected to Ableton on startup")
            from .script_handshake import handshake

            script_info = handshake(ableton.send_command)
        except Exception as e:
            logger.warning(f"Could not connect to Ableton on startup: {str(e)}")
            logger.warning("Make sure the Ableton Remote Script is running")

        if dataset_enabled():
            try:
                recorder = get_recorder()
                if recorder:
                    logger.info(
                        "Dataset recording enabled → Supabase session %s",
                        recorder.session_id,
                    )
                from .script_handshake import script_has_capability
                from .dataset.passive_poller import start_passive_poller

                # Runs even without Live-UI events: it also settles implicit
                # preferences for agent actions.
                start_passive_poller()
                if not script_has_capability("drain_passive_events"):
                    logger.warning(
                        "Passive Live listeners unavailable — Remote Script outdated "
                        "or not loaded. Restart Ableton after script update."
                    )
            except Exception as e:
                logger.warning(f"Failed to start dataset recorder: {e}")
        else:
            reason = "unknown"
            try:
                from .config import telemetry_config  # noqa: F401
                from .telemetry import get_telemetry_consent, is_telemetry_enabled

                if not is_telemetry_enabled():
                    reason = "telemetry disabled (config.enabled=False or DISABLE_TELEMETRY)"
                elif not get_telemetry_consent():
                    reason = "no dataset consent yet — the user has not opted in"
                else:
                    reason = "ABLETON_MCP_DISABLE_DATASET or unexpected gate failure"
            except Exception:
                reason = "MCP_Server/config.py missing or unreadable (gitignored; required for Supabase)"
            logger.info("Dataset recording off — %s", reason)

        yield {"script_info": script_info}
    finally:
        global _ableton_connection
        try:
            from .dataset.passive_poller import stop_passive_poller

            stop_passive_poller()
        except Exception as e:
            logger.debug(f"Failed to stop passive poller: {e}")
        if _ableton_connection:
            logger.info("Disconnecting from Ableton on shutdown")
            _ableton_connection.disconnect()
            _ableton_connection = None
        try:
            recorder = get_recorder()
            if recorder:
                # Bounded wait so queued rows land before the daemon worker
                # is killed at exit.
                recorder.end(timeout=float(
                    os.environ.get("ABLETON_MCP_DATASET_FLUSH_SEC", "5")
                ))
        except Exception as e:
            logger.debug(f"Failed to end dataset session: {e}")
        logger.info("AbletonMCP server shut down")

# Create the MCP server with lifespan support
mcp = FastMCP(
    "AbletonMCP",
    lifespan=server_lifespan
)

# Global connection for resources
_ableton_connection = None

def get_ableton_connection():
    """Get or create a persistent Ableton connection"""
    global _ableton_connection

    if _ableton_connection is not None and _ableton_connection.sock is not None:
        try:
            # Check if the socket is still alive by peeking for data
            # MSG_PEEK + MSG_DONTWAIT will raise BlockingIOError if alive but no data,
            # or return b'' if the remote end has closed the connection.
            _ableton_connection.sock.setblocking(False)
            try:
                data = _ableton_connection.sock.recv(1, socket.MSG_PEEK)
                if data == b'':
                    raise ConnectionError("Remote end closed")
            except BlockingIOError:
                pass  # Socket is alive, just no data waiting — this is normal
            finally:
                _ableton_connection.sock.setblocking(True)
            return _ableton_connection
        except Exception as e:
            logger.warning(f"Existing connection is no longer valid: {str(e)}")
            try:
                _ableton_connection.disconnect()
            except:
                pass
            _ableton_connection = None
            # Passive events stop arriving while disconnected, so any cached
            # state hash may describe a Live session that has since moved on.
            try:
                recorder = get_recorder()
                if recorder:
                    recorder.invalidate_cached_state()
            except Exception:
                pass
    
    # Connection doesn't exist or is invalid, create a new one
    if _ableton_connection is None:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                logger.info(f"Connecting to Ableton at {ABLETON_HOST}:{ABLETON_PORT} (attempt {attempt}/{max_attempts})...")
                _ableton_connection = AbletonConnection(host=ABLETON_HOST, port=ABLETON_PORT)
                if _ableton_connection.connect():
                    logger.info("Created new persistent connection to Ableton")
                    return _ableton_connection
                else:
                    _ableton_connection = None
            except Exception as e:
                logger.error(f"Connection attempt {attempt} failed: {str(e)}")
                if _ableton_connection:
                    _ableton_connection.disconnect()
                    _ableton_connection = None

            if attempt < max_attempts:
                import time
                time.sleep(1.0)
        
        # If we get here, all connection attempts failed
        if _ableton_connection is None:
            logger.error("Failed to connect to Ableton after multiple attempts")
            raise Exception("Could not connect to Ableton. Make sure the Remote Script is running.")
    
    return _ableton_connection


TRACK_TYPES = ("track", "return", "master")


def _missing_features(track_type: str = "track", chain_path=None,
                      display_value=None, extra=()) -> str | None:
    """Refuse parameters an older Remote Script would silently ignore.

    A pre-1.19 script drops track_type, so a call meant for a return track
    would land on the regular track with the same index. Check first.
    """
    from .script_handshake import require_capability

    if track_type not in TRACK_TYPES:
        return f"Unknown track_type '{track_type}' — use one of {', '.join(TRACK_TYPES)}"
    needed = list(extra)
    if track_type != "track":
        needed.append("track_type")
    if chain_path:
        needed.append("chain_path")
    if display_value is not None:
        needed.append("display_value")
    for name in needed:
        missing = require_capability(name)
        if missing:
            return missing
    return None


def _compact_json(result: Any) -> str:
    return json.dumps(result, separators=(",", ":"), default=str)


PARAMETER_COLUMNS = ["index", "name", "value", "min", "max", "display", "value_items"]


def _compact_device(device: Dict[str, Any]) -> Dict[str, Any]:
    """Parameters as rows (see PARAMETER_COLUMNS) instead of verbose objects."""
    out = {k: v for k, v in device.items() if k not in ("parameters", "chains")}
    params = device.get("parameters")
    if params is not None:
        disabled = [p.get("index") for p in params if p.get("is_enabled") is False]
        if disabled:
            # Macro-controlled: setting these directly fails.
            out["macro_controlled"] = disabled
        rows = []
        for p in params:
            row = [p.get("index"), p.get("name"), round(float(p.get("value", 0.0)), 4),
                   p.get("min"), p.get("max"), p.get("display")]
            if p.get("value_items"):
                row.append(p["value_items"])
            rows.append(row)
        out["parameters"] = rows
    chains = device.get("chains")
    if chains is not None:
        out["chains"] = [
            {
                **{k: v for k, v in chain.items() if k != "devices"},
                "devices": [
                    _compact_device(d) if isinstance(d, dict) else d
                    for d in chain.get("devices", [])
                ],
            }
            for chain in chains
        ]
    return out


# Core Tool endpoints

@mcp.tool()
def set_dataset_consent(ctx: Context, consent: bool, user_said: str = "") -> str:
    """Record the user's answer to the dataset consent question.

    Call this ONLY after the user has answered in their own words. Never infer
    the answer, never call it on their behalf, and never call it because
    contributing seems helpful — consent the user did not give is not consent.
    If they have not been asked yet, ask first and wait for their reply.

    Parameters:
    - consent: True if the user agreed to contribute, False if they declined
    - user_said: The user's reply, quoted as closely as possible
    """
    # Intentionally undecorated: recording this call would mean collecting data
    # from someone who may have just declined.
    try:
        from .dataset.consent import record_consent

        state = record_consent(consent, quote=user_said)
    except Exception as e:
        logger.error(f"Could not record dataset consent: {str(e)}")
        return f"Error recording consent: {str(e)}"

    if not consent:
        return (
            "Recorded: dataset contribution declined. Nothing is uploaded, and "
            "you will not be asked again."
        )

    try:
        from .telemetry import refresh_consent_from_dataset

        refresh_consent_from_dataset()
        recorder = get_recorder()
        # Under opt-in the poller is almost never started at boot — consent did
        # not exist yet — so a grant has to start it here or passive Live events
        # would be missed for the rest of the session. Idempotent and
        # self-gating, so this is safe even if startup did start it.
        from .dataset.passive_poller import start_passive_poller

        start_passive_poller()
    except Exception as e:
        logger.error(f"Consent saved but recording failed to start: {str(e)}")
        return (
            "Consent saved, but recording could not start in this session. "
            "It will begin after a restart."
        )

    if recorder is None:
        return (
            "Consent saved, but recording is unavailable (telemetry may be "
            f"disabled). State: {state}."
        )
    return (
        "Thank you — this session is now being contributed to the open dataset. "
        "Say so at any time to stop, or set ABLETON_MCP_DISABLE_DATASET=1."
    )


@mcp.tool()
@telemetry_tool("get_session_info")
@trajectory_tool("get_session_info")
def get_session_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Ableton session

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_session_info")
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session info from Ableton: {str(e)}")
        return f"Error getting session info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_remote_script_info")
@trajectory_tool("get_remote_script_info", modifying=False)
def get_remote_script_info(ctx: Context, user_prompt: str = "") -> str:
    """
    Report Ableton Remote Script version and capabilities (handshake).

    Use this to verify the Live-side bridge matches this MCP server package.
    """
    try:
        from .remote_script_install import EXPECTED_REMOTE_SCRIPT_VERSION
        from .script_handshake import get_cached_script_info, handshake

        ableton = get_ableton_connection()
        info = handshake(ableton.send_command)
        cached = dict(get_cached_script_info() or info)
        cached["expected_version"] = EXPECTED_REMOTE_SCRIPT_VERSION
        return json.dumps(cached, indent=2)
    except Exception as e:
        logger.error(f"Error getting remote script info: {str(e)}")
        return f"Error getting remote script info: {str(e)}"

@mcp.tool()
@telemetry_tool("get_track_info")
@trajectory_tool("get_track_info")
def get_track_info(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific track in Ableton.

    Parameters:
    - track_index: The index of the track to get information about
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_track_info", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting track info from Ableton: {str(e)}")
        return f"Error getting track info: {str(e)}"


@mcp.tool()
@telemetry_tool("get_clip_notes")
@trajectory_tool("get_clip_notes")
def get_clip_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = "",
) -> str:
    """
    Read all MIDI notes from a Session-view clip.

    Returns pitch, start_time, duration, velocity, mute (and extended fields when available).

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_clip_notes")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_clip_notes",
            {"track_index": track_index, "clip_index": clip_index},
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting clip notes: {str(e)}")
        return f"Error getting clip notes: {str(e)}"


@mcp.tool()
@telemetry_tool("get_device_parameters")
@trajectory_tool("get_device_parameters")
def get_device_parameters(
    ctx: Context,
    track_index: int,
    device_index: int,
    track_type: str = "track",
    chain_path: List[int] | None = None,
    include_chains: bool = False,
    name_filter: str = "",
    compact: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Read a device's parameters, with Live's display string for each value.

    Racks list their chains (name, devices, drum-pad note) without nested
    parameters; reach a nested device with chain_path. Drum racks also list
    drum_pads: the MIDI note that triggers each filled pad.

    Parameters:
    - track_index: Track that owns the device (index within its track_type)
    - device_index: Index into the track's device chain
    - track_type: "track" (default), "return" or "master"
    - chain_path: Walk into racks: [chain_index, device_index, ...] pairs after
      device_index. [3, 0] = first device in chain 3 of the rack.
    - include_chains: Also return every nested device's parameters (large)
    - name_filter: Case-insensitive regex on parameter names, e.g. "freq|gain|output"
    - compact: Parameters as rows [index, name, value, min, max, display,
      value_items?] instead of objects (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        missing = _missing_features(track_type, chain_path)
        if missing:
            return missing
        params: Dict[str, Any] = {
            "track_index": track_index,
            "device_index": device_index,
            "include_chains": include_chains,
        }
        if track_type != "track":
            params["track_type"] = track_type
        if chain_path:
            params["chain_path"] = chain_path
        if name_filter:
            params["name_filter"] = name_filter
        ableton = get_ableton_connection()
        result = ableton.send_command("get_device_parameters", params)
        if compact and isinstance(result.get("device"), dict):
            result["device"] = _compact_device(result["device"])
            result["parameter_columns"] = PARAMETER_COLUMNS
        return _compact_json(result)
    except Exception as e:
        logger.error(f"Error getting device parameters: {str(e)}")
        return f"Error getting device parameters: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_device_parameter")
@trajectory_tool("set_device_parameter")
def set_device_parameter(
    ctx: Context,
    track_index: int,
    device_index: int,
    parameter_index: int,
    value: float | None = None,
    display_value: str | None = None,
    track_type: str = "track",
    chain_path: List[int] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set a device parameter by raw value or by the value Live displays.

    Use get_device_parameters first to discover parameter indices, ranges and
    display strings. display_value is usually easier than value: Live's raw
    values are often normalized 0..1 with a hidden curve.

    Parameters:
    - track_index: Track that owns the device (index within its track_type)
    - device_index: Index into the track's device chain
    - parameter_index: Index into device.parameters
    - value: New raw parameter value (between the parameter's min and max)
    - display_value: Target as Live shows it, e.g. "180 Hz", "1.2 kHz",
      "-6 dB", "35 %", "250 ms", or a value_items entry like "Bell".
      Overrides value.
    - track_type: "track" (default), "return" or "master"
    - chain_path: [chain_index, device_index, ...] pairs to reach a device
      inside a rack (see get_device_parameters)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        if value is None and display_value is None:
            return "Error setting device parameter: pass value or display_value"
        missing = _missing_features(track_type, chain_path, display_value)
        if missing:
            return missing
        params: Dict[str, Any] = {
            "track_index": track_index,
            "device_index": device_index,
            "parameter_index": parameter_index,
        }
        if value is not None:
            params["value"] = value
        if display_value is not None:
            params["display_value"] = display_value
        if track_type != "track":
            params["track_type"] = track_type
        if chain_path:
            params["chain_path"] = chain_path
        ableton = get_ableton_connection()
        result = ableton.send_command("set_device_parameter", params)
        old = result.get("old_display") or result.get("old_value")
        new = result.get("display") or result.get("value")
        return (
            f"Set {result.get('device_name', 'device')} › "
            f"{result.get('name', 'parameter')}: {old} → {new} "
            f"(raw {result.get('value')})"
        )
    except Exception as e:
        logger.error(f"Error setting device parameter: {str(e)}")
        return f"Error setting device parameter: {str(e)}"


@mcp.tool()
@telemetry_tool("get_session_snapshot")
@trajectory_tool("get_session_snapshot")
def get_session_snapshot(
    ctx: Context,
    include_notes: bool = True,
    include_params: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Capture a full project state snapshot for trajectory recording.

    Includes session metadata, every track (mixer, devices, session clips,
    arrangement clips), optional MIDI notes, and optional device parameters.
    Used to version musical state S_t → S_{t+1}.

    Parameters:
    - include_notes: Include MIDI note arrays in clips (default True)
    - include_params: Include device parameter values (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("get_session_snapshot")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "get_session_snapshot",
            {
                "include_notes": include_notes,
                "include_params": include_params,
            },
        )
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting session snapshot: {str(e)}")
        return f"Error getting session snapshot: {str(e)}"


@mcp.tool()
@telemetry_tool("create_midi_track")
@trajectory_tool("create_midi_track")
def create_midi_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new MIDI track in the Ableton session.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_midi_track", {"index": index})
        return (
            f"Created new MIDI track {result.get('index')}: "
            f"{result.get('name', 'unknown')}"
        )
    except Exception as e:
        logger.error(f"Error creating MIDI track: {str(e)}")
        return f"Error creating MIDI track: {str(e)}"


@mcp.tool()
@telemetry_tool("create_audio_track")
@trajectory_tool("create_audio_track")
def create_audio_track(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Create a new audio track in the Ableton session.

    Use this for recorded or imported audio (samples, stems, vocals). For MIDI
    instruments use create_midi_track instead.

    Parameters:
    - index: The index to insert the track at (-1 = end of list)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_track", {"index": index})
        return (
            f"Created new audio track {result.get('index')}: "
            f"{result.get('name', 'unknown')}"
        )
    except Exception as e:
        logger.error(f"Error creating audio track: {str(e)}")
        return f"Error creating audio track: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_track_name")
@trajectory_tool("set_track_name")
def set_track_name(ctx: Context, track_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a track.

    Parameters:
    - track_index: The index of the track to rename
    - name: The new name for the track
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_track_name", {"track_index": track_index, "name": name})
        return f"Renamed track to: {result.get('name', name)}"
    except Exception as e:
        logger.error(f"Error setting track name: {str(e)}")
        return f"Error setting track name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_clip")
@trajectory_tool("create_clip")
def create_clip(ctx: Context, track_index: int, clip_index: int, length: float = 4.0, user_prompt: str = "") -> str:
    """
    Create a new MIDI clip in the specified track and clip slot.

    Parameters:
    - track_index: The index of the track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - length: The length of the clip in beats (default: 4.0)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_clip", {
            "track_index": track_index, 
            "clip_index": clip_index, 
            "length": length
        })
        return f"Created new clip at track {track_index}, slot {clip_index} with length {length} beats"
    except Exception as e:
        logger.error(f"Error creating clip: {str(e)}")
        return f"Error creating clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("create_audio_clip")
@trajectory_tool("create_audio_clip")
def create_audio_clip(ctx: Context, track_index: int, clip_index: int, path: str, user_prompt: str = "") -> str:
    """
    Create a new audio clip in an audio track's clip slot by importing a file.

    Requires Ableton Live 12.0.5 or newer — the underlying
    ClipSlot.create_audio_clip Live API was introduced in 12.0.5 and is not
    available in earlier 12.0.x releases.

    Parameters:
    - track_index: The index of the audio track to create the clip in
    - clip_index: The index of the clip slot to create the clip in
    - path: Absolute path to a supported audio file (e.g. a .wav). The target
      track must be an audio track and the clip slot must be empty.
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("create_audio_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "path": path
        })
        return f"Created audio clip '{result.get('name', 'clip')}' at track {track_index}, slot {clip_index} (length {result.get('length', '?')} beats)"
    except Exception as e:
        logger.error(f"Error creating audio clip: {str(e)}")
        return f"Error creating audio clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("add_notes_to_clip", capture_notes=True)
@trajectory_tool("add_notes_to_clip")
def add_notes_to_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Union[int, float, bool]]],
    user_prompt: str = ""
) -> str:
    """
    Add MIDI notes to a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - notes: List of note dictionaries, each with pitch, start_time, duration, velocity, and mute
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("add_notes_to_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
            "notes": notes
        })
        return f"Added {len(notes)} notes to clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error adding notes to clip: {str(e)}")
        return f"Error adding notes to clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("clear_notes_from_clip")
def clear_notes_from_clip(
    ctx: Context,
    track_index: int,
    clip_index: int,
    user_prompt: str = ""
) -> str:
    """
    Remove all MIDI notes from a Session clip.

    Writes are additive (add_notes_to_clip only appends), so to truly *modify*
    a clip you clear it first, then add the new notes. Use this with
    get_clip_notes and add_notes_to_clip for a real read -> modify -> write
    loop: read the notes, edit the list, clear_notes_from_clip, then
    add_notes_to_clip the edited notes.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("clear_notes_from_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("clear_notes_from_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return "Cleared {n} note(s) from clip '{name}' (track {t}, slot {c})".format(
            n=result.get("cleared_count", "?"),
            name=result.get("clip_name", "clip"),
            t=track_index,
            c=clip_index,
        )
    except Exception as e:
        logger.error(f"Error clearing notes from clip: {str(e)}")
        return f"Error clearing notes from clip: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_clip_name")
@trajectory_tool("set_clip_name")
def set_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed clip at track {track_index}, slot {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting clip name: {str(e)}")
        return f"Error setting clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_arrangement_clip_name")
@trajectory_tool("set_arrangement_clip_name")
def set_arrangement_clip_name(ctx: Context, track_index: int, clip_index: int, name: str, user_prompt: str = "") -> str:
    """
    Set the name of a clip placed in the Arrangement timeline.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip within track.arrangement_clips, in the
      same order returned by get_arrangement_clips (i.e. ordered by start_time)
    - name: The new name for the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_arrangement_clip_name", {
            "track_index": track_index,
            "clip_index": clip_index,
            "name": name
        })
        return f"Renamed arrangement clip at track {track_index}, index {clip_index} to '{name}'"
    except Exception as e:
        logger.error(f"Error setting arrangement clip name: {str(e)}")
        return f"Error setting arrangement clip name: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("set_tempo")
@trajectory_tool("set_tempo")
def set_tempo(ctx: Context, tempo: float, user_prompt: str = "") -> str:
    """
    Set the tempo of the Ableton session.

    Parameters:
    - tempo: The new tempo in BPM
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_tempo", {"tempo": tempo})
        return f"Set tempo to {tempo} BPM"
    except Exception as e:
        logger.error(f"Error setting tempo: {str(e)}")
        return f"Error setting tempo: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("load_instrument_or_effect")
@trajectory_tool("load_instrument_or_effect")
def load_instrument_or_effect(
    ctx: Context,
    track_index: int,
    uri: str,
    track_type: str = "track",
    position: int | None = None,
    user_prompt: str = "",
) -> str:
    """
    Load an instrument or effect onto a track using its URI.

    Find URIs with search_browser. Live inserts the device after the selected
    one; pass position to put it at a specific index in the chain.

    Parameters:
    - track_index: The track to load onto (index within its track_type)
    - uri: The URI of the instrument or effect to load (e.g., 'query:Synths#Instrument%20Rack:Bass:FileId_5116')
    - track_type: "track" (default), "return" or "master"
    - position: Device index to move the loaded device to (0 = first)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        missing = _missing_features(track_type)
        if missing:
            return missing
        params: Dict[str, Any] = {"track_index": track_index, "item_uri": uri}
        if track_type != "track":
            params["target"] = track_type
        if position is not None:
            params["position"] = position
        ableton = get_ableton_connection()
        result = ableton.send_command("load_browser_item", params)

        if not result.get("loaded", False):
            return f"Failed to load instrument with URI '{uri}'"
        where = f"{track_type} {track_index}" if track_type != "master" else "master"
        devices = result.get("devices") or []
        chain = ", ".join(f"{i}:{name}" for i, name in enumerate(devices))
        device_index = result.get("device_index")
        if device_index is None:
            return (
                f"Loaded '{result.get('item_name', uri)}' on {where}. "
                f"Devices: {chain or 'unknown'}"
            )
        return (
            f"Loaded '{result.get('item_name', uri)}' on {where} at device "
            f"index {device_index}. Devices: {chain}"
        )
    except Exception as e:
        logger.error(f"Error loading instrument by URI: {str(e)}")
        return f"Error loading instrument by URI: {str(e)}"

@mcp.tool()
@telemetry_tool("fire_clip")
@trajectory_tool("fire_clip")
def fire_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Start playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("fire_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Started playing clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error firing clip: {str(e)}")
        return f"Error firing clip: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_clip")
@trajectory_tool("stop_clip")
def stop_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Stop playing a clip.

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot containing the clip
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_clip", {
            "track_index": track_index,
            "clip_index": clip_index
        })
        return f"Stopped clip at track {track_index}, slot {clip_index}"
    except Exception as e:
        logger.error(f"Error stopping clip: {str(e)}")
        return f"Error stopping clip: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_clip")
def delete_clip(ctx: Context, track_index: int, clip_index: int, user_prompt: str = "") -> str:
    """
    Delete the clip in the given clip slot, freeing it for reuse.

    Use this before create_clip when you want to overwrite an existing clip
    (create_clip itself refuses to write into an occupied slot).

    Parameters:
    - track_index: The index of the track containing the clip
    - clip_index: The index of the clip slot to clear
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("delete_clip")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command("delete_clip", {
            "track_index": track_index,
            "clip_index": clip_index,
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error deleting clip: {str(e)}")
        return f"Error deleting clip: {str(e)}"


@mcp.tool()
@telemetry_tool("start_playback")
@trajectory_tool("start_playback")
def start_playback(ctx: Context, user_prompt: str = "") -> str:
    """Start playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("start_playback")
        return "Started playback"
    except Exception as e:
        logger.error(f"Error starting playback: {str(e)}")
        return f"Error starting playback: {str(e)}"

@mcp.tool()
@telemetry_tool("stop_playback")
@trajectory_tool("stop_playback")
def stop_playback(ctx: Context, user_prompt: str = "") -> str:
    """Stop playing the Ableton session.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("stop_playback")
        return "Stopped playback"
    except Exception as e:
        logger.error(f"Error stopping playback: {str(e)}")
        return f"Error stopping playback: {str(e)}"

@mcp.tool()
@rich_telemetry_tool("get_browser_tree")
@trajectory_tool("get_browser_tree")
def get_browser_tree(ctx: Context, category_type: str = "all", user_prompt: str = "") -> str:
    """
    Get a hierarchical tree of browser categories from Ableton.

    Parameters:
    - category_type: Type of categories to get ('all', 'instruments', 'sounds', 'drums', 'audio_effects', 'midi_effects')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_tree", {
            "category_type": category_type
        })
        
        # Check if we got any categories
        if "available_categories" in result and len(result.get("categories", [])) == 0:
            available_cats = result.get("available_categories", [])
            return (f"No categories found for '{category_type}'. "
                   f"Available browser categories: {', '.join(available_cats)}")
        
        # Format the tree in a more readable way
        total_folders = result.get("total_folders", 0)
        formatted_output = f"Browser tree for '{category_type}' (showing {total_folders} folders):\n\n"
        
        def format_tree(item, indent=0):
            output = ""
            if item:
                prefix = "  " * indent
                name = item.get("name", "Unknown")
                path = item.get("path", "")
                has_more = item.get("has_more", False)
                
                # Add this item
                output += f"{prefix}• {name}"
                if path:
                    output += f" (path: {path})"
                if has_more:
                    output += " [...]"
                output += "\n"
                
                # Add children
                for child in item.get("children", []):
                    output += format_tree(child, indent + 1)
            return output
        
        # Format each category
        for category in result.get("categories", []):
            formatted_output += format_tree(category)
            formatted_output += "\n"
        
        return formatted_output
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        else:
            logger.error(f"Error getting browser tree: {error_msg}")
            return f"Error getting browser tree: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("get_browser_items_at_path")
@trajectory_tool("get_browser_items_at_path")
def get_browser_items_at_path(
    ctx: Context,
    path: str,
    offset: int = 0,
    limit: int = 100,
    user_prompt: str = "",
) -> str:
    """
    Get browser items at a specific path in Ableton's browser, one page at a time.

    To find something by name, search_browser is usually faster.

    Parameters:
    - path: Path in the format "category/folder/subfolder"
            where category is one of the available browser categories in Ableton
    - offset: First item to return (for paging; see has_more and total)
    - limit: Maximum items to return (default 100)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import script_has_capability

        params: Dict[str, Any] = {"path": path}
        if script_has_capability("browser_paging"):
            params.update({"offset": offset, "limit": limit})
        ableton = get_ableton_connection()
        result = ableton.send_command("get_browser_items_at_path", params)
        
        # Check if there was an error with available categories
        if "error" in result and "available_categories" in result:
            error = result.get("error", "")
            available_cats = result.get("available_categories", [])
            return (f"Error: {error}\n"
                   f"Available browser categories: {', '.join(available_cats)}")
        
        return _compact_json(result)
    except Exception as e:
        error_msg = str(e)
        if "Browser is not available" in error_msg:
            logger.error(f"Browser is not available in Ableton: {error_msg}")
            return f"Error: The Ableton browser is not available. Make sure Ableton Live is fully loaded and try again."
        elif "Could not access Live application" in error_msg:
            logger.error(f"Could not access Live application: {error_msg}")
            return f"Error: Could not access the Ableton Live application. Make sure Ableton Live is running and the Remote Script is loaded."
        elif "Unknown or unavailable category" in error_msg:
            logger.error(f"Invalid browser category: {error_msg}")
            return f"Error: {error_msg}. Please check the available categories using get_browser_tree."
        elif "Path part" in error_msg and "not found" in error_msg:
            logger.error(f"Path not found: {error_msg}")
            return f"Error: {error_msg}. Please check the path and try again."
        else:
            logger.error(f"Error getting browser items at path: {error_msg}")
            return f"Error getting browser items at path: {error_msg}"

@mcp.tool()
@rich_telemetry_tool("load_drum_kit")
@trajectory_tool("load_drum_kit")
def load_drum_kit(ctx: Context, track_index: int, rack_uri: str, kit_path: str, user_prompt: str = "") -> str:
    """
    Load a drum rack and then load a specific drum kit into it.

    Parameters:
    - track_index: The index of the track to load on
    - rack_uri: The URI of the drum rack to load (e.g., 'Drums/Drum Rack')
    - kit_path: Path to the drum kit inside the browser (e.g., 'drums/acoustic/kit1')
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        
        # Step 1: Load the drum rack
        result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": rack_uri
        })
        
        if not result.get("loaded", False):
            return f"Failed to load drum rack with URI '{rack_uri}'"
        
        # Step 2: Get the drum kit items at the specified path
        kit_result = ableton.send_command("get_browser_items_at_path", {
            "path": kit_path
        })
        
        if "error" in kit_result:
            return f"Loaded drum rack but failed to find drum kit: {kit_result.get('error')}"
        
        # Step 3: Find a loadable drum kit
        kit_items = kit_result.get("items", [])
        loadable_kits = [item for item in kit_items if item.get("is_loadable", False)]
        
        if not loadable_kits:
            return f"Loaded drum rack but no loadable drum kits found at '{kit_path}'"
        
        # Step 4: Load the first loadable kit
        kit_uri = loadable_kits[0].get("uri")
        load_result = ableton.send_command("load_browser_item", {
            "track_index": track_index,
            "item_uri": kit_uri
        })
        
        return f"Loaded drum rack and kit '{loadable_kits[0].get('name')}' on track {track_index}"
    except Exception as e:
        logger.error(f"Error loading drum kit: {str(e)}")
        return f"Error loading drum kit: {str(e)}"

# ── Arrangement view tools ────────────────────────────────────────────────────

@mcp.tool()
@telemetry_tool("switch_to_arrangement_view")
@trajectory_tool("switch_to_arrangement_view")
def switch_to_arrangement_view(ctx: Context, user_prompt: str = "") -> str:
    """Switch Ableton's main window to the Arrangement view.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        ableton.send_command("switch_to_arrangement_view")
        return "Switched to Arrangement view"
    except Exception as e:
        logger.error(f"Error switching to arrangement view: {str(e)}")
        return f"Error switching to arrangement view: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("set_arrangement_time")
@trajectory_tool("set_arrangement_time")
def set_arrangement_time(ctx: Context, time: float, user_prompt: str = "") -> str:
    """
    Move the arrangement playhead to a specific position.

    Parameters:
    - time: Position in beats from the start of the arrangement (e.g. 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("set_current_song_time", {"time": time})
        return f"Playhead moved to beat {result.get('current_song_time', time)}"
    except Exception as e:
        logger.error(f"Error setting arrangement time: {str(e)}")
        return f"Error setting arrangement time: {str(e)}"


@mcp.tool()
@telemetry_tool("get_arrangement_clips")
@trajectory_tool("get_arrangement_clips")
def get_arrangement_clips(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    List all clips placed in the Arrangement timeline for a track.

    Returns each clip's name, start_time, end_time, length, and type.

    Parameters:
    - track_index: The index of the track to inspect
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command("get_arrangement_clips", {"track_index": track_index})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting arrangement clips: {str(e)}")
        return f"Error getting arrangement clips: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("duplicate_to_arrangement")
@trajectory_tool("duplicate_to_arrangement")
def duplicate_to_arrangement(
    ctx: Context,
    track_index: int,
    clip_index: int,
    destination_time: float,
    user_prompt: str = ""
) -> str:
    """
    Copy a Session-view clip into the Arrangement timeline.

    Uses Live's track.duplicate_clip_to_arrangement() API (Live 11 / 12).
    The clip is placed at destination_time beats from the start of the
    arrangement on the same track it lives in.

    Typical workflow:
      1. create_clip / add_notes_to_clip to build a Session clip
      2. Call duplicate_to_arrangement once per bar/section you need
      3. Call switch_to_arrangement_view to confirm the result in Live

    Parameters:
    - track_index:       Index of the track that owns the Session clip
    - clip_index:        Index of the clip slot in that track (Session view)
    - destination_time:  Beat position in the arrangement to place the clip
                         (e.g. 0.0 = start, 8.0 = bar 3 in 4/4)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "duplicate_session_clip_to_arrangement",
            {
                "track_index": track_index,
                "clip_index": clip_index,
                "destination_time": destination_time
            }
        )
        clip_name = result.get("clip_name", "clip")
        track_name = result.get("track_name", f"track {track_index}")
        return (
            f"Duplicated '{clip_name}' from Session slot {clip_index} "
            f"on '{track_name}' to arrangement at beat {destination_time}"
        )
    except Exception as e:
        logger.error(f"Error duplicating clip to arrangement: {str(e)}")
        return f"Error duplicating clip to arrangement: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("create_locator")
def create_locator(
    ctx: Context,
    name: str,
    time: float,
    user_prompt: str = ""
) -> str:
    """
    Create a named locator (cue point) in the Arrangement at a beat position.

    If a locator already exists at that beat (within ~1e-3 tolerance) it is
    renamed instead of toggled off. Time is in beats from the start of the
    arrangement (e.g. 0.0 = start, 16.0 = bar 5 in 4/4).

    Parameters:
    - name: The locator label (e.g. "Chorus", "Verse 1", "Drop")
    - time: Beat position where the locator should sit
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        from .script_handshake import require_capability

        missing = require_capability("create_locator")
        if missing:
            return missing
        ableton = get_ableton_connection()
        result = ableton.send_command(
            "create_locator",
            {"name": name, "time": time}
        )
        return (
            f"Locator '{result.get('name', name)}' set at beat "
            f"{result.get('time', time)}"
        )
    except Exception as e:
        logger.error(f"Error creating locator: {str(e)}")
        return f"Error creating locator: {str(e)}"


# ── Dataset / preference tools (Supabase trajectory recording) ─────────────────

@mcp.tool()
@telemetry_tool("submit_intent")
@trajectory_tool("submit_intent", modifying=False)
def submit_intent(
    ctx: Context,
    text: str,
    level: int = 5,
    user_prompt: str = "",
) -> str:
    """
    Record the human's creative intent for subsequent actions in this session.

    Call this before a sequence of edits so trajectory steps are conditioned on intent.
    Levels: 1=atomic, 2=musical op, 3=section, 4=song, 5=creative goal.

    Requires telemetry consent. Data is stored in Supabase (same project as telemetry).

    Parameters:
    - text: Natural-language intent (e.g. "make the chorus feel bigger")
    - level: Hierarchical intent level 1–5 (default 5)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.set_intent(text=text, level=level)
        return (
            f"Recorded intent {event.intent_id} (level {level}): {text!r}. "
            f"Session: {recorder.session_id}"
        )
    except Exception as e:
        logger.error(f"Error submitting intent: {str(e)}")
        return f"Error submitting intent: {str(e)}"


@mcp.tool()
@telemetry_tool("rate_last_action")
@trajectory_tool("rate_last_action", modifying=False)
def rate_last_action(
    ctx: Context,
    rating: str,
    tags: str = "",
    note: str = "",
    user_prompt: str = "",
) -> str:
    """
    Rate the most recent recorded action (or the track after an edit).

    Ratings: better | same | worse | keep | reject | thumbs_up | thumbs_down
    Optional tags (comma-separated): groove, harmony, melody, sound, arrangement,
    energy, mix, emotion

    Requires telemetry consent.

    Parameters:
    - rating: Preference label
    - tags: Comma-separated aspect tags
    - note: Optional free-text reason
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        event = recorder.record_preference(
            rating=rating.strip().lower(),
            tags=tag_list,
            note=note or None,
        )
        target = event.target_action_id or "none"
        return f"Recorded preference {rating!r} for action {target}"
    except Exception as e:
        logger.error(f"Error rating action: {str(e)}")
        return f"Error rating action: {str(e)}"


@mcp.tool()
@telemetry_tool("prefer_candidate")
@trajectory_tool("prefer_candidate", modifying=False)
def prefer_candidate(
    ctx: Context,
    candidate_a: str,
    candidate_b: str,
    winner: str,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Record a pairwise preference between two candidate actions or auditions.

    winner should be 'a', 'b', or an explicit candidate id matching A or B.

    Requires telemetry consent.

    Parameters:
    - candidate_a: Id / URI / label for option A
    - candidate_b: Id / URI / label for option B
    - winner: 'a', 'b', or the winning id
    - reason: Optional reason ("C has the right attack")
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        w = winner.strip().lower()
        if w == "a":
            w = candidate_a
        elif w == "b":
            w = candidate_b
        event = recorder.record_preference(
            rating="pairwise",
            winner=w,
            candidate_a=candidate_a,
            candidate_b=candidate_b,
            note=reason or None,
        )
        return f"Recorded pairwise preference: winner={w!r} (event {event.event_id})"
    except Exception as e:
        logger.error(f"Error recording pairwise preference: {str(e)}")
        return f"Error recording pairwise preference: {str(e)}"


@mcp.tool()
@telemetry_tool("reject_last_action")
@trajectory_tool("reject_last_action", modifying=False)
def reject_last_action(
    ctx: Context,
    reason: str = "",
    user_prompt: str = "",
) -> str:
    """
    Mark the last action as rejected (exploration / preference boundary).

    Use when the human undoes or discards an agent edit. Requires telemetry consent.

    Parameters:
    - reason: Optional why it was rejected
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_preference(
            rating="reject",
            note=reason or None,
        )
        return f"Recorded rejection for action {event.target_action_id or 'none'}"
    except Exception as e:
        logger.error(f"Error rejecting action: {str(e)}")
        return f"Error rejecting action: {str(e)}"


@mcp.tool()
@rich_telemetry_tool("record_audition")
@trajectory_tool("record_audition", modifying=False)
def record_audition(
    ctx: Context,
    uri: str,
    kept: bool = False,
    search_query: str = "",
    dwell_ms: float = 0.0,
    user_prompt: str = "",
) -> str:
    """
    Log a browser/preset/sample audition (keep or reject) for preference learning.

    Call once per candidate auditioned. Does not load the device — use
    load_instrument_or_effect when kept=True and you want to commit.

    Requires telemetry consent.

    Parameters:
    - uri: Browser item URI (or stable preset/sample id)
    - kept: Whether this candidate was kept
    - search_query: Optional search text that led here (e.g. "analog bass")
    - dwell_ms: Optional time spent auditioning
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    recorder = get_recorder()
    if recorder is None:
        return (
            "Dataset recording is off (telemetry disabled or user has not consented)."
        )
    try:
        event = recorder.record_audition(
            uri=uri,
            kept=kept,
            search_query=search_query or None,
            dwell_ms=dwell_ms if dwell_ms > 0 else None,
        )
        status = "kept" if kept else "rejected"
        return f"Recorded audition ({status}) uri={uri!r} event={event.event_id}"
    except Exception as e:
        logger.error(f"Error recording audition: {str(e)}")
        return f"Error recording audition: {str(e)}"



# ── Mixer, routing, device management and editing ─────────────────────────────

def _send(command: str, params: Dict[str, Any] | None = None,
          capability: str | None = None) -> Dict[str, Any] | str:
    """Send a command after checking the Remote Script supports it.

    Returns the result dict, or an error string to hand straight back.
    """
    from .script_handshake import require_capability

    missing = require_capability(capability or command)
    if missing:
        return missing
    return get_ableton_connection().send_command(command, params or {})


def _volume_display(db: float) -> str:
    return "-inf dB" if db <= -70 else f"{db} dB"


def _send_index(key: str) -> int:
    """Send keys may be an index ("0") or a return letter ("A")."""
    key = str(key).strip()
    if key.isdigit():
        return int(key)
    if len(key) == 1 and key.isalpha():
        return ord(key.upper()) - ord("A")
    raise ValueError(f"Send key '{key}' is neither an index nor a return letter")


@mcp.tool()
@telemetry_tool("get_mixer")
@trajectory_tool("get_mixer", modifying=False)
def get_mixer(ctx: Context, user_prompt: str = "") -> str:
    """
    Read volume, pan, sends, mute and solo for every track, return and master.

    Volume and send levels include Live's display string ("-6.0 dB").

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("get_mixer")
        if isinstance(result, str):
            return result

        def line(t: Dict[str, Any]) -> str:
            label = {"track": "", "return": "return ", "master": ""}[t["track_type"]]
            parts = [f"{label}{t['index']} {t['name']}" if t["track_type"] != "master" else "master",
                     f"vol {t.get('volume_display')}", f"pan {t.get('panning_display')}"]
            for send in t.get("sends", []):
                parts.append(f"send {chr(ord('A') + send['index'])} {send.get('display')}")
            if t.get("mute"):
                parts.append("MUTED")
            if t.get("solo"):
                parts.append("SOLO")
            return " | ".join(parts)

        rows = [line(t) for t in result.get("tracks", [])]
        rows += [line(t) for t in result.get("returns", [])]
        if result.get("master"):
            rows.append(line(result["master"]))
        return "\n".join(rows)
    except Exception as e:
        logger.error(f"Error reading mixer: {str(e)}")
        return f"Error reading mixer: {str(e)}"


@mcp.tool()
@telemetry_tool("set_mixer")
@trajectory_tool("set_mixer")
def set_mixer(
    ctx: Context,
    track_index: int,
    track_type: str = "track",
    volume_db: float | None = None,
    pan: float | None = None,
    sends_db: Dict[str, float] | None = None,
    mute: bool | None = None,
    solo: bool | None = None,
    arm: bool | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set a track's fader, pan, send levels, mute, solo or arm in one call.

    Only the arguments you pass change. Works on tracks, return tracks and
    the master.

    Parameters:
    - track_index: Track index within its track_type (ignored for master)
    - track_type: "track" (default), "return" or "master"
    - volume_db: Fader level in dB, e.g. -6.0 (0 = unity, max +6, -70 or lower = -inf)
    - pan: -1.0 (hard left) to 1.0 (hard right), 0 = center
    - sends_db: Send levels in dB keyed by return letter or index,
      e.g. {"A": -12, "B": -18}; -70 or lower = off
    - mute / solo: Mute or solo the track (not available on master)
    - arm: Arm the track for recording
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        uses_display = volume_db is not None or bool(sends_db)
        missing = _missing_features(
            track_type, display_value="" if uses_display else None)
        if missing:
            return missing
        base: Dict[str, Any] = {"track_index": track_index}
        if track_type != "track":
            base["track_type"] = track_type
        changes = []
        if volume_db is not None:
            r = _send("set_mixer_value", {**base, "target": "volume",
                                          "display_value": _volume_display(volume_db)})
            if isinstance(r, str):
                return r
            changes.append(f"volume {r.get('old_display')} → {r.get('display')}")
        if pan is not None:
            r = _send("set_mixer_value", {**base, "target": "panning",
                                          "value": max(-1.0, min(1.0, float(pan)))})
            if isinstance(r, str):
                return r
            changes.append(f"pan {r.get('old_display')} → {r.get('display')}")
        for key, db in (sends_db or {}).items():
            index = _send_index(key)
            r = _send("set_mixer_value", {**base, "target": "send", "send_index": index,
                                          "display_value": _volume_display(db)})
            if isinstance(r, str):
                return r
            changes.append(
                f"send {chr(ord('A') + index)} {r.get('old_display')} → {r.get('display')}")
        if mute is not None or solo is not None or arm is not None:
            state = {k: v for k, v in (("mute", mute), ("solo", solo), ("arm", arm))
                     if v is not None}
            r = _send("set_track_state", {**base, **state})
            if isinstance(r, str):
                return r
            changes.extend(f"{k} {v}" for k, v in r.items())
        if not changes:
            return "Nothing to change: pass volume_db, pan, sends_db, mute, solo or arm"
        where = "master" if track_type == "master" else f"{track_type} {track_index}"
        return f"{where}: " + "; ".join(changes)
    except Exception as e:
        logger.error(f"Error setting mixer: {str(e)}")
        return f"Error setting mixer: {str(e)}"


@mcp.tool()
@telemetry_tool("get_meters")
@trajectory_tool("get_meters", modifying=False)
def get_meters(ctx: Context, seconds: float = 2.0, user_prompt: str = "") -> str:
    """
    Sample output meters on every track, return and master while Live plays.

    Reports peak and average per track on Live's meter scale (0..1, 1.0 =
    full scale; not linear amplitude). Use it to compare levels between
    tracks and to check the master for clipping. Start playback first.

    Parameters:
    - seconds: How long to listen, 0.1–10 (default 2)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("get_meters", {"seconds": seconds})
        if isinstance(result, str):
            return result
        rows = []
        if not result.get("is_playing"):
            rows.append("Warning: Live is not playing, so meters read silence.")
        for m in result.get("meters", []):
            name = "master" if m["track_type"] == "master" else (
                f"{'return ' if m['track_type'] == 'return' else ''}{m['index']} {m['name']}")
            rows.append(f"{name}: peak {m['peak']:.3f}, avg {m['average']:.3f}")
        rows.append(f"({result.get('scale')}; sampled {result.get('seconds')} s)")
        return "\n".join(rows)
    except Exception as e:
        logger.error(f"Error reading meters: {str(e)}")
        return f"Error reading meters: {str(e)}"


@mcp.tool()
@telemetry_tool("get_track_routing")
@trajectory_tool("get_track_routing", modifying=False)
def get_track_routing(
    ctx: Context, track_index: int, track_type: str = "track", user_prompt: str = ""
) -> str:
    """
    Read a track's input/output routing and the options available for each.

    Parameters:
    - track_index: Track index within its track_type
    - track_type: "track" (default), "return" or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        missing = _missing_features(track_type)
        if missing:
            return missing
        params: Dict[str, Any] = {"track_index": track_index}
        if track_type != "track":
            params["track_type"] = track_type
        result = _send("get_track_routing", params)
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error getting track routing: {str(e)}")
        return f"Error getting track routing: {str(e)}"


@mcp.tool()
@telemetry_tool("set_track_routing")
@trajectory_tool("set_track_routing")
def set_track_routing(
    ctx: Context,
    track_index: int,
    direction: str,
    type_name: str = "",
    channel_name: str = "",
    track_type: str = "track",
    user_prompt: str = "",
) -> str:
    """
    Route a track's input or output, e.g. feed a track from another track.

    Names match the options get_track_routing lists (exact, then substring,
    case-insensitive). For a device's sidechain input (Compressor ducking
    to the kick), use set_device_sidechain.

    Parameters:
    - track_index: Track index within its track_type
    - direction: "input" or "output"
    - type_name: Routing type, e.g. "Kick", "Ext. In", "Master", "Sends Only"
    - channel_name: Channel within that type, e.g. "Post FX", "Pre FX", "1/2"
    - track_type: "track" (default), "return" or "master"
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        extra = ["routing_channels"] if channel_name else []
        missing = _missing_features(track_type, extra=extra)
        if missing:
            return missing
        params: Dict[str, Any] = {
            "track_index": track_index, "direction": direction, "type_name": type_name,
        }
        if channel_name:
            params["channel_name"] = channel_name
        if track_type != "track":
            params["track_type"] = track_type
        result = _send("set_track_routing", params)
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error setting track routing: {str(e)}")
        return f"Error setting track routing: {str(e)}"


def _device_target(track_index: int, device_index: int, track_type: str,
                   chain_path) -> Dict[str, Any]:
    params: Dict[str, Any] = {"track_index": track_index, "device_index": device_index}
    if track_type != "track":
        params["track_type"] = track_type
    if chain_path:
        params["chain_path"] = chain_path
    return params


def _sidechain_summary(result: Dict[str, Any]) -> str:
    line = f"{result.get('device_name')} sidechain: {result.get('source')}"
    if result.get("channel"):
        line += f" / {result['channel']}"
    if "enabled" in result:
        line += f" ({'on' if result['enabled'] else 'off'})"
    sources = ", ".join(result.get("available_sources", []))
    return f"{line}\nSources: {sources}"


@mcp.tool()
@telemetry_tool("get_device_sidechain")
@trajectory_tool("get_device_sidechain", modifying=False)
def get_device_sidechain(
    ctx: Context,
    track_index: int,
    device_index: int,
    track_type: str = "track",
    chain_path: List[int] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Read a device's sidechain source and the sources it can take.

    Works on devices that expose sidechain routing to Remote Scripts
    (Compressor; Gate and Auto Filter where Live exposes it). Other devices
    return an error naming what the device does expose.

    Parameters:
    - track_index: Track index within its track_type
    - device_index: Index into the track's device chain
    - track_type: "track" (default), "return" or "master"
    - chain_path: [chain_index, device_index, ...] pairs to reach a device inside a rack
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        missing = _missing_features(track_type, chain_path)
        if missing:
            return missing
        result = _send("get_device_sidechain",
                       _device_target(track_index, device_index, track_type, chain_path))
        return result if isinstance(result, str) else _sidechain_summary(result)
    except Exception as e:
        logger.error(f"Error reading device sidechain: {str(e)}")
        return f"Error reading device sidechain: {str(e)}"


@mcp.tool()
@telemetry_tool("set_device_sidechain")
@trajectory_tool("set_device_sidechain")
def set_device_sidechain(
    ctx: Context,
    track_index: int,
    device_index: int,
    source_track: int | None = None,
    source_track_type: str = "track",
    source: str = "",
    channel: str = "",
    enable: bool = True,
    track_type: str = "track",
    chain_path: List[int] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Feed a Compressor, Gate or Auto Filter's sidechain from another track.

    The classic duck: set_device_sidechain(track_index=<bass>,
    device_index=<its Compressor>, source_track=<kick>). Also switches the
    device's sidechain ("S/C On") on, unless enable is False.

    Parameters:
    - track_index: Track that holds the device, within its track_type
    - device_index: Index into the track's device chain
    - source_track: Index of the track to listen to (matched by its name)
    - source_track_type: "track" (default) or "return" for source_track
    - source: Or a source name as get_device_sidechain lists it, e.g. "6-Kick", "No Input"
    - channel: Tap point within the source, e.g. "Post FX", "Pre FX", "Post Mixer"
    - enable: Turn the sidechain on (True, default) or off (False)
    - track_type: "track" (default), "return" or "master" for the device's track
    - chain_path: [chain_index, device_index, ...] pairs to reach a device inside a rack
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        if source_track_type not in ("track", "return"):
            return "source_track_type must be 'track' or 'return'"
        missing = _missing_features(track_type, chain_path)
        if missing:
            return missing
        params = _device_target(track_index, device_index, track_type, chain_path)
        params["enable"] = enable
        if source_track is not None:
            params["source_track_index"] = source_track
            if source_track_type != "track":
                params["source_track_type"] = source_track_type
        elif source:
            params["source"] = source
        if channel:
            params["channel_name"] = channel
        result = _send("set_device_sidechain", params)
        return result if isinstance(result, str) else _sidechain_summary(result)
    except Exception as e:
        logger.error(f"Error setting device sidechain: {str(e)}")
        return f"Error setting device sidechain: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_device")
@trajectory_tool("delete_device")
def delete_device(
    ctx: Context,
    track_index: int,
    device_index: int,
    track_type: str = "track",
    chain_path: List[int] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Delete a device from a track, or from inside a rack via chain_path.

    Parameters:
    - track_index: Track index within its track_type
    - device_index: Index into the track's device chain
    - track_type: "track" (default), "return" or "master"
    - chain_path: [chain_index, device_index, ...] pairs to reach a nested device
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        missing = _missing_features(track_type, chain_path)
        if missing:
            return missing
        params: Dict[str, Any] = {"track_index": track_index, "device_index": device_index}
        if track_type != "track":
            params["track_type"] = track_type
        if chain_path:
            params["chain_path"] = chain_path
        result = _send("delete_device", params)
        if isinstance(result, str):
            return result
        remaining = ", ".join(f"{i}:{n}" for i, n in enumerate(result.get("devices", [])))
        return f"Deleted {result.get('deleted')}. Devices now: {remaining or 'none'}"
    except Exception as e:
        logger.error(f"Error deleting device: {str(e)}")
        return f"Error deleting device: {str(e)}"


@mcp.tool()
@telemetry_tool("move_device")
@trajectory_tool("move_device")
def move_device(
    ctx: Context,
    track_index: int,
    device_index: int,
    new_index: int,
    track_type: str = "track",
    chain_path: List[int] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Reorder a device within its track (or rack chain).

    Parameters:
    - track_index: Track index within its track_type
    - device_index: Current index of the device
    - new_index: Index it should end up at
    - track_type: "track" (default), "return" or "master"
    - chain_path: [chain_index, device_index, ...] pairs to reach a nested device
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        missing = _missing_features(track_type, chain_path)
        if missing:
            return missing
        params: Dict[str, Any] = {
            "track_index": track_index, "device_index": device_index, "new_index": new_index,
        }
        if track_type != "track":
            params["track_type"] = track_type
        if chain_path:
            params["chain_path"] = chain_path
        result = _send("move_device", params)
        if isinstance(result, str):
            return result
        order = ", ".join(f"{i}:{n}" for i, n in enumerate(result.get("devices", [])))
        return f"Moved {result.get('moved')}. Devices now: {order}"
    except Exception as e:
        logger.error(f"Error moving device: {str(e)}")
        return f"Error moving device: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_track")
@trajectory_tool("delete_track")
def delete_track(ctx: Context, track_index: int, user_prompt: str = "") -> str:
    """
    Delete a track and everything on it. Undo with undo if it was a mistake.

    Parameters:
    - track_index: Index of the track to delete
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("delete_track", {"track_index": track_index})
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error deleting track: {str(e)}")
        return f"Error deleting track: {str(e)}"


@mcp.tool()
@telemetry_tool("fire_scene")
@trajectory_tool("fire_scene", modifying=False)
def fire_scene(ctx: Context, index: int, user_prompt: str = "") -> str:
    """
    Launch every clip in a scene (a row of the Session view).

    Parameters:
    - index: Scene index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("fire_scene", {"index": index})
        return result if isinstance(result, str) else f"Fired scene {index}"
    except Exception as e:
        logger.error(f"Error firing scene: {str(e)}")
        return f"Error firing scene: {str(e)}"


@mcp.tool()
@telemetry_tool("create_scene")
@trajectory_tool("create_scene")
def create_scene(ctx: Context, index: int = -1, user_prompt: str = "") -> str:
    """
    Insert an empty scene.

    Parameters:
    - index: Where to insert it (-1 = end)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("create_scene", {"index": index})
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error creating scene: {str(e)}")
        return f"Error creating scene: {str(e)}"


@mcp.tool()
@telemetry_tool("delete_scene")
@trajectory_tool("delete_scene")
def delete_scene(ctx: Context, index: int, user_prompt: str = "") -> str:
    """
    Delete a scene and the clips in its row.

    Parameters:
    - index: Scene index
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("delete_scene", {"index": index})
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error deleting scene: {str(e)}")
        return f"Error deleting scene: {str(e)}"


@mcp.tool()
@telemetry_tool("undo")
@trajectory_tool("undo", modifying=False)
def undo(ctx: Context, user_prompt: str = "") -> str:
    """
    Undo the last change in Live (same as Cmd/Ctrl+Z).

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("undo")
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error undoing: {str(e)}")
        return f"Error undoing: {str(e)}"


@mcp.tool()
@telemetry_tool("redo")
@trajectory_tool("redo", modifying=False)
def redo(ctx: Context, user_prompt: str = "") -> str:
    """
    Redo the last undone change in Live.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("redo")
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error redoing: {str(e)}")
        return f"Error redoing: {str(e)}"


def _automation_target(device_index: int | None, parameter_index: int | None,
                       mixer_target: str = "", chain_path=None):
    """Turn a tool's target arguments into script params.

    Returns (params, capabilities_needed), or an error string. mixer_target
    is "volume", "pan", or "send A" / "send 0" (letter or index).
    """
    params: Dict[str, Any] = {}
    needed = []
    target = (mixer_target or "").strip().lower()
    if target:
        needed.append("automation_targets")
        if target in ("volume", "vol"):
            params["mixer_target"] = "volume"
        elif target in ("pan", "panning"):
            params["mixer_target"] = "panning"
        elif target.startswith("send"):
            key = target[4:].strip(" :_-")
            if not key:
                return "mixer_target 'send' needs a return letter or index, e.g. 'send A'"
            params["mixer_target"] = "send"
            params["send_index"] = _send_index(key)
        else:
            return f"Unknown mixer_target '{mixer_target}' — use volume, pan or 'send A'"
    elif device_index is None or parameter_index is None:
        return "Pass device_index and parameter_index, or mixer_target"
    else:
        params["device_index"] = device_index
        params["parameter_index"] = parameter_index
        if chain_path:
            needed.append("chain_path")
            params["chain_path"] = chain_path
    return params, needed


def _require_all(names) -> str | None:
    from .script_handshake import require_capability

    for name in names:
        missing = require_capability(name)
        if missing:
            return missing
    return None


def _automation_where(params: Dict[str, Any]) -> str:
    if params.get("mixer_target") == "send":
        return f"send {chr(ord('A') + params['send_index'])}"
    if params.get("mixer_target"):
        return params["mixer_target"]
    path = f" chain_path {params['chain_path']}" if params.get("chain_path") else ""
    return f"device {params['device_index']}{path} parameter {params['parameter_index']}"


@mcp.tool()
@telemetry_tool("create_clip_automation")
@trajectory_tool("create_clip_automation")
def create_clip_automation(
    ctx: Context,
    track_index: int,
    clip_index: int,
    points: List[Dict[str, Any]],
    device_index: int | None = None,
    parameter_index: int | None = None,
    mixer_target: str = "",
    chain_path: List[int] | None = None,
    clear: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Write a clip envelope in a Session clip, for a device or mixer parameter.

    The envelope loops with the clip, which suits filter sweeps, delay
    throws and slow movement across a long loop. Ramps between points are
    written as 1/16-note steps. Target a device parameter with
    device_index + parameter_index (chain_path for a device inside a rack),
    or the track's own fader, pan or send with mixer_target.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot
    - points: At least 2 points, each {"time": beats_from_clip_start} plus
      either "value" (raw, in the parameter's min..max) or "display"
      (Live's own string, e.g. "-12 dB", "800 Hz", "25L")
    - device_index / parameter_index: Device parameter to automate (see get_device_parameters)
    - mixer_target: "volume", "pan", or "send A" / "send 0" instead of a device parameter
    - chain_path: [chain_index, device_index, ...] pairs to reach a device inside a rack
    - clear: Remove this parameter's existing envelope in the clip first (default True)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        target = _automation_target(device_index, parameter_index, mixer_target, chain_path)
        if isinstance(target, str):
            return target
        params, needed = target
        if any(p.get("display") is not None for p in points) or not clear:
            needed.append("automation_targets")
        missing = _require_all(needed)
        if missing:
            return missing
        params.update({"track_index": track_index, "clip_index": clip_index, "points": points})
        if not clear:
            params["clear"] = False
        result = _send("create_automation", params)
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error creating automation: {str(e)}")
        return f"Error creating automation: {str(e)}"


@mcp.tool()
@telemetry_tool("create_arrangement_automation")
@trajectory_tool("create_arrangement_automation")
def create_arrangement_automation(
    ctx: Context,
    track_index: int,
    points: List[Dict[str, Any]],
    device_index: int | None = None,
    parameter_index: int | None = None,
    mixer_target: str = "",
    chain_path: List[int] | None = None,
    user_prompt: str = "",
) -> str:
    """
    Write automation across a span of the Arrangement, in song beats.

    Live's API gives no access to a track's own automation lane, so this
    writes the envelopes of the arrangement clips under the span (the
    Arrangement plays them as automation). Parts of the span with no clip,
    and clips Live refuses an envelope for, are reported. Return and master
    tracks have no clips, so they can't be automated this way. If nothing
    can be written, put the automation on a Session clip with
    create_clip_automation and duplicate_to_arrangement it instead.

    Parameters:
    - track_index: Regular track (not a return or the master)
    - points: At least 2 points, each {"time": song_beat} plus "value" (raw)
      or "display" (e.g. "-12 dB", "800 Hz")
    - device_index / parameter_index: Device parameter to automate
    - mixer_target: "volume", "pan", or "send A" / "send 0" instead of a device parameter
    - chain_path: [chain_index, device_index, ...] pairs to reach a device inside a rack
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        target = _automation_target(device_index, parameter_index, mixer_target, chain_path)
        if isinstance(target, str):
            return target
        params, needed = target
        missing = _require_all(needed)
        if missing:
            return missing
        params.update({"track_index": track_index, "points": points})
        result = _send("create_arrangement_automation", params)
        if isinstance(result, str):
            return result
        rows = [f"{result.get('parameter')} ({_automation_where(params)}) on track {track_index}:"]
        for c in result.get("clips", []):
            line = (f"  clip {c['clip_index']} {c['name']}: beats {c['from']:g}–{c['to']:g}, "
                    f"{c['inserted_steps']} steps")
            if c.get("error"):
                line += f" (stopped: {c['error']})"
            rows.append(line)
        for r in result.get("refused", []):
            rows.append(f"  clip {r['clip_index']} {r['name']}: refused — {r['reason']}")
        for lo, hi in result.get("uncovered", []):
            rows.append(f"  beats {lo:g}–{hi:g}: no clip, nothing written")
        return "\n".join(rows)
    except Exception as e:
        logger.error(f"Error creating arrangement automation: {str(e)}")
        return f"Error creating arrangement automation: {str(e)}"


@mcp.tool()
@telemetry_tool("get_automation")
@trajectory_tool("get_automation", modifying=False)
def get_automation(
    ctx: Context,
    track_index: int,
    device_index: int | None = None,
    parameter_index: int | None = None,
    mixer_target: str = "",
    chain_path: List[int] | None = None,
    clip_index: int | None = None,
    track_type: str = "track",
    start: float | None = None,
    end: float | None = None,
    step: float = 1.0,
    user_prompt: str = "",
) -> str:
    """
    Read a parameter's automation back as sampled [time, value, display] rows.

    With clip_index, samples that Session clip's envelope (clip beats).
    Without it, samples the envelopes of the track's arrangement clips
    (song beats). automation_state reports whether the parameter has track
    automation ("playing", or "overridden" after a manual change), which is
    readable on returns and master too even though their envelopes aren't.

    Parameters:
    - track_index: Track index within its track_type
    - device_index / parameter_index: Device parameter to read
    - mixer_target: "volume", "pan", or "send A" / "send 0" instead of a device parameter
    - chain_path: [chain_index, device_index, ...] pairs to reach a device inside a rack
    - clip_index: Session clip slot; omit to read the Arrangement
    - track_type: "track" (default), "return" or "master"
    - start / end: Span to sample, in beats (default: the whole clip)
    - step: Beats between samples (default 1; widened to keep at most 256 rows)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        target = _automation_target(device_index, parameter_index, mixer_target, chain_path)
        if isinstance(target, str):
            return target
        params, needed = target
        missing = _missing_features(track_type, extra=needed)
        if missing:
            return missing
        params.update({"track_index": track_index, "step": step})
        if track_type != "track":
            params["track_type"] = track_type
        for key, value in (("clip_index", clip_index), ("start", start), ("end", end)):
            if value is not None:
                params[key] = value
        result = _send("get_automation", params)
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error reading automation: {str(e)}")
        return f"Error reading automation: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_loop")
@trajectory_tool("set_clip_loop")
def set_clip_loop(
    ctx: Context,
    track_index: int,
    clip_index: int,
    loop_start: float | None = None,
    loop_end: float | None = None,
    length: float | None = None,
    looping: bool | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set a Session clip's loop start, loop end or length, and looping on/off.

    Only the arguments you pass change. length sets loop_end to
    loop_start + length (pass loop_end or length, not both). For an
    unlooped clip these move the clip's start and end. Times are in beats
    (seconds for unwarped audio). Lengthening a MIDI clip's loop doesn't
    copy notes into the new space.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot
    - loop_start: Loop start in beats
    - loop_end: Loop end in beats
    - length: Loop length in beats, measured from loop_start
    - looping: Turn looping on or off
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        changes = {k: v for k, v in (
            ("loop_start", loop_start), ("loop_end", loop_end),
            ("length", length), ("looping", looping),
        ) if v is not None}
        if not changes:
            return "Nothing to change: pass loop_start, loop_end, length or looping"
        result = _send("set_clip_loop", {
            "track_index": track_index, "clip_index": clip_index, **changes,
        })
        if isinstance(result, str):
            return result
        state = "looped" if result.get("looping") else "not looped"
        return (f"Clip {track_index}/{clip_index}: loop {result.get('loop_start'):g}–"
                f"{result.get('loop_end'):g} ({result.get('length'):g} beats, {state})")
    except Exception as e:
        logger.error(f"Error setting clip loop: {str(e)}")
        return f"Error setting clip loop: {str(e)}"


@mcp.tool()
@telemetry_tool("update_notes")
@trajectory_tool("update_notes")
def update_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    notes: List[Dict[str, Any]],
    user_prompt: str = "",
) -> str:
    """
    Edit existing notes in place by note_id, leaving every other note alone.

    Read note_ids with get_clip_notes. Each note needs note_id plus the
    fields to change: pitch, start_time, duration, velocity, mute,
    probability, velocity_deviation.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot
    - notes: Notes to modify, each with its note_id
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("update_notes", {
            "track_index": track_index, "clip_index": clip_index, "notes": notes,
        })
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error updating notes: {str(e)}")
        return f"Error updating notes: {str(e)}"


@mcp.tool()
@telemetry_tool("remove_notes")
@trajectory_tool("remove_notes")
def remove_notes(
    ctx: Context,
    track_index: int,
    clip_index: int,
    from_time: float = 0.0,
    time_span: float = 128.0,
    from_pitch: int = 0,
    pitch_span: int = 128,
    user_prompt: str = "",
) -> str:
    """
    Remove the notes inside a time and pitch window, keeping the rest.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot
    - from_time: Window start in beats
    - time_span: Window length in beats
    - from_pitch: Lowest MIDI pitch to remove
    - pitch_span: Number of pitches upward from from_pitch
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("remove_notes_range", {
            "track_index": track_index, "clip_index": clip_index,
            "from_time": from_time, "time_span": time_span,
            "from_pitch": from_pitch, "pitch_span": pitch_span,
        })
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error removing notes: {str(e)}")
        return f"Error removing notes: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_launch_settings")
@trajectory_tool("set_clip_launch_settings")
def set_clip_launch_settings(
    ctx: Context,
    track_index: int,
    clip_index: int,
    quantization: int | None = None,
    legato: bool | None = None,
    follow_action_a: int | None = None,
    follow_action_b: int | None = None,
    follow_action_time: float | None = None,
    user_prompt: str = "",
) -> str:
    """
    Set a Session clip's launch quantization, legato and follow actions.

    Follow actions let clip variations alternate on their own, which is how
    a long set evolves without new clips.

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot
    - quantization: Live's launch quantization enum (0 = global)
    - legato: Legato mode on/off
    - follow_action_a / follow_action_b: Live follow action enums
    - follow_action_time: Beats before the follow action fires
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        settings = {k: v for k, v in (
            ("quantization", quantization), ("legato", legato),
            ("follow_action_a", follow_action_a), ("follow_action_b", follow_action_b),
            ("follow_action_time", follow_action_time),
        ) if v is not None}
        result = _send("set_clip_launch_settings", {
            "track_index": track_index, "clip_index": clip_index, **settings,
        })
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error setting clip launch settings: {str(e)}")
        return f"Error setting clip launch settings: {str(e)}"


@mcp.tool()
@telemetry_tool("get_groove_pool")
@trajectory_tool("get_groove_pool", modifying=False)
def get_groove_pool(ctx: Context, user_prompt: str = "") -> str:
    """
    List the grooves in the project's groove pool.

    Parameters:
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("get_groove_pool")
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error reading groove pool: {str(e)}")
        return f"Error reading groove pool: {str(e)}"


@mcp.tool()
@telemetry_tool("set_clip_groove")
@trajectory_tool("set_clip_groove")
def set_clip_groove(
    ctx: Context, track_index: int, clip_index: int, groove_index: int, user_prompt: str = ""
) -> str:
    """
    Apply a groove from the groove pool to a Session clip (swing, shuffle).

    Parameters:
    - track_index: Track that owns the clip
    - clip_index: Session clip slot
    - groove_index: Index from get_groove_pool
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("set_clip_groove", {
            "track_index": track_index, "clip_index": clip_index, "groove_index": groove_index,
        })
        return result if isinstance(result, str) else _compact_json(result)
    except Exception as e:
        logger.error(f"Error setting clip groove: {str(e)}")
        return f"Error setting clip groove: {str(e)}"


@mcp.tool()
@telemetry_tool("search_browser")
@trajectory_tool("search_browser", modifying=False)
def search_browser(
    ctx: Context,
    query: str,
    category: str = "all",
    limit: int = 20,
    loadable_only: bool = True,
    refresh: bool = False,
    user_prompt: str = "",
) -> str:
    """
    Find instruments, effects, presets and kits in Live's browser by name.

    Every word in query must appear in the item's name or folder path.
    Returns name, path and URI; pass the URI to load_instrument_or_effect.
    The first search of each browser section takes a few seconds while it is
    indexed; later searches are instant.

    Parameters:
    - query: Words to match, e.g. "dub chord", "909 kit", "echo"
    - category: "all" (default: instruments, sounds, drums, audio_effects,
      midi_effects, max_for_live, plugins, user_library) or one browser
      section, including "packs" or "samples"
    - limit: Maximum results (default 20, max 200)
    - loadable_only: Skip folders (default True)
    - refresh: Rebuild the index (after installing packs or saving presets)
    - user_prompt: The original user prompt that led to this tool call (for telemetry)
    """
    try:
        result = _send("search_browser", {
            "query": query, "category": category, "limit": limit,
            "loadable_only": loadable_only, "refresh": refresh,
        })
        if isinstance(result, str):
            return result
        rows = [f"{r['name']} — {r['path']} — {r['uri']}" for r in result.get("results", [])]
        header = (f"{result.get('total_matches', 0)} matches for '{query}'"
                  f" (showing {len(rows)})")
        if result.get("truncated_roots"):
            header += f"; index truncated in {', '.join(result['truncated_roots'])}"
        return "\n".join([header] + rows)
    except Exception as e:
        logger.error(f"Error searching browser: {str(e)}")
        return f"Error searching browser: {str(e)}"


# Main execution
def main():
    """Run the MCP server"""
    mcp.run()

if __name__ == "__main__":
    main()