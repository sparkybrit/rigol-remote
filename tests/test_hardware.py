"""Against the real scope: uv run pytest -m hardware. All read-only."""

import threading

import pytest

from rigol_remote.connection import ScopeBusy, connect

pytestmark = pytest.mark.hardware


def test_identify():
    with connect() as scope:
        assert scope.identify()["model"] == "DS1054Z"
        assert scope.errors() == []


def test_screenshot_is_png():
    with connect() as scope:
        png = scope.screenshot_png()
    assert png.startswith(b"\x89PNG") and len(png) > 10_000


def test_settings_raise_no_scope_errors():
    with connect() as scope:
        s = scope.settings()
        assert scope.errors() == []
    assert set(s["channels"]) == {"CHAN1", "CHAN2", "CHAN3", "CHAN4"}
    assert s["timebase"]["scale_s_per_div"] > 0


def test_waveform_of_first_displayed_channel():
    with connect() as scope:
        on = [c for c, v in scope.settings()["channels"].items() if v["display"]]
        if not on:
            pytest.skip("no channel is switched on")
        w = scope.waveform(on[0])
        assert scope.errors() == []
    assert len(w.raw) == 1200


def test_measure_returns_values_or_none():
    with connect() as scope:
        m = scope.measure(1, ["VPP", "VAVG", "FREQ"])
        assert scope.errors() == []
    assert set(m["measurements"]) == {"VPP", "VAVG", "FREQ"}


def test_second_connection_waits_for_the_first():
    with connect():
        errors = []

        def contend():
            try:
                with connect(lock_timeout=0.3):
                    pass
            except ScopeBusy as e:
                errors.append(e)

        t = threading.Thread(target=contend)
        t.start()
        t.join()
    assert len(errors) == 1
    with connect(lock_timeout=0.3) as scope:  # and it's free again afterwards
        scope.identify()
