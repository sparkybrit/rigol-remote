# rigol-remote

Drive a Rigol DS1054Z oscilloscope from Claude Code. This project is the shared tool other sessions
on this host use: a Python package with a `rigol` CLI and an MCP server (`rigol-mcp`), registered at
user scope so every Claude Code session gets its tools.

**Status (2026-09-13):** milestones 1 and 2 are done. Reading covers identify, screenshot, settings,
measure, waveform, SCPI query and setup save. Control covers channel, timebase, trigger and
acquisition changes, run/stop/single/force, setup restore, reset, autoscale, clearing the
measurement bar and raw SCPI writes. Everything goes over USB, and the unit tests, read-only
hardware tests and restoring control-hardware tests all pass. Items marked ✅ were verified on this
unit. Everything else comes from the DS1000Z Programming Guide or community experience. Update this
file when you confirm or correct something against the real scope.

## The instrument

- DS1054Z: 4 analog channels, 50 MHz stock, 1 GSa/s max, 800×480 screen. Sample rate and memory depth
  are shared across the enabled channels and depend on installed options, so query them rather than
  assuming. It has no AWG (that's the `-S` models) and no logic channels (MSO models only).
- ✅ `*IDN?` → `RIGOL TECHNOLOGIES,DS1054Z,DS1ZA224210859,00.04.04.SP4`.
- The bench also has a Saleae Logic Pro 16 (`logic2` MCP server), useful for cross-checking digital
  timing.

## Connecting

- ✅ **USB (in use):** USBTMC `1ab1:04ce` → `/dev/usbtmc0` via the kernel `usbtmc` driver, which loads
  automatically. The kernel creates the node as `root:root 0600`. The udev rule
  `/etc/udev/rules.d/99-rigol.rules`, installed 2026-09-13, makes it `root:plugdev 0660`:
  `KERNEL=="usbtmc[0-9]*", ATTRS{idVendor}=="1ab1", GROUP="plugdev", MODE="0660"`.
  `connection.find_usbtmc()` locates the scope through sysfs, so there's no config. `RIGOL_ADDR`
  overrides it.
- **LAN (not implemented, untested):** raw SCPI over TCP port 5555, `\n`-terminated. To find the IP,
  use the front panel (*Utility → IO Setting → LAN Conf.*) or run `ip neigh | grep -i 00:19:af`.
  `resolve_address()` rejects non-`/dev/` addresses until a `TcpTransport` exists.
- `sudo` needs a password on this box, so hand any privileged command to the user.

## USB transport ✅ (`usbtmc.py`; worked out on this unit, 2026-09-13)

**Plain `read()`/`write()` on `/dev/usbtmc0` is unusable for replies over 52 bytes.** The driver
silently truncates each reply to its first 64-byte packet minus the 12-byte header, and the rest is
lost. The root cause is that the scope's bulk endpoints declare `wMaxPacketSize` 64 while running at
high speed, where the spec requires 512.

What works: `usbtmc.py` does the USBTMC framing itself through the driver's raw ioctls
(`USBTMC_IOCTL_WRITE`/`READ` = `_IOWR(91, 13/14, struct usbtmc_message)`; the packed struct is
`u32 transfer_size, u32 transferred, u32 flags, void *message`). This needs no pyusb and no second
udev rule. `tests/conftest.py::FakeRigol` reproduces the observed behaviour below. Keep it faithful
when you learn more.

- **Send:** `01, bTag, ~bTag, 00, u32 len, 01 (EOM), 00 00 00`, then the payload, zero-padded to a
  multiple of 4. `bTag` cycles 1–255. ✅ One transfer works even for a 2 KB binary setup block.
- **Receive, one transfer per request:**
  1. Send `02, bTag, ~bTag, 00, u32 max_len, 00 00 00 00`.
  2. Read **one 64-byte packet per ioctl** until you have `12 + TransferSize` bytes.
  3. If the byte count is an exact multiple of 64, read and discard the **zero-length packet** that
     follows.
  4. Repeat while EOM (header byte 8, bit 0) is 0.
- Long replies are a 500-byte transfer followed by one transfer with the rest. The scope pads to
  **even** length, not a multiple of 4. The reply's bTag echoes the request's, which is how
  `read()` detects a desync.
- A ZLP (0-byte read) is not a timeout (`ETIMEDOUT`). Mistaking one for the other desyncs the stream.
- **Never use `USBTMC_IOCTL_CLEAR`.** It timed out (`-110`) and left bulk-OUT halted (`EPIPE`).
  `_IO(91, 6)`/`_IO(91, 7)` (clear OUT/IN halt) plus a drain recovered it. The first packet after
  that was lost. If that fails, replug the cable. `connect()` drains on open and after any error
  instead.
- The driver rejects timeouts under 100 ms.
- Measured: `*IDN?` ~1 ms; NORMal waveform (1212 B) 2–3 ms; PNG screenshot (32–58 KB) ~1 s; setup
  restore ~6 s.

## Architecture

```
src/rigol_remote/
  usbtmc.py      # KernelUsbtmc (raw ioctls, one packet at a time) + UsbtmcTransport (framing, quirks)
  connection.py  # find_usbtmc() via sysfs, flock-based exclusive lock, connect() context manager
  scope.py       # DS1054Z: reading, configure_*, run control, setup save/restore, reset/autoscale, write
  captures.py    # save_capture(), backup_setup(), read_setup(): ~/.local/share/rigol-remote/captures
  cli.py         # `rigol` console script (numbers take SI suffixes: 500m, 20ns)
  mcp_server.py  # `rigol-mcp`: MCPServer (mcp 2.x) over stdio; INSTRUCTIONS carry the rules for users
tests/           # unit tests on FakeRigol; test_hardware.py (read-only) and
                 # test_hardware_control.py (changes settings, then restores the saved setup)
```

- **Other sessions use the MCP server.** It's registered with
  `claude mcp add --scope user rigol -- ~/Projects/rigol-remote/.venv/bin/rigol-mcp`, and
  `~/.claude/CLAUDE.md` tells every session to use it and never touch `/dev/usbtmc*` directly. Keep
  the path stable: moving the project or deleting `.venv` breaks every session's server.
- **Every session runs its own server process**, so each operation is `with connect() as scope:`.
  That opens the device, takes an exclusive `flock` on the device node (waiting up to 30 s), drains,
  does the work and closes. Never hold the device open between tool calls.
- **Three tiers of tools, marked by MCP annotations** (tested in `test_mcp.py`), so users can allow
  some without prompts and keep the rest behind approval:
  - read-only: `identify`, `screenshot`, `get_settings`, `get_waveform`, `scpi_query`, `save_setup`
  - changes that can be undone: `measure` (adds to the measurement bar) and the `configure_*` tools
  - destructive: `run_control`, `clear_measurements`, `restore_setup`, `autoscale`, `reset`,
    `scpi_write`
- **Undo contract:** `configure_*` return `{requested, before, after}` per setting. Result keys are
  the parameter names, and `_apply` reads *every* before-value before writing anything, because
  changes interact (a new probe ratio rescales V/div). So passing the before-values back in one call
  is a true undo. The exception is trigger `mode`: it's switched to EDGE implicitly, so undo drops it.
  `restore_setup`, `autoscale`, `reset` and `scpi_write` save a setup backup first and return its
  path.
- **No PyVISA, pyusb or NI-VISA.** PyVISA's pure-Python USB path goes through libusb, which means
  detaching the kernel driver and a second udev rule, and it doesn't handle this scope's quirks. The
  `write`/`query`/`query_bytes` transport interface is VISA-like, so switching is cheap if more
  instruments arrive.
- **MCP error handling:** only `ToolError` messages reach the model; any other exception shows up as
  "Error executing tool X". `_with_scope()` converts `ScopeError`/`OSError`/`ValueError`.
- **Validation before sending:** every argument is checked and formatted in `scope.py` (`choice()`,
  `number()`, `channel_name()`…), so nothing a model passes can inject SCPI. `check_read_only_query()`
  and `check_command()` allow exactly one command each: no `;`, a query only when its header ends in
  `?`, and never a query through `write`, because the unread reply would desync the stream.
- Captures deliberately don't use `$XDG_DATA_HOME`: inside the VS Code snap it points at
  `~/snap/code/<rev>/…`, which vanishes on update.

**Roadmap:**
1. Verify `reset`/`autoscale`/`clear_measurements` on hardware. This needs the user's OK, because
   they wipe the setup or the measurement bar.
2. RAW (memory-depth) waveforms.
3. Waveform plots.
4. `TcpTransport` once a network cable is connected.

## Commands

uv manages everything; it's at `~/.local/bin/uv`. Python is the system 3.12, pinned in `.python-version`.

```bash
uv sync                           # create/refresh .venv from uv.lock
uv run pytest                     # unit tests, no scope needed (hardware tests deselected by default)
uv run pytest -m hardware         # read-only tests against the real scope
uv run pytest -m hardware_write   # changes settings and restores them (~15 s; the user sees it)
RIGOL_ALLOW_RESET=1 uv run pytest -m hardware_write   # ...including *RST and autoscale
uv run rigol --help               # read: idn screenshot settings measure waveform query save-setup
                                  # change: channel timebase trigger acquisition run stop single force
                                  #         clear-measurements restore-setup autoscale reset write
claude mcp get rigol              # check the registered server; `claude mcp list` health-checks it
```

After changing the MCP server, sessions pick up the new code only when their server restarts
(`/mcp` → reconnect, or a new session).

## Conventions

- Structured CLI output is JSON on stdout, in SI floats (V, s, Hz, Sa/s) with units in the key names.
  `query` prints the raw reply. Errors go to stderr with exit code 1. `reset` and `autoscale` require
  `--yes`.
- A measurement the scope can't make (`9.9E37`) becomes `null`, never a number.
- Every change is read back and the error queue checked (`_apply`, `_command`). Report the scope's
  "after", not what was requested.
- Every parsing and scaling path gets a unit test on `FakeRigol`. Hardware tests either only read,
  or restore everything they change and verify that they did.

## Rules for driving the live scope

- **Never report a reading you didn't just get from the instrument.** Quote the returned values with
  units. If there's no live connection, say so.
- **The user may have set the scope up by hand.** Change only what they asked for. Confirm before
  anything broader: reset, autoscale, clearing measurements, or channels they didn't mention. Also
  confirm before `run`/`single` while the scope is stopped on a capture, because it gets
  overwritten.
- ✅ `measure` has a visible side effect: `:MEASure:ITEM?` adds the item to the on-screen measurement
  bar. The bar holds at most 5 items, so this can scroll off the user's own readouts.
- **Physical actions are the user's:** probing, the probe's 1×/10× switch, compensation, cabling.
- Voltages off by exactly 10× mean the channel's `:PROBe` ratio doesn't match the physical probe.

## Behaviour verified on this unit

- ✅ **Snapping:** the scope snaps to its own steps. A requested 30 ns/div became 50 ns/div.
  Trigger level and offset snap to fractions of a division.
- ✅ **Status lag:** `:TRIGger:STATus?` lags run-control commands by ~100–150 ms, even after `*OPC?`.
  `run`/`stop` poll until it changes. `single` first waits for the scope to arm, so a stale `STOP`
  isn't mistaken for a capture.
- ✅ **Single and run:** `:SINGle` sets the sweep to `SING`, and a following `:RUN` restores the
  previous sweep mode.
- ✅ **Setup snapshot:** `:SYSTem:SETup?` is a 2081-byte binary blob starting `VZ8\0DS1054Z`.
  Writing it back with `:SYSTem:SETup #9…` (one 2 KB transfer) takes ~6 s and restores every
  setting. The blob isn't byte-identical afterwards (it holds transient state), so compare
  `settings()` instead.
- ✅ **Waveforms:** `:WAVeform:SOURce/MODE NORMal/FORMat BYTE`, then `:PREamble?`
  (`format,type,points,count,xinc,xorigin,xref,yinc,yorigin,yref`) and `:DATA?` (1200 points).
  `V = (raw − yorigin − yref) × yinc` matched the scope's own VPP to within 2%.
  Raw 0 or 255 means clipped.
  - `RAW` mode (untested) needs `:STOP` first. Read it in chunks of ≤250 000 points via
    `:WAVeform:STARt`/`:STOP`, which are 1-based and inclusive.
- ✅ **Screenshot:** `:DISPlay:DATA? ON,OFF,PNG` returns an 800×480 RGB PNG. Binary replies are TMC
  blocks: `#9<9-digit length><data>\n`.

## SCPI quick reference

Capitals mark the short form (`:TRIGger:STATus?` ≡ `:TRIG:STAT?`). ✅ = verified on this unit, for
both the query and the set form unless noted.

| Area | Commands |
|---|---|
| Identity / errors | `*IDN?` ✅, `*OPC?` ✅, `:SYSTem:ERRor?` ✅ |
| Run control | `:RUN` ✅, `:STOP` ✅, `:SINGle` ✅, `:TFORce` ✅; `:TRIGger:STATus?` ✅ → `TD` / `WAIT` / `RUN` / `AUTO` / `STOP` |
| Channel *n* | `:CHANnel<n>:DISPlay` ✅, `:PROBe` ✅, `:SCALe` ✅, `:OFFSet` ✅, `:COUPling` ✅, `:BWLimit` ✅, `:INVert` ✅, `:VERNier` ✅ |
| Timebase | `:TIMebase:MAIN:SCALe` ✅, `:TIMebase:MAIN:OFFSet` ✅, `:TIMebase:MODE` (query ✅; XY/ROLL untested) |
| Trigger | `:TRIGger:MODE` ✅, `:SWEep` ✅, `:COUPling` ✅, `:HOLDoff` ✅, `:EDGe:SOURce` ✅ (AC line untested), `:EDGe:SLOPe` ✅, `:EDGe:LEVel` ✅ |
| Acquire | `:ACQuire:TYPE` ✅, `:AVERages` ✅, `:MDEPth` ✅, `:SRATe?` ✅ |
| Measure | `:MEASure:ITEM? <item>,CHAN<n>` ✅ (VPP, VAVG, VRMS, FREQ tried; the full list is in `scope.py`); `:MEASure:CLEar ALL\|ITEM<n>` (untested) |
| Waveform | `:WAVeform:SOURce` ✅, `:MODE` ✅, `:FORMat` ✅, `:PREamble?` ✅, `:DATA?` ✅, `:STARt`, `:STOP` |
| Screen / setup | `:DISPlay:DATA? ON,OFF,PNG` ✅, `:SYSTem:SETup?` ✅, `:SYSTem:SETup <block>` ✅ |
| **Destructive** | `*RST`, `:AUToscale` (untested: they wipe the user's setup), `:CLEar` |

The authoritative source is the *MSO1000Z/DS1000Z Programming Guide*. Save the PDF under `docs/` and
Read it with the `pages` parameter.
