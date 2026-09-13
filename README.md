# rigol-remote

Control a Rigol DS1054Z oscilloscope on USB, from Claude Code sessions (MCP server) and from people
and scripts (the `rigol` CLI). Linux only: it drives the kernel `usbtmc` driver directly, working
around the scope's USB firmware bugs.

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

Numbers take SI suffixes (`500m`, `20ns`, `2.5V`). Changes print each setting's requested, before
and after values. The scope snaps to the steps it supports, so "after" is what you got.

```bash
# reading
uv run rigol idn
uv run rigol screenshot                  # prints the PNG's path
uv run rigol settings                    # channels, timebase, trigger, acquisition (JSON)
uv run rigol measure 1 VPP FREQ VRMS     # null = the scope couldn't measure it
uv run rigol waveform 1                  # on-screen trace → CSV, plus summary stats
uv run rigol query ':TRIGger:STATus?'    # any single read-only SCPI query
uv run rigol save-setup                  # whole setup → file

# changing (only the options given)
uv run rigol channel 2 --on --scale 500m --coupling AC --probe 10x
uv run rigol timebase --scale 20ns --offset 0
uv run rigol trigger --source 1 --slope rising --level 1.5 --sweep normal
uv run rigol acquisition --type averages --averages 16 --memory-depth 12k
uv run rigol run | stop | force
uv run rigol single --timeout 5          # arm, and wait up to 5 s for the capture

# whole-setup changes: each saves a backup first and prints its path
uv run rigol restore-setup FILE
uv run rigol autoscale --yes
uv run rigol reset --yes
uv run rigol write ':CHANnel1:VERNier ON'
uv run rigol clear-measurements [--item N]
```

Captures and backups go to `~/.local/share/rigol-remote/captures/` (override with
`RIGOL_CAPTURE_DIR`). The scope is found automatically; set `RIGOL_ADDR=/dev/usbtmcN` to pick one
explicitly. Concurrent users (sessions, scripts) take turns via a lock on the device.

## MCP tools

| Tier | Tools |
|---|---|
| Read-only | `identify`, `screenshot` (returns the image), `get_settings`, `get_waveform`, `scpi_query`, `save_setup` |
| Changes (undoable: pass the returned before-values back) | `measure` (adds items to the scope's measurement bar), `configure_channel`, `configure_timebase`, `configure_trigger`, `configure_acquisition` |
| Destructive | `run_control`, `clear_measurements`, `restore_setup`, `autoscale`, `reset`, `scpi_write` (the last four back up the setup first) |

The tools carry MCP read-only/destructive hints. In Claude Code you can allow the read-only ones
without prompting, e.g. `"mcp__rigol__screenshot"` in `permissions.allow`, and keep the rest behind
approval.

## Tests

```bash
uv run pytest                    # unit tests, no scope needed
uv run pytest -m hardware        # read-only tests against the real scope
uv run pytest -m hardware_write  # changes settings on the scope, then restores the saved setup
```
