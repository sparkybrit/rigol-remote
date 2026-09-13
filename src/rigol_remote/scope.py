"""The DS1054Z's SCPI, wrapped: identity, screenshots, settings, measurements and waveforms.

Nothing here changes acquisition settings. Two side effects: measure() adds its items to the
on-screen measurement bar, and waveform() sets the :WAVeform: readout parameters, which only affect
what :WAVeform:DATA? returns.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Protocol

INVALID_MEASUREMENT = 9.9e37  # what :MEASure:ITEM? returns when a value can't be measured

# Single-source :MEASure:ITEM? items, long form with the short form in capitals, and their units.
_MEASUREMENTS = {
    "VMAX": "V", "VMIN": "V", "VPP": "V", "VTOP": "V", "VBASe": "V", "VAMP": "V", "VAVG": "V",
    "VRMS": "V", "VUPper": "V", "VMID": "V", "VLOWer": "V", "PVRMS": "V",
    "PERiod": "s", "RTIMe": "s", "FTIMe": "s", "PWIDth": "s", "NWIDth": "s", "TVMAX": "s", "TVMIN": "s",
    "FREQuency": "Hz", "OVERshoot": "%", "PREShoot": "%", "PDUTy": "%", "NDUTy": "%",
    "MARea": "V*s", "MPARea": "V*s", "PSLEWrate": "V/s", "NSLEWrate": "V/s", "VARIance": "V^2",
    "PPULses": "count", "NPULses": "count", "PEDGes": "count", "NEDGes": "count",
}
_ITEM_BY_NAME = {}
for _long, _unit in _MEASUREMENTS.items():
    _short = re.match(r"[A-Z]+", _long).group()
    _ITEM_BY_NAME[_short] = _ITEM_BY_NAME[_long.upper()] = (_short, _unit)
MEASUREMENT_ITEMS = sorted({short for short, _ in _ITEM_BY_NAME.values()})


class ScopeError(Exception):
    """The scope refused or couldn't do what was asked."""


class Transport(Protocol):
    def write(self, command: str) -> None: ...
    def query(self, command: str, timeout: float = ...) -> str: ...
    def query_bytes(self, command: str, timeout: float = ...) -> bytes: ...


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


def channel_name(channel: str | int) -> str:
    """'1', 'ch1', 'CHAN1', 'CHANnel1' → 'CHAN1'."""
    m = re.fullmatch(r"(?:CH(?:AN(?:NEL)?)?)?([1-4])", str(channel).strip().upper())
    if not m:
        raise ValueError(f"not a channel: {channel!r} (use 1-4 or CHAN1-CHAN4)")
    return f"CHAN{m.group(1)}"


def measurement_item(name: str) -> tuple[str, str]:
    """'freq' / 'FREQuency' → ('FREQ', 'Hz')."""
    try:
        return _ITEM_BY_NAME[name.strip().upper()]
    except KeyError:
        raise ValueError(f"unknown measurement {name!r}; choose from {', '.join(MEASUREMENT_ITEMS)}") from None


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


class DS1054Z:
    def __init__(self, transport: Transport):
        self.transport = transport

    def _q(self, command: str) -> str:
        return self.transport.query(command)

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
                "display": self._q(f"{c}:DISPlay?") == "1",
                "scale_v_per_div": float(self._q(f"{c}:SCALe?")),
                "offset_v": float(self._q(f"{c}:OFFSet?")),
                "coupling": self._q(f"{c}:COUPling?"),
                "probe_ratio": float(self._q(f"{c}:PROBe?")),
                "bw_limit": self._q(f"{c}:BWLimit?"),
                "invert": self._q(f"{c}:INVert?") == "1",
            }
        trigger = {
            "mode": self._q(":TRIGger:MODE?"),
            "sweep": self._q(":TRIGger:SWEep?"),
            "status": self._q(":TRIGger:STATus?"),
        }
        if trigger["mode"] == "EDGE":
            trigger |= {
                "source": self._q(":TRIGger:EDGe:SOURce?"),
                "slope": self._q(":TRIGger:EDGe:SLOPe?"),
                "level_v": float(self._q(":TRIGger:EDGe:LEVel?")),
            }
        depth = self._q(":ACQuire:MDEPth?")
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
                "memory_depth": int(depth) if depth.isdigit() else depth,
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
