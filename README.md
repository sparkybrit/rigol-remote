# rigol-remote

Read-only access to a Rigol DS1054Z oscilloscope on USB, for Claude Code sessions (MCP server) and
for people and scripts (the `rigol` CLI). Linux only: it drives the kernel `usbtmc` driver directly,
working around the scope's USB firmware bugs.

## Setup

```bash
uv sync
# once, as root: let the plugdev group open the scope
echo 'KERNEL=="usbtmc[0-9]*", ATTRS{idVendor}=="1ab1", GROUP="plugdev", MODE="0660"' | sudo tee /etc/udev/rules.d/99-rigol.rules
sudo udevadm control --reload && sudo udevadm trigger --subsystem-match=usbmisc
# make the tools available to every Claude Code session on this machine
claude mcp add --scope user rigol -- "$PWD/.venv/bin/rigol-mcp"
```

## CLI

```bash
uv run rigol idn
uv run rigol screenshot                  # prints the PNG's path
uv run rigol settings                    # channels, timebase, trigger, acquisition (JSON)
uv run rigol measure 1 VPP FREQ VRMS     # null = the scope couldn't measure it
uv run rigol waveform 1                  # on-screen trace → CSV, plus summary stats
uv run rigol query ':TRIGger:STATus?'    # any single read-only SCPI query
```

Captures go to `~/.local/share/rigol-remote/captures/` (override with `RIGOL_CAPTURE_DIR`). The scope
is found automatically; set `RIGOL_ADDR=/dev/usbtmcN` to pick one explicitly.

## MCP tools

`identify`, `screenshot` (returns the image), `get_settings`, `measure`, `get_waveform`, `scpi_query`.
None change acquisition settings; `measure` does add its items to the scope's on-screen
measurement bar.

## Tests

```bash
uv run pytest               # unit tests, no scope needed
uv run pytest -m hardware   # read-only tests against the real scope
```
