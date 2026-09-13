from __future__ import annotations

import struct
from collections import deque

import pytest

from rigol_remote.scope import DS1054Z
from rigol_remote.usbtmc import UsbtmcTransport


class FakeRigol:
    """A PacketDevice that behaves like the DS1054Z's USB as observed through the kernel driver:
    64-byte packets, a 500-byte first transfer for long replies, replies padded to even length,
    and a zero-length packet after any transfer that exactly fills its last packet."""

    def __init__(self, replies: dict[str, bytes | str] | None = None):
        self.replies = {k: v.encode() if isinstance(v, str) else v for k, v in (replies or {}).items()}
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
            command = data[12 : 12 + size].decode()
            assert command.endswith("\n")
            command = command[:-1]
            self.commands.append(command)
            if "?" in command.split()[0]:
                self._pending = self.replies.get(command, b"")
                self._first_transfer = True
        elif msg_id == 2:  # REQUEST_DEV_DEP_MSG_IN
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
        else:
            raise AssertionError(f"unexpected MsgID {msg_id}")

    def read_packet(self, timeout: float) -> bytes | None:
        return self.packets.popleft() if self.packets else None

    def close(self) -> None:
        pass


def block(payload: bytes) -> bytes:
    return b"#9%09d" % len(payload) + payload + b"\n"


@pytest.fixture
def make_scope():
    def make(replies: dict[str, bytes | str]) -> tuple[DS1054Z, FakeRigol]:
        fake = FakeRigol(replies)
        return DS1054Z(UsbtmcTransport(fake)), fake

    return make
