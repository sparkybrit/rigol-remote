import os

import pytest

from rigol_remote.connection import ScopeBusy, ScopeNotFound, find_usbtmc, lock, resolve_address
from rigol_remote.scope import ScopeError


def make_sysfs(root, nodes):
    """Lay out /sys/class/usbmisc/usbtmcN → .../<usb device>/<interface>, like the real kernel."""
    for name, (vendor, product) in nodes.items():
        usb_device = root / "devices" / f"usb-{name}"
        interface = usb_device / "1-1:1.0"
        interface.mkdir(parents=True)
        (usb_device / "idVendor").write_text(vendor + "\n")
        (usb_device / "idProduct").write_text(product + "\n")
        node = root / "class/usbmisc" / name
        node.mkdir(parents=True)
        (node / "device").symlink_to(interface)
    return root


def test_find_usbtmc_picks_the_rigol(tmp_path):
    sysfs = make_sysfs(tmp_path, {"usbtmc0": ("0957", "1755"), "usbtmc1": ("1ab1", "04ce")})
    assert find_usbtmc(sysfs) == "/dev/usbtmc1"


def test_find_usbtmc_when_absent(tmp_path):
    with pytest.raises(ScopeNotFound):
        find_usbtmc(make_sysfs(tmp_path, {"usbtmc0": ("0957", "1755")}))


def test_resolve_address(monkeypatch):
    monkeypatch.setenv("RIGOL_ADDR", "/dev/usbtmc3")
    assert resolve_address() == "/dev/usbtmc3"
    assert resolve_address("/dev/usbtmc1") == "/dev/usbtmc1"
    with pytest.raises(ScopeError, match="only USB"):
        resolve_address("10.0.0.50")


def test_lock_excludes_a_second_opener(tmp_path):
    path = tmp_path / "device"
    path.touch()
    first, second = os.open(path, os.O_RDWR), os.open(path, os.O_RDWR)
    try:
        lock(first, timeout=0)
        with pytest.raises(ScopeBusy):
            lock(second, timeout=0.2)
        os.close(first)  # closing releases the lock
        first = None
        lock(second, timeout=0)
    finally:
        for fd in (first, second):
            if fd is not None:
                os.close(fd)
