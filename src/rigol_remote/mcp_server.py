"""MCP server (stdio) giving Claude Code sessions read-only access to the scope.

Each tool call opens the scope, takes its exclusive lock, does its work and closes it again, so
any number of sessions can have this server running at once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import anyio.to_thread
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from rigol_remote.captures import save_capture
from rigol_remote.connection import connect
from rigol_remote.scope import MEASUREMENT_ITEMS, DS1054Z, ScopeError

INSTRUCTIONS = """\
A Rigol DS1054Z oscilloscope (4 channels, 50 MHz) is attached to this host over USB. These tools
only read from it and never change its acquisition settings. The one visible side effect: `measure`
adds the items it reads to the measurement bar at the bottom of the scope's screen.

- Never report a reading you didn't just get from these tools, and never estimate one.
- A measurement value of null means the scope could not measure it (shown as **** on its screen);
  say so rather than reporting a number.
- Voltages off by exactly 10x mean the channel's probe_ratio (see get_settings) doesn't match the
  physical probe's 1x/10x switch; ask the user.
- `screenshot` is the quickest way to see what the scope is showing.
- Probing, cabling and front-panel changes are for the user to do; ask them.
- Don't open /dev/usbtmc* yourself: the scope's USB is quirky and easily wedged. Use these tools
  (or the `rigol` CLI in ~/Projects/rigol-remote, which shares the same locking).
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

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
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False),
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


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
