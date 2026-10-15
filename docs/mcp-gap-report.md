# ableton-mcp: gaps found while producing a track

These are field notes from one production session: a minimal dub techno groove at 125 BPM, built from 8 tracks with drum racks, Drift, Wavetable, instrument racks and native effect chains. I cross-checked them against a read-only audit of `~/Development/ableton-mcp` on `main` at 36c7fc9.

- **RS** = `AbletonMCP_Remote_Script/__init__.py` (Live side, v1.18.0)
- **server** = `MCP_Server/server.py` (the MCP tool layer)

**Headline:** the Remote Script advertises 49 commands, and 31 of them have no MCP tool. The rest of the Live-side work is mostly done; the tool layer is missing.

## Status (v1.19.0, branch docs/mcp-gap-report)

Unit-tested against a fake Live song; **not yet run in Live**. Install the script with `ableton-mcp-install-script` and restart Live fully before relying on it.

| # | Gap | Status |
|---|---|---|
| 0 | Stale uvx server / handshake | Handshake fixed (newer script = compatible). Pointing the config at the local checkout is still a manual step. |
| 1 | Mixer | `get_mixer`, `set_mixer` (volume_db, pan, sends_db, mute, solo, arm) |
| 2 | Return / master | `track_type` on device, mixer, routing and load tools |
| 3 | Display values | `display` + `value_items` on parameters; `display_value` on `set_device_parameter` |
| 4 | Routing / sidechain | `get_track_routing` / `set_track_routing` with channels. **Device sidechain inputs are still not reachable.** |
| 5 | Rack devices | `chain_path` on get / set / delete / move |
| 6 | Drum pad notes | `drum_pads` on drum racks |
| 7 | Payload size | Chains summarized by default, `name_filter`, compact rows, no indent |
| 8 | Browser | `search_browser`, and paging on `get_browser_items_at_path` |
| 9 | Automation | `create_clip_automation` (Session clips, top-level devices). **Still missing: arrangement automation, mixer-parameter targets, read-back.** |
| 10 | Device management | `delete_device`, `move_device`, load `position`; the load result reports the device index |
| 11 | Scenes / clips | `fire_scene`, `create_scene`, `delete_scene`, `set_clip_launch_settings`, grooves. **Still missing: clip loop/length, arrangement trim/split.** |
| 12 | Note editing | `update_notes`, `remove_notes` |
| 13 | Metering | `get_meters` (peaks/averages on Live's meter scale). **Still missing: render, LUFS.** |
| 14 | Indices | Track creation and load report indices |
| 15 | Undo | `undo` / `redo` |
| 16 | Broken commands | Dead browser dispatch removed |

---

## P0: fix first

### 0. The running MCP server isn't this repo
- **What happens:** `get_remote_script_info` reports `expected_version: 1.7.1`, but the checkout has `EXPECTED_REMOTE_SCRIPT_VERSION = "1.18.0"` (`MCP_Server/remote_script_install.py:23`). The bump landed in today's merge 36c7fc9; before that it was 1.7.1, set in 7e90cd6.
- **Cause:** `~/.claude.json` starts the `ableton` server as `uvx ableton-mcp`. That resolves the published package (pyproject 1.4.5), not the local checkout. Anything added to `server.py` won't reach Claude until this changes. It also trips the `require_capability` gates, because the handshake reports "not up to date".
- **Confirmed:** both uv cache copies (`~/.cache/uv/archive-v0/niyBB5tjxX0WdtoM/...` and `.../sk5akTbAUgFRmajB/...`) have `"1.7.1"` at `remote_script_install.py:23`. The tool surface Claude sees may be older than the repo's `server.py`.
- **Fix:**
  - Point the config at the repo, e.g. `uv run --directory ~/Development/ableton-mcp ableton-mcp`. Alternatively, publish and run `uvx --refresh ableton-mcp` (or `uv cache clean ableton-mcp`), then restart the server.
  - Make the handshake treat a *newer* script as compatible.

### 1. No mixer control: volume, pan, sends, mute, solo
- **Tried:** balancing kick against bass, tucking the pad back, sending the stab to the A-Reverb and B-Delay returns.
- **What's there:** RS has `set_mixer_value` (RS:3401; volume, panning or a send, as raw 0..1) and `set_track_state` (RS:1185; mute, solo, arm). Neither has a tool. Both index `song.tracks` only.
- **Workaround:** I used EQ Eight `Output` (±12 dB) or the instrument `Volume` as a fake fader on every track. The real faders sit untouched and the returns are unused.
- **Want:**
  - `set_track_mixer(target, volume_db?, pan?, sends?: {i: level}, mute?, solo?)`, with volume in dB. Convert through `param.str_for_value` bisection, or a known fader curve.
  - A matching `get_track_mixer`.
  - Both should work on tracks, returns and master.

### 2. Return and master tracks can't be reached by device tools
- **What's there:**
  - The script's `load_instrument_or_effect` already supports `target = track | return | master` (`_resolve_track_for_loading`, RS:2365-2375).
  - The MCP tool (server:892-906) bypasses that command, sends `load_browser_item` with only `track_index`, and never exposes `target`.
  - `set_device_parameter` (RS:3374-3384), `delete_device` (RS:2143) and `create_automation` (RS:2180) all index `song.tracks[i]` only.
- **Effect:** I couldn't tune the Delay and Reverb already on the returns, or put a glue compressor or limiter on the master.
- **Want:** one shared target resolver, `{type: "track"|"return"|"master", index}`, used by every device, mixer and automation command.

### 3. Parameter display values never come through
- **Effect:** values came back as raw normalized floats with no units. To place an EQ cut at 180 Hz I reverse-engineered EQ Eight's mapping (≈ `10·2200^x` Hz). I couldn't map Utility gain (−1…1) at all.
- **Code:** RS:3046 only emits `value_string` when `hasattr(param, "value_string")` is true. Live's `DeviceParameter` exposes `str_for_value(v)` and `str(param)`, not a `value_string` attribute, so the field is probably always skipped.
- **Want:**
  - Emit `display = str(param)`, plus `value_items` for quantized parameters.
  - Add a `set_device_parameter(..., display="180 Hz")` variant that bisects `str_for_value` to find the raw value.

### 4. No routing or sidechain
- **Effect:** I couldn't sidechain the bass and pad to the kick, which is core to the genre.
- **What's there:** `get_track_routing` and `set_track_routing` (RS:1091 and 1119). They have no tools, and they only set the routing *type*, matched by display name, not the routing *channel*. Nothing touches a device's sidechain input.
- **Want:**
  - Expose both commands and add channel selection.
  - Add device sidechain routing for Compressor, Gate and Auto Filter through the Live 12 routing properties on those devices.

---

## P1: blocks sound design and arrangement

### 5. Devices inside racks are read-only
- **What's there:** `get_device_parameters` recurses into chains up to depth 4 (`_serialize_chains`, RS:3072-3119), but nothing can *write* below the top level. The unadvertised `inspect_rack` (RS:2522) and `map_rack_magnitude` (RS:2560) commands hint at the start of this.
- **Effect:** I couldn't:
  - Tune the 909 kick inside the kit.
  - Adjust the Wavetable or Echo inside "Chord Dub Keys".
  - Set per-pad volume or pan.
- **Want:**
  - A device path `[device, chain, device, …]`, or `drum_pad: note`, on get, set, load and delete.
  - Chain mixer access: volume, pan, mute and solo per chain.

### 6. Drum Rack pad → note mapping is missing
- **Code:** `_serialize_chains` (RS:3096-3100) emits only `chain.out_note`, which came back as 60 for every pad. Nothing reads `chain.in_note` or `device.drum_pads`; `can_have_drum_pads` is only used for type labels (RS:2631).
- **Effect:** I had to assume 36 + chain index and ask the user to check by ear.
- **Want:** a `drum_pads: [{note, name, chain_index}]` list for any rack where `can_have_drum_pads` is true. List only the filled pads.

### 7. `get_device_parameters` payloads are huge
- **Observed:** 294 KB for Percussion Core Kit and 504 KB for 909 Core Kit, both over the tool-result limit. A plain Wavetable is about 15 KB.
- **Cause:**
  - Every parameter is a verbose object (RS:3018-3070).
  - The serializer recurses through every chain (`_MAX_CHAIN_DEPTH = 4`, RS:3016).
  - The server then pretty-prints with `json.dumps(indent=2)` (server:536).
  - `get_session_snapshot` reuses the same serializer for every track (RS:3320, 3223, 3245).
- **Want:**
  - `depth` (default 0, top level only).
  - `filter` (regex on parameter names).
  - Compact rows: `[i, name, value, min, max, display]`.
  - No `indent`.

### 8. Browser: no search, no pagination
- **Observed:** `sounds/Bass` returned about 60 KB and `sounds/Pad` about 52 KB. I dumped them to disk and grepped.
- **Code:**
  - `get_browser_items_at_path` (RS:3710-3721) returns every child.
  - On a path miss, the error includes the full `available_children` list (RS:3706).
  - The only lookups are by exact URI (RS:2436-2498) or by walking a path.
- **Want:**
  - `search_browser(query, category?, limit=20)` returning name and URI, backed by a cached index.
  - `offset`/`limit` on path listings.

### 9. Automation isn't exposed
- **What's there:** `create_automation` (RS:2153). It isn't exposed, and it has limits:
  - Session clips only.
  - Top-level devices only.
  - Ramps approximated with 1/16-note steps.
  - No mixer parameters and no read-back.
- **Effect:** filter sweeps, echo-feedback throws and reverb swells are the core of dub techno movement. I faked them with Auto Filter LFOs.
- **Want:**
  - Expose the command.
  - Allow mixer parameters (send levels!) as targets.
  - Add arrangement automation.
  - Add envelope read-back.

### 10. Device management
- **What's there:** `delete_device` (RS:2137) has no tool. Loading calls `browser.load_item` on the selected track (RS:2392-2395), so Live decides where the device lands.
- **Bug:** the tool reads `new_devices` and `devices_after` (server:910-915), but the script never returns those keys (RS:2397-2402). The success message always lists no devices.
- **Want:**
  - Expose `delete_device`.
  - Add `move_device` (`song.move_device`).
  - Add a `position` argument on load: select the device before it, or move it after loading.
  - Return `device_index` from load.

### 11. Scenes, clips and arrangement
- **Script commands with no tool:**
  - `create_scene` / `delete_scene` / `fire_scene` (RS:922-946)
  - `set_clip_launch_settings` (RS:1257)
  - `set_color` (RS:1226)
  - `get_groove_pool` / `set_clip_groove` (RS:958, 968). A shared groove would be ideal for swinging the hats.
- **Effect:** I fired six clips one at a time.
- **Want:**
  - Expose the commands above.
  - Add clip `loop_start` / `loop_end` / `length`.
  - Add arrangement clip trim and split for building intro, breakdown and outro.

### 12. Note editing is append-only
- **What's there:** `update_notes` (RS:1294, note IDs via `apply_note_modifications`), `remove_notes_range` (RS:1379) and `select_notes` (RS:1032). None of them has a tool.
- **Effect:** every edit was clear-and-rewrite, which meant re-sending 300+ notes per clip.
- **Want:**
  - Expose these three commands.
  - Have `get_clip_notes` return `note_id`, `probability` and `velocity_deviation`.

---

## P2: quality of life

### 13. No ears: metering and render
- **What's there:** `investigate_render_capability` (RS:990) only lists `dir()` names. The repo has no `output_meter_*`, spectrum or LUFS code.
- **Want, cheapest first:**
  1. `get_meters(targets, seconds)`: sample `output_meter_left` / `output_meter_right` on tracks and master and return the peak and average.
  2. Resample to an audio track or freeze-and-flatten a loop, then analyze the WAV server-side (LUFS, spectrum balance, kick/bass overlap).

### 14. Indices in responses
- `create_midi_track` returns a name ("8-MIDI") but not the index.
- Load doesn't return a device index.
- **Want:** every create or load call returns `{track_index, device_index, clip_index}`.

### 15. Undo
- `undo` and `redo` (RS:1163, 1174) exist but have no tools. These are cheap to expose and make experimenting safe.

### 16. Broken or dead code
- `get_browser_categories` and `get_browser_items` dispatch to `_get_browser_categories` and `_get_browser_items` (RS:615, 618), but neither method is defined. Both always raise AttributeError.
- `MCP_Server/bundled_ableton_remote_script/AbletonMCP_init.py` duplicates the RS file. It has the same length and version lines, but it hasn't been diffed in full. Add a CI check that keeps the two in sync.

---

## Suggested next release, in order

1. **Run the local server (#0).** Without this, nothing else ships to Claude.
2. **Expose the script commands that already work:**
   - `set_mixer_value` and `set_track_state`
   - `delete_device`
   - scenes
   - `update_notes`, `remove_notes_range`
   - `undo`, `redo`
   - `get_track_routing`, `set_track_routing`
   - `create_automation`
   - `set_clip_launch_settings`, groove commands

   This is mostly thin `@mcp.tool()` wrappers in `server.py`.
3. **Add a shared target resolver** for tracks, returns and master. Pass `target` through the load tool.
4. **Parameter display strings** via `str(param)`, plus set-by-display.
5. **`get_device_parameters` size controls:** depth, filter, compact rows, no indent.
6. **Drum pad notes**, then device paths into racks.
7. **`search_browser`** with pagination.
8. **Metering** with `get_meters`.

---

## Appendix: MCP tools today → script command

These are the tools as defined in `server.py`:

- get_session_info, get_remote_script_info (→ get_script_info), get_track_info, get_clip_notes, get_device_parameters, set_device_parameter, get_session_snapshot
- create_midi_track, create_audio_track, set_track_name
- create_clip, create_audio_clip, add_notes_to_clip, clear_notes_from_clip, set_clip_name, set_arrangement_clip_name
- set_tempo, start_playback, stop_playback, fire_clip, stop_clip, delete_clip
- load_instrument_or_effect (→ load_browser_item), load_drum_kit, get_browser_tree, get_browser_items_at_path
- switch_to_arrangement_view, set_arrangement_time (→ set_current_song_time), get_arrangement_clips, duplicate_to_arrangement, create_locator
- Dataset tools (local only): set_dataset_consent, submit_intent, rate_last_action, prefer_candidate, reject_last_action, record_audition
