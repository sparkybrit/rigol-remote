"""Changes the real scope's settings and puts them back: uv run pytest -m hardware_write.

Each test undoes its own changes by passing the returned before-values back. As a safety net, the
module saves the whole setup first, restores it at the end (~6 s) and checks nothing was left
changed. reset/autoscale only run with RIGOL_ALLOW_RESET=1.
"""

import os

import pytest

from rigol_remote.connection import connect
from rigol_remote.scope import ScopeError

pytestmark = pytest.mark.hardware_write


def comparable(settings: dict) -> dict:
    """Settings minus the live trigger status."""
    return settings | {"trigger": {k: v for k, v in settings["trigger"].items() if k != "status"}}


def before(result: dict) -> dict:
    """The arguments that undo a configure_* result (mode is implied by the edge-trigger settings)."""
    return {k: v["before"] for k, v in result["changes"].items() if k != "mode"}


@pytest.fixture(scope="module", autouse=True)
def original():
    with connect() as scope:
        setup, settings, status = scope.save_setup(), scope.settings(), scope.query(":TRIGger:STATus?")
    yield settings
    with connect() as scope:
        scope.restore_setup(setup)
        if status != "STOP":
            scope.run()
        assert comparable(scope.settings()) == comparable(settings)


def test_channel_changes_and_undo(original):
    with connect() as scope:
        r = scope.configure_channel(2, display=True, probe_ratio=1, scale_v_per_div=0.5, offset_v=0.2,
                                    coupling="AC", bw_limit="20M", invert=True)
        c = r["changes"]
        assert (c["display"]["after"], c["probe_ratio"]["after"], c["scale_v_per_div"]["after"]) == (True, 1, 0.5)
        assert c["offset_v"]["after"] == pytest.approx(0.2, abs=0.02)
        assert (c["coupling"]["after"], c["bw_limit"]["after"], c["invert"]["after"]) == ("AC", "20M", True)
        scope.configure_channel(2, **before(r))
        assert scope.settings()["channels"]["CHAN2"] == original["channels"]["CHAN2"]


def test_values_snap_to_what_the_scope_supports():
    with connect() as scope:
        r = scope.configure_timebase(scale_s_per_div=3e-8)
        scope.configure_timebase(**before(r))
    after = r["changes"]["scale_s_per_div"]["after"]
    print(f"timebase 30 ns/div requested → {after}")
    assert after in (2e-8, 3e-8, 5e-8)


def test_timebase_changes_and_undo(original):
    with connect() as scope:
        r = scope.configure_timebase(scale_s_per_div=1e-7, offset_s=1e-7)
        assert (r["changes"]["scale_s_per_div"]["after"], r["changes"]["offset_s"]["after"]) == (1e-7, 1e-7)
        scope.configure_timebase(**before(r))
        assert scope.settings()["timebase"] == original["timebase"]


def test_trigger_changes_and_undo(original):
    with connect() as scope:
        r = scope.configure_trigger(level_v=1.0, slope="falling", coupling="HFREJECT", holdoff_s=1e-6, sweep="NORMAL")
        c = r["changes"]
        assert c["level_v"]["after"] == pytest.approx(1.0, abs=0.1)
        assert (c["slope"]["after"], c["coupling"]["after"], c["sweep"]["after"]) == ("NEG", "HFR", "NORM")
        assert c["holdoff_s"]["after"] == pytest.approx(1e-6)
        scope.configure_trigger(**before(r))
        assert comparable(scope.settings())["trigger"] == comparable(original)["trigger"]


def test_acquisition_changes_and_undo(original):
    with connect() as scope:
        r = scope.configure_acquisition(acquisition_type="AVERAGES", averages=16, memory_depth=12000)
        c = r["changes"]
        assert (c["acquisition_type"]["after"], c["averages"]["after"], c["memory_depth"]["after"]) == ("AVER", 16, 12000)
        scope.configure_acquisition(**before(r))
        assert scope.settings()["acquire"] == original["acquire"]


def test_run_control(original):
    with connect() as scope:
        assert scope.stop()["status"] == "STOP"
        assert scope.run()["status"] != "STOP"
        scope.force_trigger()
        single = scope.single(wait_s=3)
        assert single["captured"] and single["sweep"] == "SING"
        scope.configure_trigger(sweep=original["trigger"]["sweep"])
        scope.run()


def test_raw_write():
    with connect() as scope:
        scope.write(":CHANnel2:VERNier ON")
        assert scope.query(":CHANnel2:VERNier?") == "1"
        scope.write(":CHANnel2:VERNier OFF")
        with pytest.raises(ScopeError, match="reported"):
            scope.write(":BOGus:COMMand 1")


@pytest.mark.skipif(os.environ.get("RIGOL_ALLOW_RESET") != "1",
                    reason="wipes the setup (restored afterwards); set RIGOL_ALLOW_RESET=1")
def test_reset_and_autoscale():
    with connect() as scope:
        assert scope.reset()["timebase"]["scale_s_per_div"] == 1e-6  # factory default
        assert scope.autoscale()["channels"]["CHAN1"]["display"]
