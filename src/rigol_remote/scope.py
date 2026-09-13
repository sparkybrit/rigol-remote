"""The DS1054Z's SCPI, wrapped.

Reading: identify, screenshot, settings, measure, waveform, query. Reading never changes acquisition
settings, with two side effects: measure() adds its items to the on-screen measurement bar, and
waveform() sets the :WAVeform: readout parameters (they only affect what :WAVeform:DATA? returns).

Control: configure_* change only the settings passed. Each change is read back and returned with
its before value, so it can be undone, and the error queue is checked afterwards. save_setup() and
restore_setup() snapshot and restore the whole setup.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

INVALID_MEASUREMENT = 9.9e37  # what :MEASure:ITEM? returns when a value can't be measured
SETUP_SIGNATURE = b"DS1054Z"  # appears in the first bytes of a :SYSTem:SETup? block

# Single-source :MEASure:ITEM? items, long form with the short form in capitals, and their units.
_MEASUREMENTS = {
    "VMAX": "V", "VMIN": "V", "VPP": "V", "VTOP": "V", "VBASe": "V", "VAMP": "V", "VAVG": "V",
    "VRMS": "V", "VUPper": "V", "VMID": "V", "VLOWer": "V", "PVRMS": "V",
    "PERiod": "s", "RTIMe": "s", "FTIMe": "s", "PWIDth": "s", "NWIDth": "s", "TVMAX": "s", "TVMIN": "s",
    "FREQuency": "Hz", "OVERshoot": "%", "PREShoot": "%", "PDUTy": "%", "NDUTy": "%",
    "MARea": "V*s", "MPARea": "V*s", "PSLEWrate": "V/s", "NSLEWrate": "V/s", "VARIance": "V^2",
    "PPULses": "count", "NPULses": "count", "PEDGes": "count", "NEDGes": "count",
}
PROBE_RATIOS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000)
AVERAGES = tuple(2**n for n in range(1, 11))  # 2..1024
MEMORY_DEPTHS = (3000, 6000, 12000, 30000, 60000, 120000, 300000, 600000,
                 1200000, 3000000, 6000000, 12000000, 24000000)  # valid subset depends on channels on


class ScopeError(Exception):
    """The scope refused or couldn't do what was asked."""


class Transport(Protocol):
    def write(self, command: str) -> None: ...
    def write_block(self, command: str, data: bytes) -> None: ...
    def query(self, command: str, timeout: float = ...) -> str: ...
    def query_bytes(self, command: str, timeout: float = ...) -> bytes: ...


# --- validation: every argument is checked and formatted here before anything is sent ---

def _short(keyword: str) -> str:
    return re.match(r"[A-Z0-9]+", keyword).group()


def choice(value: str, keywords: Iterable[str], what: str, aliases: Mapping[str, str] | None = None) -> str:
    """Match value against SCPI keywords (long form with the short form in capitals, e.g. 'NORMal')
    or friendly aliases; return the short form. SCPI accepts only the short or the full long form."""
    v = str(value).strip()
    for keyword in keywords:
        if v.upper() in (keyword.upper(), _short(keyword)):
            return _short(keyword)
    if aliases and v.lower() in aliases:
        return aliases[v.lower()]
    options = [_short(k) for k in keywords] + sorted(aliases or {})
    raise ValueError(f"unknown {what} {value!r}; choose from {', '.join(options)}")


def number(value: float, what: str) -> str:
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{what} must be a finite number, not {value!r}")
    return f"{v:.7g}"


def channel_name(channel: str | int) -> str:
    """'1', 'ch1', 'CHAN1', 'CHANnel1' → 'CHAN1'."""
    m = re.fullmatch(r"(?:CH(?:AN(?:NEL)?)?)?([1-4])", str(channel).strip().upper())
    if not m:
        raise ValueError(f"not a channel: {channel!r} (use 1-4 or CHAN1-CHAN4)")
    return f"CHAN{m.group(1)}"


def measurement_item(name: str) -> tuple[str, str]:
    """'freq' / 'FREQuency' → ('FREQ', 'Hz')."""
    short = choice(name, _MEASUREMENTS, "measurement")
    return short, _UNITS[short]


_UNITS = {_short(k): unit for k, unit in _MEASUREMENTS.items()}
MEASUREMENT_ITEMS = sorted(_UNITS)


def _probe_ratio(value: float) -> str:
    v = float(value)
    if not any(math.isclose(v, r) for r in PROBE_RATIOS):
        raise ValueError(f"probe ratio must be one of {', '.join(f'{r:g}' for r in PROBE_RATIOS)}")
    return f"{v:g}"


