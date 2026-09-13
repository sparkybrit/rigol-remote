import json
from contextlib import contextmanager

import pytest
from conftest import SETUP, block, settings_state

from rigol_remote import cli
from rigol_remote.scope import DS1054Z


@pytest.mark.parametrize("text, value", [
    ("0.5", 0.5), ("500m", 0.5), ("500mV", 0.5), ("20ns", 20e-9), ("20n", 20e-9), ("1e-3", 1e-3),
    ("2.5V", 2.5), ("-1.2", -1.2), ("1ms", 1e-3), ("3u", 3e-6), ("12k", 12e3), ("24M", 24e6), ("1s", 1.0),
])
def test_si_number(text, value):
    assert cli.si_number(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["", "abc", "5x", "1..2", "m"])
def test_si_number_rejects(text):
    with pytest.raises(Exception):
        cli.si_number(text)


def test_single_timeout_does_not_clobber_the_lock_wait():
    args = cli.build_parser().parse_args(["--wait", "7", "single", "--timeout", "2"])
    assert (args.wait, args.timeout) == (7.0, 2.0)


def test_channel_options_parse():
    args = cli.build_parser().parse_args(["channel", "2", "--on", "--scale", "500m", "--probe", "10x", "--no-invert"])
    assert (args.display, args.scale, args.probe, args.invert, args.offset) == (True, 0.5, 10.0, False, None)


def test_reset_and_autoscale_need_yes():
    for command in ("reset", "autoscale"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([command])


@pytest.fixture
def fake_connect(make_scope, monkeypatch, tmp_path):
    """Point the CLI at a fake scope and a temporary capture directory."""
    monkeypatch.setenv("RIGOL_CAPTURE_DIR", str(tmp_path))
    scope, fake = make_scope(replies={":SYSTem:SETup?": block(SETUP)}, state=settings_state())

    @contextmanager
    def connect(address=None, lock_timeout=30.0):
        yield scope

    monkeypatch.setattr(cli, "connect", connect)
    return fake


def run_cli(capsys, *argv) -> tuple[int, dict | str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    try:
        return code, json.loads(out), err
    except json.JSONDecodeError:
        return code, out, err


def test_cli_changes_a_setting(fake_connect, capsys):
    code, out, _ = run_cli(capsys, "timebase", "--scale", "20ns")
    assert code == 0
    assert out["changes"]["scale_s_per_div"] == {"requested": 2e-8, "before": 1e-6, "after": 2e-8}


def test_cli_reports_bad_input_without_a_traceback(fake_connect, capsys):
    code, _, err = run_cli(capsys, "channel", "2", "--coupling", "XX")
    assert code == 1 and "unknown coupling" in err


def test_cli_reset_backs_up_first(fake_connect, capsys, tmp_path):
    fake_connect.effects["*RST"] = lambda f: None
    code, out, _ = run_cli(capsys, "reset", "--yes")
    assert code == 0
    assert open(out["backup"], "rb").read() == SETUP
    assert out["result"]["timebase"]["mode"] == "MAIN"
