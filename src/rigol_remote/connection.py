"""Finding the scope, and making sure only one process talks to it at a time.

Every Claude Code session runs its own copy of the MCP server, so they all contend for the same
/dev/usbtmcN. Interleaved USBTMC exchanges corrupt each other, so each connection holds an
exclusive flock on the device node for as long as it's open.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rigol_remote.scope import DS1054Z, ScopeError
from rigol_remote.usbtmc import KernelUsbtmc, UsbtmcTransport

RIGOL_VENDOR_ID = "1ab1"
DS1000Z_PRODUCT_ID = "04ce"


class ScopeNotFound(ScopeError):
    pass


class ScopeBusy(ScopeError):
    pass


def find_usbtmc(sysfs: Path = Path("/sys")) -> str:
    """Device path of the first USBTMC node that is a Rigol DS1000Z."""
    for node in sorted((sysfs / "class/usbmisc").glob("usbtmc*")):
        usb_device = (node / "device").resolve().parent  # the interface's parent is the USB device
        try:
            vendor = (usb_device / "idVendor").read_text().strip()
            product = (usb_device / "idProduct").read_text().strip()
        except OSError:
            continue
        if (vendor, product) == (RIGOL_VENDOR_ID, DS1000Z_PRODUCT_ID):
            return f"/dev/{node.name}"
    raise ScopeNotFound("no Rigol DS1000Z on USB (is it switched on and plugged in?)")


def resolve_address(address: str | None = None) -> str:
    address = address or os.environ.get("RIGOL_ADDR")
    if not address:
        return find_usbtmc()
    if address.startswith("/dev/"):
        return address
    raise ScopeError(f"unsupported address {address!r}: only USB (/dev/usbtmcN) is implemented so far")


def lock(fd: int, timeout: float) -> None:
    """Take an exclusive flock on fd, waiting up to `timeout` seconds for other users to finish."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise ScopeBusy(f"the scope is still in use by another process after {timeout:g} s") from None
            time.sleep(0.05)


@contextmanager
def connect(address: str | None = None, lock_timeout: float = 30.0) -> Iterator[DS1054Z]:
    """Open the scope exclusively; the lock is released when the block exits."""
    path = resolve_address(address)
    try:
        device = KernelUsbtmc(path)
    except FileNotFoundError:
        raise ScopeNotFound(f"{path} does not exist (is the scope plugged in?)") from None
    except PermissionError:
        raise ScopeError(
            f"no permission to open {path}: the udev rule in /etc/udev/rules.d/99-rigol.rules "
            "should make it group plugdev (see CLAUDE.md in the rigol-remote project)"
        ) from None
    try:
        lock(device.fd, lock_timeout)  # released when the fd is closed
        transport = UsbtmcTransport(device)
        transport.drain()  # a previous user may have died mid-reply
        try:
            yield DS1054Z(transport)
        except BaseException:
            try:
                transport.drain()  # leave nothing half-read for the next user
            except OSError:
                pass
            raise
    finally:
        device.close()
