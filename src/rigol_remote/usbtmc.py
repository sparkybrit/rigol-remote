"""USBTMC to a Rigol DS1000Z through the Linux kernel driver's raw ioctls.

Plain read() on /dev/usbtmcN truncates this scope's replies to 52 bytes: its bulk endpoints declare
wMaxPacketSize 64 while running at high speed, and the driver hands back only the first packet. So
we do the USBTMC framing ourselves with USBTMC_IOCTL_WRITE / USBTMC_IOCTL_READ, one packet at a
time. CLAUDE.md ("USB transport") records the observed device behaviour this implements.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import struct
from typing import Protocol

PACKET_SIZE = 64  # the scope's (non-compliant) bulk wMaxPacketSize
HEADER_SIZE = 12
DEV_DEP_MSG_OUT = 1
REQUEST_DEV_DEP_MSG_IN = 2
EOM = 0x01
MAX_TRANSFER = 1 << 20  # TransferSize offered in each REQUEST_DEV_DEP_MSG_IN
PACKET_TIMEOUT = 2.0  # seconds to wait for any packet after a reply has started
MIN_TIMEOUT = 0.1  # the driver rejects timeouts under 100 ms


class UsbtmcError(OSError):
    """The USBTMC exchange went wrong; the stream may be out of sync until drained."""


class PacketDevice(Protocol):
    """Raw bulk I/O: all UsbtmcTransport needs from the device (faked in tests)."""

    def write(self, data: bytes) -> None: ...

    def read_packet(self, timeout: float) -> bytes | None:
        """Return one bulk-IN packet: b"" for a zero-length packet, None on timeout."""
        ...

    def close(self) -> None: ...


class _Message(ctypes.Structure):  # struct usbtmc_message, <linux/usb/tmc.h>
    _pack_ = 1
    _fields_ = [
        ("transfer_size", ctypes.c_uint32),
        ("transferred", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("message", ctypes.c_void_p),
    ]


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (91 << 8) | nr  # USBTMC_IOC_NR = 91


_IOCTL_WRITE = _ioc(3, 13, ctypes.sizeof(_Message))
_IOCTL_READ = _ioc(3, 14, ctypes.sizeof(_Message))
_IOCTL_SET_TIMEOUT = _ioc(1, 10, 4)


class KernelUsbtmc:
    """PacketDevice over /dev/usbtmcN, using the driver's raw bulk ioctls."""

    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        self._timeout_ms: int | None = None

    def _set_timeout(self, seconds: float) -> None:
        ms = int(max(seconds, MIN_TIMEOUT) * 1000)
        if ms != self._timeout_ms:
            fcntl.ioctl(self.fd, _IOCTL_SET_TIMEOUT, struct.pack("I", ms))
            self._timeout_ms = ms

    def write(self, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, len(data))
        msg = _Message(len(data), 0, 0, ctypes.cast(buf, ctypes.c_void_p))
        fcntl.ioctl(self.fd, _IOCTL_WRITE, msg)
        if msg.transferred != len(data):
            raise UsbtmcError(f"short write: {msg.transferred} of {len(data)} bytes")

    def read_packet(self, timeout: float) -> bytes | None:
        self._set_timeout(timeout)
        buf = ctypes.create_string_buffer(PACKET_SIZE)
        msg = _Message(PACKET_SIZE, 0, 0, ctypes.cast(buf, ctypes.c_void_p))
        try:
            fcntl.ioctl(self.fd, _IOCTL_READ, msg)
        except OSError as e:
            if e.errno == errno.ETIMEDOUT:
                return None
            raise
        return buf.raw[: msg.transferred]

    def close(self) -> None:
        os.close(self.fd)


class UsbtmcTransport:
    """SCPI over USBTMC, with the DS1000Z's framing quirks handled."""

    def __init__(self, device: PacketDevice):
        self.device = device
        self._tag = 0

    def _next_tag(self) -> int:
        self._tag = self._tag % 255 + 1  # 1..255; 0 is not a valid bTag
        return self._tag

    @staticmethod
    def _header(msg_id: int, tag: int, size: int, attributes: int = 0) -> bytes:
        return struct.pack("<BBBxIB3x", msg_id, tag, ~tag & 0xFF, size, attributes)

    def write(self, command: str) -> None:
        self._send(command.encode("ascii") + b"\n")

    def write_block(self, command: str, data: bytes) -> None:
        """Send `command` with `data` as an IEEE 488.2 definite-length block argument."""
        self._send(command.encode("ascii") + b" #9%09d" % len(data) + data + b"\n")

    def _send(self, payload: bytes) -> None:
        # One DEV_DEP_MSG_OUT transfer, even for multi-KB setups: verified with :SYSTem:SETup.
        frame = self._header(DEV_DEP_MSG_OUT, self._next_tag(), len(payload), EOM) + payload
        self.device.write(frame + b"\0" * (-len(frame) % 4))

    def read(self, timeout: float = PACKET_TIMEOUT) -> bytes:
        """Read one complete reply. `timeout` covers the wait for its first packet."""
        reply = bytearray()
        while True:
            # The scope sends one transfer per request: 500 bytes, then the rest, EOM on the last.
            tag = self._next_tag()
            self.device.write(self._header(REQUEST_DEV_DEP_MSG_IN, tag, MAX_TRANSFER))
            received = bytearray(self._packet(timeout if not reply else PACKET_TIMEOUT))
            if len(received) < HEADER_SIZE or received[:3] != bytes([2, tag, ~tag & 0xFF]):
                raise UsbtmcError(f"expected a reply header for bTag {tag}, got {bytes(received[:12])!r}")
            size = struct.unpack_from("<I", received, 4)[0]
            attributes = received[8]
            while len(received) < HEADER_SIZE + size:
                packet = self._packet(PACKET_TIMEOUT)
                if not packet:
                    raise UsbtmcError(f"transfer ended at {len(received)} of {HEADER_SIZE + size} bytes")
                received += packet
            # A transfer that exactly fills its last packet is terminated by a zero-length packet.
            if len(received) % PACKET_SIZE == 0 and self.device.read_packet(PACKET_TIMEOUT):
                raise UsbtmcError("expected a zero-length packet, got data")
            reply += received[HEADER_SIZE : HEADER_SIZE + size]  # drops the scope's pad byte, if any
            if attributes & EOM:
                return bytes(reply)

    def _packet(self, timeout: float) -> bytes:
        packet = self.device.read_packet(timeout)
        if packet is None:
            raise TimeoutError(f"no reply from the scope within {timeout:g} s")
        return packet

    def query_bytes(self, command: str, timeout: float = PACKET_TIMEOUT) -> bytes:
        self.write(command)
        return self.read(timeout)

    def query(self, command: str, timeout: float = PACKET_TIMEOUT) -> str:
        return self.query_bytes(command, timeout).decode("ascii").rstrip("\n")

    def drain(self, max_packets: int = 100_000) -> int:
        """Discard anything still queued in the scope (e.g. after an error); return bytes dropped.

        Never use USBTMC_IOCTL_CLEAR for this: on this scope it times out and halts bulk-OUT.
        """
        dropped = 0
        for _ in range(max_packets):
            packet = self.device.read_packet(MIN_TIMEOUT)
            if packet is None:
                break
            dropped += len(packet)
        return dropped

    def close(self) -> None:
        self.device.close()
