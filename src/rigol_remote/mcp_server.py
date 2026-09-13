"""MCP server (stdio) giving Claude Code sessions access to the scope.

Each tool call opens the scope, takes its exclusive lock, does its work and closes it again, so
any number of sessions can have this server running at once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TypeVar

import anyio.to_thread
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from rigol_remote.captures import backup_setup, read_setup, save_capture
from rigol_remote.connection import connect
from rigol_remote.scope import MEASUREMENT_ITEMS, DS1054Z, ScopeError

INSTRUCTIONS = """\
A Rigol DS1054Z oscilloscope (4 channels, 50 MHz) is attached to this host over USB.

Reading: identify, screenshot (the quickest way to see what the scope shows), get_settings, measure,
get_waveform, scpi_query and save_setup. None of them change acquisition settings. The one exception
is visible: measure adds the items it reads to the measurement bar at the bottom of the screen.

Changing:
- configure_channel, configure_timebase, configure_trigger and configure_acquisition change only
  the parameters you pass. For each one they return requested, before and after values. The scope
  snaps to the values it supports, so report "after"; call again with the "before" values to undo.
- run_control runs, stops, single-shots or forces a trigger. run or single overwrites a stopped
  capture.
- reset, autoscale, restore_setup and scpi_write replace much or all of the setup. Each saves a
  backup first and returns its path; restore_setup(path) undoes them.

Rules:
- The user may have set the scope up by hand. Change only what they asked for. Ask before anything
  broader: reset, autoscale, clearing measurements, or touching channels they didn't mention.
- Never report a reading you didn't just get from these tools, and never estimate one.
- A measurement value of null means the scope could not measure it (shown as **** on its screen).
- Voltages off by exactly 10x mean the channel's probe_ratio doesn't match the probe's 1x/10x
  switch. Ask the user.
- Probing and cabling are for the user to do. Ask them.
- Don't open /dev/usbtmc* yourself: the scope's USB is quirky and easily wedged. Use these tools,
  or the `rigol` CLI in ~/Projects/rigol-remote, which shares the same locking.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Adds display elements or changes settings, but reports before-values so it can be undone.
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
# Can lose something: a stopped capture, the measurement bar, or the whole setup (backed up first).
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

mcp = MCPServer("rigol", instructions=INSTRUCTIONS)

T = TypeVar("T")


async def _with_scope(action: Callable[[DS1054Z], T]) -> T:
    """Run blocking scope I/O in a worker thread, holding the scope's lock for the duration."""

    def run() -> T:
        with connect() as scope:
            return action(scope)

    try:
        return await anyio.to_thread.run_sync(run)
    except (ScopeError, OSError, ValueError) as e:
        raise ToolError(str(e)) from e  # other exceptions reach the model only as "Error executing tool"


async def _with_backup(action: Callable[[DS1054Z], object]) -> dict:
    """Save the whole setup, then run action; the result says where the backup is."""

    def run(scope: DS1054Z) -> dict:
        backup = backup_setup(scope)
        return {"backup": str(backup), "result": action(scope)}

    return await _with_scope(run)


# --- reading ---

@mcp.tool(annotations=READ_ONLY)
async def identify() -> dict:
    """Manufacturer, model, serial number and firmware of the attached scope."""
    return await _with_scope(lambda scope: scope.identify())


@mcp.tool(annotations=READ_ONLY, structured_output=False)
async def screenshot() -> list:
    """Capture the scope's screen (800x480 PNG). Also saved to disk; the path is returned."""
    png = await _with_scope(lambda scope: scope.screenshot_png())
    path = save_capture("screen", "png", png)
    return [Image(data=png, format="png"), f"Saved to {path}"]


@mcp.tool(annotations=READ_ONLY)
async def get_settings() -> dict:
    """Channel (on/off, V/div, offset, coupling, probe ratio), timebase, trigger and acquisition settings.
    Values are SI: volts, seconds, samples per second."""
    return await _with_scope(lambda scope: scope.settings())


@mcp.tool(
    # Not read-only: the scope adds each item it measures to its on-screen measurement bar.
    annotations=CHANGES,
    description=(
        "The scope's automatic measurements on one channel. channel: 1-4 or CHAN1-CHAN4. items: any of "
        f"{', '.join(MEASUREMENT_ITEMS)} (long forms like FREQuency also accepted). "
        "A value of null means the scope could not measure it. Side effect: each item is added to the "
        "measurement bar on the scope's screen (it shows at most 5, so older ones scroll off)."
    ),
)
async def measure(channel: str, items: list[str]) -> dict:
    return await _with_scope(lambda scope: scope.measure(channel, items))


@mcp.tool(annotations=READ_ONLY)
async def get_waveform(channel: str) -> dict:
    """Read one channel's on-screen trace (1200 points), save it as CSV (time_s,volts) and return
    summary statistics plus the CSV path. `clipped: true` means the trace ran off screen, so its
    extremes are unreliable. channel: 1-4 or CHAN1-CHAN4."""
    wave = await _with_scope(lambda scope: scope.waveform(channel))
    path = save_capture(wave.channel.lower(), "csv", wave.csv().encode())
    return wave.summary() | {"csv_path": str(path)}


@mcp.tool(annotations=READ_ONLY)
async def scpi_query(command: str) -> str:
    """Send one read-only SCPI query (its header must end in '?', e.g. ':TRIGger:STATus?' or
    ':CHANnel1:SCALe?') and return the reply. Commands that change settings are refused."""
    return await _with_scope(lambda scope: scope.query(command))


@mcp.tool(annotations=READ_ONLY)
async def save_setup() -> dict:
    """Save the scope's whole setup to a file and return its path; restore_setup(path) brings it back."""
    return {"path": str(await _with_scope(backup_setup))}