def _flag(value: bool) -> str:
    if not isinstance(value, bool):
        raise ValueError(f"expected true or false, not {value!r}")
    return "1" if value else "0"


def check_read_only_query(command: str) -> str:
    """Accept a single SCPI query; refuse anything that could change the scope's state."""
    command = command.strip()
    if not command or any(c in command for c in ";\r\n"):
        raise ValueError("send exactly one SCPI query (no ';' or newlines)")
    header = command.split()[0]
    if not header.endswith("?"):
        raise ValueError(f"{header!r} is not a query (its header must end in '?')")
    if header.upper() == "*TST?":
        raise ValueError("*TST? runs a self-test; not allowed as a read-only query")
    return command


def check_command(command: str) -> str:
    """Accept a single SCPI command that expects no reply."""
    command = command.strip()
    if not command or any(c in command for c in ";\r\n"):
        raise ValueError("send exactly one SCPI command (no ';' or newlines)")
    if "?" in command.split()[0]:
        raise ValueError("that's a query: use the query tool (an unread reply would desync the scope)")
    return command


# --- read-back parsers ---

def _is_on(reply: str) -> bool:
    return reply in ("1", "ON")


def _depth(reply: str) -> int | str:
    return int(reply) if reply.isdigit() else reply


@dataclass
class _Change:
    key: str  # the configure_* parameter name, so a result's before-values can be passed back to undo
    header: str  # SCPI header; queried with '?' for the read-back
    argument: str  # validated, formatted argument
    parse: Callable[[str], Any]


@dataclass
class Waveform:
    """Screen waveform in raw BYTE form plus the preamble needed to scale it."""

    channel: str
    raw: bytes
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    @classmethod
    def from_preamble(cls, channel: str, preamble: str, raw: bytes) -> Waveform:
        # format,type,points,count,xincrement,xorigin,xreference,yincrement,yorigin,yreference
        f = [float(v) for v in preamble.split(",")]
        if len(f) != 10:
            raise ScopeError(f"unexpected preamble: {preamble!r}")
        return cls(channel, raw, f[4], f[5], f[6], f[7], f[8], f[9])

    def times(self) -> list[float]:
        return [(i - self.x_reference) * self.x_increment + self.x_origin for i in range(len(self.raw))]

    def volts(self) -> list[float]:
        return [(b - self.y_origin - self.y_reference) * self.y_increment for b in self.raw]

    def summary(self) -> dict:
        v = self.volts()
        mean = sum(v) / len(v)
        return {
            "channel": self.channel,
            "points": len(v),
            "t_start_s": self.x_origin - self.x_reference * self.x_increment,
            "t_step_s": self.x_increment,
            "v_min": min(v),
            "v_max": max(v),
            "v_pp": max(v) - min(v),
            "v_mean": mean,
            "v_rms": math.sqrt(sum(x * x for x in v) / len(v)),
            # 0 and 255 are the ADC's rails: the trace ran off screen, so extremes are unreliable.
            "clipped": any(b in (0, 255) for b in self.raw),
        }

    def csv(self) -> str:
        return "time_s,volts\n" + "".join(f"{t:.6e},{v:.6e}\n" for t, v in zip(self.times(), self.volts()))


def parse_block(data: bytes) -> bytes:
    """Payload of an IEEE 488.2 definite-length block: #<n><n-digit length><payload>[\\n]."""
    if len(data) < 2 or data[:1] != b"#" or not b"1" <= data[1:2] <= b"9":
        raise ValueError(f"not a definite-length block: {data[:12]!r}")
    digits = int(data[1:2])
    length = int(data[2 : 2 + digits])
    payload = data[2 + digits : 2 + digits + length]
    if len(payload) != length:
        raise ValueError(f"block truncated: {len(payload)} of {length} bytes")
    return payload


