"""`rigol`: command-line access to the scope. Structured results are JSON on stdout."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from rigol_remote.captures import backup_setup, read_setup, save_capture
from rigol_remote.connection import connect
from rigol_remote.scope import MEASUREMENT_ITEMS, DS1054Z, ScopeError

_SI_PREFIXES = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "M": 1e6, "G": 1e9}


def si_number(text: str) -> float:
    """'0.5', '500m', '500mV', '20ns', '1e-3', '2.5V' → float."""
    m = re.fullmatch(r"\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([pnuµmkMG]?)(?:V|s|Hz)?\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"not a number: {text!r} (e.g. 0.5, 500m, 20n, 2.5V, 1ms)")
    return float(m.group(1)) * _SI_PREFIXES[m.group(2)]


def _probe(text: str) -> float:
    return si_number(text.rstrip("xX"))


def _depth(text: str) -> int | str:
    return "AUTO" if text.strip().upper() == "AUTO" else round(si_number(text))


def _write(data: bytes, output: Path | None, kind: str, extension: str) -> Path:
    if output is None:
        return save_capture(kind, extension, data)
    output.write_bytes(data)
    return output


# --- reading ---

def _screenshot(scope: DS1054Z, args: argparse.Namespace) -> dict:
    png = scope.screenshot_png()
    return {"path": str(_write(png, args.output, "screen", "png")), "bytes": len(png)}


def _waveform(scope: DS1054Z, args: argparse.Namespace) -> dict:
    wave = scope.waveform(args.channel)
    path = _write(wave.csv().encode(), args.output, f"{wave.channel.lower()}", "csv")
    return wave.summary() | {"path": str(path)}


def _save_setup(scope: DS1054Z, args: argparse.Namespace) -> dict:
    return {"path": str(_write(scope.save_setup(), args.output, "setup", "bin"))}


# --- changing ---

def _channel(scope: DS1054Z, args: argparse.Namespace) -> dict:
    return scope.configure_channel(
        args.channel, display=args.display, probe_ratio=args.probe, scale_v_per_div=args.scale,
        offset_v=args.offset, coupling=args.coupling, bw_limit=args.bw_limit, invert=args.invert,
    )


def _trigger(scope: DS1054Z, args: argparse.Namespace) -> dict:
    return scope.configure_trigger(
        sweep=args.sweep, source=args.source, slope=args.slope, level_v=args.level,
        coupling=args.coupling, holdoff_s=args.holdoff,
    )


def _with_backup(action):
    """Save the whole setup first; the result says where, so the change can be undone."""

    def run(scope: DS1054Z, args: argparse.Namespace) -> dict:
        backup = backup_setup(scope)
        return {"backup": str(backup), "result": action(scope, args)}

    return run


def _write_command(scope: DS1054Z, args: argparse.Namespace) -> dict:
    scope.write(args.scpi)
    return {"sent": args.scpi}


COMMANDS = {
    "idn": lambda scope, args: scope.identify(),
    "screenshot": _screenshot,
    "settings": lambda scope, args: scope.settings(),
    "measure": lambda scope, args: scope.measure(args.channel, args.items),
    "waveform": _waveform,
    "query": lambda scope, args: scope.query(args.scpi),
    "save-setup": _save_setup,
    "channel": _channel,
    "timebase": lambda scope, args: scope.configure_timebase(
        scale_s_per_div=args.scale, offset_s=args.offset, mode=args.mode),
    "trigger": _trigger,
    "acquisition": lambda scope, args: scope.configure_acquisition(
        acquisition_type=args.type, averages=args.averages, memory_depth=args.memory_depth),
    "run": lambda scope, args: scope.run(),
    "stop": lambda scope, args: scope.stop(),
    "single": lambda scope, args: scope.single(args.timeout),
    "force": lambda scope, args: scope.force_trigger(),
    "clear-measurements": lambda scope, args: scope.clear_measurements(args.item),
    "restore-setup": _with_backup(lambda scope, args: scope.restore_setup(read_setup(args.file))),
    "autoscale": _with_backup(lambda scope, args: scope.autoscale()),
    "reset": _with_backup(lambda scope, args: scope.reset()),
    "write": _with_backup(_write_command),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rigol", description="Read from and control the Rigol DS1054Z on this host.",
        epilog="Numbers take SI suffixes: 500m, 20n, 2.5V, 1ms. Changes print before/after values.")
    parser.add_argument("--addr", help="device path, e.g. /dev/usbtmc0 (default: $RIGOL_ADDR, else auto-detect)")
    parser.add_argument("--wait", type=float, default=30.0, metavar="S",
                        help="seconds to wait if another process is using the scope (default 30)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # reading
    sub.add_parser("idn", help="identify the scope")
    p = sub.add_parser("screenshot", help="save the screen as PNG")
    p.add_argument("-o", "--output", type=Path, help="file to write (default: the capture directory)")
    sub.add_parser("settings", help="channel, timebase, trigger and acquisition settings")
    p = sub.add_parser("measure", help="automatic measurements on one channel (adds them to the scope's measurement bar)",
                       epilog=f"items: {' '.join(MEASUREMENT_ITEMS)} (long forms like FREQuency work too)")
    p.add_argument("channel", help="1-4 or CHAN1-CHAN4")
    p.add_argument("items", nargs="+", help="e.g. VPP FREQ VRMS")
    p = sub.add_parser("waveform", help="save one channel's on-screen trace as CSV and summarise it")
    p.add_argument("channel", help="1-4 or CHAN1-CHAN4")
    p.add_argument("-o", "--output", type=Path, help="file to write (default: the capture directory)")
    p = sub.add_parser("query", help="send one read-only SCPI query and print the reply")
    p.add_argument("scpi", help="e.g. ':TRIGger:STATus?'")
    p = sub.add_parser("save-setup", help="save the scope's whole setup to a file")
    p.add_argument("-o", "--output", type=Path, help="file to write (default: the capture directory)")

    # changing settings: only the options given are changed
    p = sub.add_parser("channel", help="change a channel's settings")
    p.add_argument("channel", help="1-4 or CHAN1-CHAN4")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--on", dest="display", action="store_const", const=True, help="show the channel")
    g.add_argument("--off", dest="display", action="store_const", const=False, help="hide the channel")
    p.add_argument("--probe", type=_probe, metavar="RATIO", help="probe attenuation, e.g. 1 or 10x")
    p.add_argument("--scale", type=si_number, metavar="V", help="volts per division")
    p.add_argument("--offset", type=si_number, metavar="V", help="vertical offset")
    p.add_argument("--coupling", metavar="AC|DC|GND")
    p.add_argument("--bw-limit", metavar="20M|OFF")
    p.add_argument("--invert", action=argparse.BooleanOptionalAction)
    p = sub.add_parser("timebase", help="change the horizontal settings")
    p.add_argument("--scale", type=si_number, metavar="S", help="seconds per division")
    p.add_argument("--offset", type=si_number, metavar="S", help="horizontal (trigger) position")
    p.add_argument("--mode", metavar="MAIN|XY|ROLL")
    p = sub.add_parser("trigger", help="change the (edge) trigger settings")
    p.add_argument("--sweep", metavar="AUTO|NORMAL|SINGLE")
    p.add_argument("--source", metavar="CH", help="1-4, CHAN1-CHAN4 or AC (line)")
    p.add_argument("--slope", metavar="rising|falling|either")
    p.add_argument("--level", type=si_number, metavar="V")
    p.add_argument("--coupling", metavar="AC|DC|LFR|HFR")
    p.add_argument("--holdoff", type=si_number, metavar="S")
    p = sub.add_parser("acquisition", help="change acquisition type, averaging and memory depth")
    p.add_argument("--type", metavar="NORMAL|AVERAGES|PEAK|HRES")
    p.add_argument("--averages", type=int, metavar="N", help="power of two, 2-1024")
    p.add_argument("--memory-depth", type=_depth, metavar="AUTO|N", help="e.g. AUTO, 12k, 24M")

    # run control
    sub.add_parser("run", help="start continuous acquisition")
    sub.add_parser("stop", help="stop acquiring (the screen freezes)")
    p = sub.add_parser("single", help="arm a single acquisition (overwrites a stopped capture)")
    p.add_argument("--timeout", type=float, default=0.0, metavar="S", help="wait up to S seconds for it to trigger")
    sub.add_parser("force", help="force a trigger")
    p = sub.add_parser("clear-measurements", help="remove items from the scope's measurement bar")
    p.add_argument("--item", type=int, choices=range(1, 6), help="just this item (default: all)")

    # whole-setup changes: each saves a backup of the setup first
    p = sub.add_parser("restore-setup", help="restore a setup saved with save-setup (backs up first)")
    p.add_argument("file", type=Path)
    for name, what in (("autoscale", "let the scope pick scales for the connected signals"),
                       ("reset", "reset the scope to factory defaults (*RST)")):
        p = sub.add_parser(name, help=f"{what} (backs up first)")
        p.add_argument("--yes", action="store_true", required=True, help="confirm: this replaces the whole setup")
    p = sub.add_parser("write", help="send one raw SCPI command (backs up first)")
    p.add_argument("scpi", help="e.g. ':CHANnel1:VERNier ON'")
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
