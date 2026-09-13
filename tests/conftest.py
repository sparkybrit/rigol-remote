from __future__ import annotations

import struct
from collections import deque
from collections.abc import Callable

import pytest

from rigol_remote.scope import DS1054Z, parse_block
from rigol_remote.usbtmc import UsbtmcTransport

NO_ERROR = '0,"No error"'


class FakeRigol:
    """A PacketDevice that behaves like the DS1054Z's USB as observed through the kernel driver:
    64-byte packets, a 500-byte first transfer for long replies, replies padded to even length,
    and a zero-length packet after any transfer that exactly fills its last packet.

    Its SCPI side is a lookup table: `replies` answers exact queries; `state` holds settings that
    can be set ("HEADER value") and read back ("HEADER?"), optionally snapped by `snap` (a state
    value may be a callable, for readings that change); `effects` run on bare commands like ":RUN".
    Anything else it doesn't know queues an SCPI error."""

    def __init__(self, replies: dict[str, bytes | str] | None = None, state: dict | None = None):
        self.replies = {k: v.encode() if isinstance(v, str) else v for k, v in (replies or {}).items()}
        self.state = dict(state or {})
        self.snap: dict[str, Callable[[str], str]] = {}
        self.effects: dict[str, Callable[[FakeRigol], None]] = {}
        self.errors: list[str] = []
        self.blocks: dict[str, bytes] = {}
        self.commands: list[str] = []
        self.packets: deque[bytes] = deque()
        self._pending = b""
        self._first_transfer = True
        self.tag_offset = 0  # set non-zero to answer with the wrong bTag

    def write(self, data: bytes) -> None:
        msg_id, tag, inverse, size, attributes = struct.unpack_from("<BBBxIB", data)
        assert inverse == ~tag & 0xFF and 1 <= tag <= 255
        if msg_id == 1:  # DEV_DEP_MSG_OUT
            assert attributes & 1 and len(data) % 4 == 0
            message = data[12 : 12 + size]
            assert message.endswith(b"\n")
            self._execute(message[:-1])
        elif msg_id == 2:  # REQUEST_DEV_DEP_MSG_IN
            self._send_transfer(tag)
        else:
            raise AssertionError(f"unexpected MsgID {msg_id}")

    def _execute(self, message: bytes) -> None:
        header, _, argument = message.partition(b" ")
        if argument.startswith(b"#"):  # binary block argument
            self.blocks[header.decode()] = parse_block(argument)
            self.commands.append(f"{header.decode()} <block>")
            return
        command = message.decode()
        self.commands.append(command)
        header, _, argument = command.partition(" ")
        if header.endswith("?"):
            if command in self.replies:
                reply = self.replies[command]
            elif command == ":SYSTem:ERRor?":
                reply = (self.errors.pop(0) if self.errors else NO_ERROR).encode() + b"\n"
            elif header[:-1] in self.state and not argument:
                value = self.state[header[:-1]]
                reply = (value() if callable(value) else value).encode() + b"\n"
            else:
                reply = b""  # unknown query: no reply, so the host's read times out
            self._pending, self._first_transfer = reply, True
        elif header in self.state and argument:
            self.state[header] = self.snap.get(header, str)(argument)
        elif command in self.effects:
            self.effects[command](self)
        else:
            self.errors.append('-113,"Undefined header"')

    def _send_transfer(self, tag: int) -> None:
        if not self._pending:
            return  # nothing to say: the host's read times out
        n = 500 if self._first_transfer and len(self._pending) > 500 else len(self._pending)
        chunk, self._pending = self._pending[:n], self._pending[n:]
        self._first_transfer = False
        reply_tag = (tag + self.tag_offset - 1) % 255 + 1
        frame = struct.pack("<BBBxIB3x", 2, reply_tag, ~reply_tag & 0xFF, len(chunk), 0 if self._pending else 1)
        frame += chunk + b"^" * ((12 + len(chunk)) % 2)
        self.packets.extend(frame[i : i + 64] for i in range(0, len(frame), 64))
        if len(frame) % 64 == 0:
            self.packets.append(b"")

    def read_packet(self, timeout: float) -> bytes | None:
        return self.packets.popleft() if self.packets else None

    def close(self) -> None:
        pass


def block(payload: bytes) -> bytes:
    return b"#9%09d" % len(payload) + payload + b"\n"


SETUP = b"VZ8\x00DS1054Z\x00\x00\x00\x00\x00" + bytes(2065)  # shaped like a real :SYSTem:SETup? blob


def settings_state() -> dict[str, str]:
    """Every setting DS1054Z.settings() reads, formatted the way the real scope replies."""
    state = {}
    for n in range(1, 5):
        c = f":CHANnel{n}"
        state |= {
            f"{c}:DISPlay": "1" if n == 1 else "0", f"{c}:SCALe": "1.000000e+00", f"{c}:OFFSet": "0.000000e+00",
            f"{c}:COUPling": "DC", f"{c}:PROBe": "10", f"{c}:BWLimit": "OFF", f"{c}:INVert": "0",
        }
    return state | {
        ":TRIGger:MODE": "EDGE", ":TRIGger:SWEep": "AUTO", ":TRIGger:STATus": "AUTO",
        ":TRIGger:COUPling": "DC", ":TRIGger:HOLDoff": "1.600000e-08", ":TRIGger:EDGe:SOURce": "CHAN1",
        ":TRIGger:EDGe:SLOPe": "POS", ":TRIGger:EDGe:LEVel": "0.000000e+00",
        ":TIMebase:MODE": "MAIN", ":TIMebase:MAIN:SCALe": "1.000000e-06", ":TIMebase:MAIN:OFFSet": "0.000000e+00",
        ":ACQuire:TYPE": "NORM", ":ACQuire:AVERages": "2", ":ACQuire:MDEPth": "AUTO", ":ACQuire:SRATe": "1.000000e+09",
    }


def sent_commands(fake: FakeRigol) -> list[str]:
    """The non-query commands the fake received, in order."""
    return [c for c in fake.commands if not c.split()[0].endswith("?")]


@pytest.fixture
def make_scope():
    def make(replies: dict[str, bytes | str] | None = None, state: dict | None = None):
        fake = FakeRigol({"*OPC?": "1\n"} | (replies or {}), state)
        return DS1054Z(UsbtmcTransport(fake)), fake

    return make