class DS1054Z:
    def __init__(self, transport: Transport):
        self.transport = transport

    def _q(self, command: str) -> str:
        return self.transport.query(command)

    # --- reading ---

    def identify(self) -> dict:
        manufacturer, model, serial, firmware = self._q("*IDN?").split(",")
        return {"manufacturer": manufacturer, "model": model, "serial": serial, "firmware": firmware}

    def errors(self) -> list[str]:
        """Drain the error queue; [] means no errors."""
        errors = []
        for _ in range(32):
            err = self._q(":SYSTem:ERRor?")
            if err.startswith("0,"):
                break
            errors.append(err)
        return errors

    def screenshot_png(self) -> bytes:
        png = parse_block(self.transport.query_bytes(":DISPlay:DATA? ON,OFF,PNG", timeout=10))
        if not png.startswith(b"\x89PNG"):
            raise ScopeError(f"screenshot is not a PNG (starts {png[:8]!r})")
        return png

    def settings(self) -> dict:
        channels = {}
        for n in range(1, 5):
            c = f":CHANnel{n}"
            channels[f"CHAN{n}"] = {
                "display": _is_on(self._q(f"{c}:DISPlay?")),
                "scale_v_per_div": float(self._q(f"{c}:SCALe?")),
                "offset_v": float(self._q(f"{c}:OFFSet?")),
                "coupling": self._q(f"{c}:COUPling?"),
                "probe_ratio": float(self._q(f"{c}:PROBe?")),
                "bw_limit": self._q(f"{c}:BWLimit?"),
                "invert": _is_on(self._q(f"{c}:INVert?")),
            }
        trigger = {
            "mode": self._q(":TRIGger:MODE?"),
            "sweep": self._q(":TRIGger:SWEep?"),
            "status": self._q(":TRIGger:STATus?"),
            "coupling": self._q(":TRIGger:COUPling?"),
            "holdoff_s": float(self._q(":TRIGger:HOLDoff?")),
        }
        if trigger["mode"] == "EDGE":
            trigger |= {
                "source": self._q(":TRIGger:EDGe:SOURce?"),
                "slope": self._q(":TRIGger:EDGe:SLOPe?"),
                "level_v": float(self._q(":TRIGger:EDGe:LEVel?")),
            }
        return {
            "channels": channels,
            "timebase": {
                "mode": self._q(":TIMebase:MODE?"),
                "scale_s_per_div": float(self._q(":TIMebase:MAIN:SCALe?")),
                "offset_s": float(self._q(":TIMebase:MAIN:OFFSet?")),
            },
            "trigger": trigger,
            "acquire": {
                "type": self._q(":ACQuire:TYPE?"),
                "averages": int(self._q(":ACQuire:AVERages?")),
                "memory_depth": _depth(self._q(":ACQuire:MDEPth?")),
                "sample_rate_sa_per_s": float(self._q(":ACQuire:SRATe?")),
            },
        }

    def measure(self, channel: str | int, items: list[str]) -> dict:
        """Current value of each measurement item; None where the scope can't measure it."""
        ch = channel_name(channel)
        wanted = [measurement_item(i) for i in items]  # validate everything before talking to the scope
        results = {}
        for short, unit in wanted:
            value = float(self._q(f":MEASure:ITEM? {short},{ch}"))
            results[short] = {"value": None if value >= INVALID_MEASUREMENT else value, "unit": unit}
        return {"channel": ch, "measurements": results}

    def waveform(self, channel: str | int) -> Waveform:
        """The on-screen trace of one channel (NORMal mode: 1200 points, works while running)."""
        ch = channel_name(channel)
        if self._q(f":{ch}:DISPlay?") != "1":
            raise ScopeError(f"{ch} is switched off, so it has no waveform")
        self.transport.write(f":WAVeform:SOURce {ch}")
        self.transport.write(":WAVeform:MODE NORMal")
        self.transport.write(":WAVeform:FORMat BYTE")
        preamble = self._q(":WAVeform:PREamble?")
        raw = parse_block(self.transport.query_bytes(":WAVeform:DATA?", timeout=5))
        return Waveform.from_preamble(ch, preamble, raw)

    def query(self, command: str) -> str:
        """Read-only escape hatch: one SCPI query, reply as text (binary blocks are summarised)."""
        reply = self.transport.query_bytes(check_read_only_query(command), timeout=10)
        if reply.startswith(b"#"):
            try:
                return f"<binary block, {len(parse_block(reply))} bytes>"
            except ValueError:
                pass
        return reply.decode("ascii", errors="replace").rstrip("\n")

    # --- changing settings ---

    def _apply(self, changes: list[_Change]) -> dict:
        """Make the changes in order and check the error queue.

        Every "before" is read before anything is written, and every "after" once all writes are
        done: changes interact (a new probe ratio rescales V/div), so passing the before values
        back in one call is a true undo.
        """
        if not changes:
            raise ValueError("nothing to change: give at least one setting")
        self.errors()  # start from an empty queue so any errors below are ours
        before = {c.key: c.parse(self._q(f"{c.header}?")) for c in changes}
        for c in changes:
            self.transport.write(f"{c.header} {c.argument}")
            self._q("*OPC?")  # one command at a time: don't let writes pile up in the scope's parser
        results = {
            c.key: {"requested": c.parse(c.argument), "before": before[c.key], "after": c.parse(self._q(f"{c.header}?"))}
            for c in changes
        }
        if errors := self.errors():
            raise ScopeError(f"the scope reported {errors}; values now: {results}")
        return results

    def configure_channel(
        self,
        channel: str | int,
        *,
        display: bool | None = None,
        probe_ratio: float | None = None,
        scale_v_per_div: float | None = None,
        offset_v: float | None = None,
        coupling: str | None = None,
        bw_limit: str | None = None,
        invert: bool | None = None,
    ) -> dict:
        ch = channel_name(channel)
        c = f":CHANnel{ch[-1]}"
        changes = []
        if display is not None:
            changes.append(_Change("display", f"{c}:DISPlay", _flag(display), _is_on))
        # Probe ratio first: it rescales V/div. Scale before offset: the offset range depends on it.
        if probe_ratio is not None:
            changes.append(_Change("probe_ratio", f"{c}:PROBe", _probe_ratio(probe_ratio), float))
        if scale_v_per_div is not None:
            changes.append(_Change("scale_v_per_div", f"{c}:SCALe", number(scale_v_per_div, "scale"), float))
        if offset_v is not None:
            changes.append(_Change("offset_v", f"{c}:OFFSet", number(offset_v, "offset"), float))
        if coupling is not None:
            changes.append(_Change("coupling", f"{c}:COUPling", choice(coupling, ("AC", "DC", "GND"), "coupling"), str))
        if bw_limit is not None:
            changes.append(_Change("bw_limit", f"{c}:BWLimit", choice(bw_limit, ("20M", "OFF"), "bandwidth limit"), str))
        if invert is not None:
            changes.append(_Change("invert", f"{c}:INVert", _flag(invert), _is_on))
        return {"channel": ch, "changes": self._apply(changes)}

    def configure_timebase(
        self, *, scale_s_per_div: float | None = None, offset_s: float | None = None, mode: str | None = None
    ) -> dict:
        changes = []
        if mode is not None:
            changes.append(_Change("mode", ":TIMebase:MODE", choice(mode, ("MAIN", "XY", "ROLL"), "timebase mode"), str))
        if scale_s_per_div is not None:
            changes.append(_Change("scale_s_per_div", ":TIMebase:MAIN:SCALe", number(scale_s_per_div, "scale"), float))
        if offset_s is not None:
            changes.append(_Change("offset_s", ":TIMebase:MAIN:OFFSet", number(offset_s, "offset"), float))
        return {"changes": self._apply(changes)}

    def configure_trigger(
        self,
        *,
        sweep: str | None = None,
        source: str | None = None,
        slope: str | None = None,
        level_v: float | None = None,
        coupling: str | None = None,
        holdoff_s: float | None = None,
    ) -> dict:
        """Edge trigger settings; giving source, slope or level switches the trigger to EDGE mode."""
        changes = []
        if source is not None or slope is not None or level_v is not None:
            changes.append(_Change("mode", ":TRIGger:MODE", "EDGE", str))
        if source is not None:
            src = "AC" if str(source).strip().upper() in ("AC", "LINE") else channel_name(source)
            changes.append(_Change("source", ":TRIGger:EDGe:SOURce", src, str))
        if slope is not None:
            s = choice(slope, ("POSitive", "NEGative", "RFALl"), "slope",
                       {"rising": "POS", "falling": "NEG", "either": "RFAL"})
            changes.append(_Change("slope", ":TRIGger:EDGe:SLOPe", s, str))
        if coupling is not None:
            cp = choice(coupling, ("AC", "DC", "LFReject", "HFReject"), "trigger coupling")
            changes.append(_Change("coupling", ":TRIGger:COUPling", cp, str))
        if holdoff_s is not None:
            changes.append(_Change("holdoff_s", ":TRIGger:HOLDoff", number(holdoff_s, "holdoff"), float))
        if level_v is not None:  # after the source: the level's range depends on its scale and offset
            changes.append(_Change("level_v", ":TRIGger:EDGe:LEVel", number(level_v, "level"), float))
        if sweep is not None:
            changes.append(_Change("sweep", ":TRIGger:SWEep", choice(sweep, ("AUTO", "NORMal", "SINGle"), "sweep"), str))
        return {"changes": self._apply(changes)}

    def configure_acquisition(
        self, *, acquisition_type: str | None = None, averages: int | None = None, memory_depth: int | str | None = None
    ) -> dict:
        changes = []
        if acquisition_type is not None:
            t = choice(acquisition_type, ("NORMal", "AVERages", "PEAK", "HRESolution"), "acquisition type",
                       {"average": "AVER", "high_resolution": "HRES"})
            changes.append(_Change("acquisition_type", ":ACQuire:TYPE", t, str))
        if averages is not None:
            if averages not in AVERAGES:
                raise ValueError(f"averages must be a power of two from 2 to 1024, not {averages!r}")
            changes.append(_Change("averages", ":ACQuire:AVERages", str(int(averages)), int))
        if memory_depth is not None:
            if str(memory_depth).strip().upper() == "AUTO":
                depth = "AUTO"
            elif memory_depth in MEMORY_DEPTHS:
                depth = str(int(memory_depth))
            else:
                raise ValueError(f"memory depth must be AUTO or one of {', '.join(map(str, MEMORY_DEPTHS))}")
            changes.append(_Change("memory_depth", ":ACQuire:MDEPth", depth, _depth))
        return {"changes": self._apply(changes)}

    # --- run control ---

    def _run_state(self) -> dict:
        self._q("*OPC?")  # let the preceding command take effect first
        return {"status": self._q(":TRIGger:STATus?"), "sweep": self._q(":TRIGger:SWEep?")}

    def _wait_for_status(self, done: Callable[[str], bool], timeout: float) -> dict:
        # :TRIGger:STATus? lags run-control commands by ~100-150 ms, even after *OPC?.
        deadline = time.monotonic() + timeout
        state = self._run_state()
        while not done(state["status"]) and time.monotonic() < deadline:
            time.sleep(0.05)
            state = self._run_state()
        return state

    def run(self) -> dict:
        self.transport.write(":RUN")  # after a single, this also restores the previous sweep mode
        return self._wait_for_status(lambda s: s != "STOP", 2.0)

    def stop(self) -> dict:
        self.transport.write(":STOP")
        return self._wait_for_status(lambda s: s == "STOP", 2.0)

    def force_trigger(self) -> dict:
        self.transport.write(":TFORce")
        return self._run_state()

    def single(self, wait_s: float = 0.0) -> dict:
        """Arm a single acquisition; optionally wait up to wait_s for it to complete (status STOP)."""
        self.transport.write(":SINGle")
        # Wait for it to arm first, so a stale STOP from before isn't mistaken for a capture. A
        # capture that completes within the lag goes straight to STOP, which ends this wait too.
        state = self._wait_for_status(lambda s: s != "STOP", 0.5)
        if wait_s > 0:
            state = self._wait_for_status(lambda s: s == "STOP", wait_s)
        return state | {"captured": state["status"] == "STOP"}

    # --- whole-setup and screen-wide operations ---

    def _command(self, command: str, timeout: float = 10.0) -> None:
        """Send a command, wait until the scope has processed it, and check the error queue."""
        self.errors()
        self.transport.write(command)
        self.transport.query("*OPC?", timeout=timeout)
        if errors := self.errors():
            raise ScopeError(f"{command}: the scope reported {errors}")

    def clear_measurements(self, item: int | None = None) -> dict:
        """Remove one item (1-5) or all items from the on-screen measurement bar."""
        if item is not None and item not in range(1, 6):
            raise ValueError("measurement item must be 1-5 (or omitted for all)")
        target = "ALL" if item is None else f"ITEM{item}"
        self._command(f":MEASure:CLEar {target}")
        return {"cleared": target}

    def save_setup(self) -> bytes:
        """The whole setup as the scope's own binary blob (restore it with restore_setup)."""
        return parse_block(self.transport.query_bytes(":SYSTem:SETup?", timeout=5))

    def restore_setup(self, data: bytes) -> dict:
        if SETUP_SIGNATURE not in data[:16]:
            raise ValueError("that is not a DS1054Z setup saved by save_setup")
        self.errors()
        self.transport.write_block(":SYSTem:SETup", data)
        self.transport.query("*OPC?", timeout=30)  # applying a setup takes the scope ~6 s
        if errors := self.errors():
            raise ScopeError(f"restoring the setup: the scope reported {errors}")
        return self.settings()

    def autoscale(self) -> dict:
        self._command(":AUToscale", timeout=30)
        return self.settings()

    def reset(self) -> dict:
        self._command("*RST", timeout=30)
        return self.settings()

    def write(self, command: str) -> None:
        """Escape hatch: one SCPI command that expects no reply; raises if the scope reports an error."""
        self._command(check_command(command))
