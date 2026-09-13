"""`rigol`: command-line access to the scope. Structured results are JSON on stdout."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rigol_remote.captures import save_capture
from rigol_remote.connection import connect
from rigol_remote.scope import MEASUREMENT_ITEMS, DS1054Z, ScopeError


def _write(data: bytes, output: Path | None, kind: str, extension: str) -> Path:
    if output is None:
        return save_capture(kind, extension, data)
    output.write_bytes(data)
    return output


def _screenshot(scope: DS1054Z, args: argparse.Namespace) -> dict:
    png = scope.screenshot_png()
    return {"path": str(_write(png, args.output, "screen", "png")), "bytes": len(png)}


def _waveform(scope: DS1054Z, args: argparse.Namespace) -> dict:
    wave = scope.waveform(args.channel)
    path = _write(wave.csv().encode(), args.output, f"{wave.channel.lower()}", "csv")
    return wave.summary() | {"path": str(path)}


COMMANDS = {
    "idn": lambda scope, args: scope.identify(),
    "screenshot": _screenshot,
    "settings": lambda scope, args: scope.settings(),
    "measure": lambda scope, args: scope.measure(args.channel, args.items),
    "waveform": _waveform,
    "query": lambda scope, args: scope.query(args.scpi),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rigol", description="Read from the Rigol DS1054Z on this host.")
    parser.add_argument("--addr", help="device path, e.g. /dev/usbtmc0 (default: $RIGOL_ADDR, else auto-detect)")
    parser.add_argument("--wait", type=float, default=30.0, metavar="S",
                        help="seconds to wait if another process is using the scope (default 30)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("idn", help="identify the scope")
    p = sub.add_parser("screenshot", help="save the screen as PNG")
    p.add_argument("-o", "--output", type=Path, help="file to write (default: the capture directory)")
    sub.add_parser("settings", help="channel, timebase, trigger and acquisition settings")
    p = sub.add_parser("measure", help="automatic measurements on one channel",
                       epilog=f"items: {' '.join(MEASUREMENT_ITEMS)} (long forms like FREQuency work too)")
    p.add_argument("channel", help="1-4 or CHAN1-CHAN4")
    p.add_argument("items", nargs="+", help="e.g. VPP FREQ VRMS")
    p = sub.add_parser("waveform", help="save one channel's on-screen trace as CSV and summarise it")
    p.add_argument("channel", help="1-4 or CHAN1-CHAN4")
    p.add_argument("-o", "--output", type=Path, help="file to write (default: the capture directory)")
    p = sub.add_parser("query", help="send one read-only SCPI query and print the reply")
    p.add_argument("scpi", help="e.g. ':TRIGger:STATus?'")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with connect(args.addr, lock_timeout=args.wait) as scope:
            result = COMMANDS[args.command](scope, args)
    except (ScopeError, OSError, ValueError) as e:
        print(f"rigol: {e}", file=sys.stderr)
        return 1
    print(result if isinstance(result, str) else json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
