"""Where screenshots and waveforms are saved."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path


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
