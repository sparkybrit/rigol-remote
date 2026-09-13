"""Where screenshots, waveforms and setup backups are saved."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rigol_remote.scope import DS1054Z

MAX_SETUP_BYTES = 64 * 1024  # a DS1054Z setup is ~2 KB


def capture_dir() -> Path:
    # Deliberately not $XDG_DATA_HOME: inside the VS Code snap it points at a per-revision
    # directory (~/snap/code/<rev>/...) that disappears when the snap updates.
    return Path(os.environ.get("RIGOL_CAPTURE_DIR") or Path.home() / ".local/share/rigol-remote/captures")


def save_capture(kind: str, extension: str, data: bytes) -> Path:
    """Write data to <capture dir>/<YYYYmmdd-HHMMSS>-<kind>[-n].<extension> and return the path."""
    directory = capture_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{kind}"
    for n in range(1000):
        path = directory / (f"{stem}.{extension}" if n == 0 else f"{stem}-{n}.{extension}")
        try:
            with open(path, "xb") as f:  # exclusive create: concurrent sessions never clobber each other
                f.write(data)
            return path
        except FileExistsError:
            continue
    raise FileExistsError(f"too many captures named {stem}.* in {directory}")


def backup_setup(scope: DS1054Z) -> Path:
    """Save the whole setup before a risky operation; restoring that file undoes it."""
    return save_capture("setup", "bin", scope.save_setup())


def read_setup(path: str | Path) -> bytes:
    path = Path(path).expanduser()
    if path.stat().st_size > MAX_SETUP_BYTES:
        raise ValueError(f"{path} is too big to be a scope setup")
    return path.read_bytes()