# --- changing settings ---

@mcp.tool(annotations=CHANGES)
async def configure_channel(
    channel: str,
    display: bool | None = None,
    probe_ratio: float | None = None,
    scale_v_per_div: float | None = None,
    offset_v: float | None = None,
    coupling: Literal["AC", "DC", "GND"] | None = None,
    bw_limit: Literal["20M", "OFF"] | None = None,
    invert: bool | None = None,
) -> dict:
    """Change one channel's settings; only the parameters given are changed. channel: 1-4 or
    CHAN1-CHAN4. probe_ratio must match the probe's attenuation switch (1, 10, ...); it rescales
    the V/div. Returns requested/before/after for each setting."""
    return await _with_scope(lambda scope: scope.configure_channel(
        channel, display=display, probe_ratio=probe_ratio, scale_v_per_div=scale_v_per_div,
        offset_v=offset_v, coupling=coupling, bw_limit=bw_limit, invert=invert))


@mcp.tool(annotations=CHANGES)
async def configure_timebase(
    scale_s_per_div: float | None = None,
    offset_s: float | None = None,
    mode: Literal["MAIN", "XY", "ROLL"] | None = None,
) -> dict:
    """Change the horizontal settings; only the parameters given are changed. offset_s moves the
    trigger point. Returns requested/before/after for each setting."""
    return await _with_scope(lambda scope: scope.configure_timebase(
        scale_s_per_div=scale_s_per_div, offset_s=offset_s, mode=mode))


@mcp.tool(annotations=CHANGES)
async def configure_trigger(
    sweep: Literal["AUTO", "NORMAL", "SINGLE"] | None = None,
    source: str | None = None,
    slope: Literal["rising", "falling", "either"] | None = None,
    level_v: float | None = None,
    coupling: Literal["AC", "DC", "LFREJECT", "HFREJECT"] | None = None,
    holdoff_s: float | None = None,
) -> dict:
    """Change the edge-trigger settings; only the parameters given are changed. source: 1-4,
    CHAN1-CHAN4 or AC (mains). Giving source, slope or level switches the trigger to EDGE mode.
    sweep AUTO free-runs without a trigger, NORMAL waits for one. Returns requested/before/after."""
    return await _with_scope(lambda scope: scope.configure_trigger(
        sweep=sweep, source=source, slope=slope, level_v=level_v, coupling=coupling, holdoff_s=holdoff_s))


@mcp.tool(annotations=CHANGES)
async def configure_acquisition(
    acquisition_type: Literal["NORMAL", "AVERAGES", "PEAK", "HRESOLUTION"] | None = None,
    averages: int | None = None,
    memory_depth: int | Literal["AUTO"] | None = None,
) -> dict:
    """Change how the scope acquires; only the parameters given are changed. averages (a power of
    two, 2-1024) applies in AVERAGES mode. Valid memory depths depend on how many channels are on
    (1 channel: 12000 to 24000000; 2: 6000 to 12000000; 3-4: 3000 to 6000000). Returns
    requested/before/after."""
    return await _with_scope(lambda scope: scope.configure_acquisition(
        acquisition_type=acquisition_type, averages=averages, memory_depth=memory_depth))


@mcp.tool(annotations=DESTRUCTIVE)
async def run_control(action: Literal["run", "stop", "single", "force_trigger"], wait_s: float = 0.0) -> dict:
    """Start (run) or freeze (stop) acquisition, arm one acquisition (single), or force a trigger.
    run and single overwrite a stopped capture. For single, wait_s waits up to that long for it to
    complete. Returns the trigger status (RUN, WAIT, TD, AUTO or STOP) and sweep mode."""

    def act(scope: DS1054Z) -> dict:
        if action == "single":
            return scope.single(wait_s)
        return {"run": scope.run, "stop": scope.stop, "force_trigger": scope.force_trigger}[action]()

    return await _with_scope(act)


@mcp.tool(annotations=DESTRUCTIVE)
async def clear_measurements(item: int | None = None) -> dict:
    """Remove one item (1-5, left to right) or, by default, all items from the measurement bar at
    the bottom of the scope's screen. That includes items the user added."""
    return await _with_scope(lambda scope: scope.clear_measurements(item))


@mcp.tool(annotations=DESTRUCTIVE)
async def restore_setup(path: str) -> dict:
    """Restore a whole setup saved by save_setup (or a backup from another tool). The current setup
    is backed up first. Takes about 6 s. Returns the backup path and the resulting settings."""

    def act(scope: DS1054Z) -> dict:
        data = read_setup(path)  # before the backup, so a bad path fails cleanly
        backup = backup_setup(scope)
        return {"backup": str(backup), "result": scope.restore_setup(data)}

    return await _with_scope(act)


@mcp.tool(annotations=DESTRUCTIVE)
async def autoscale() -> dict:
    """Let the scope choose scales, timebase and trigger for the connected signals, like the AUTO
    key. It replaces the user's setup, so it is backed up first. Returns the backup path and the new
    settings."""
    return await _with_backup(lambda scope: scope.autoscale())


@mcp.tool(annotations=DESTRUCTIVE)
async def reset() -> dict:
    """Reset the scope to factory defaults (*RST). The setup is backed up first. Returns the backup
    path and the new settings."""
    return await _with_backup(lambda scope: scope.reset())


@mcp.tool(annotations=DESTRUCTIVE)
async def scpi_write(command: str) -> dict:
    """Send one raw SCPI command that expects no reply, for anything the other tools don't cover
    (e.g. ':CHANnel1:VERNier ON'). Use scpi_query for queries. The setup is backed up first. Fails
    if the scope reports an error."""
    return await _with_backup(lambda scope: scope.write(command))


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
