import pytest
from conftest import SETUP, block, sent_commands, settings_state

from rigol_remote.scope import ScopeError, check_command


# --- configure_* ---

def test_configure_channel_orders_changes_and_reads_back_snapped_values(make_scope):
    scope, fake = make_scope(state=settings_state())
    fake.snap[":CHANnel2:SCALe"] = lambda v: "5.000000e-01"  # the scope only offers 1-2-5 steps
    result = scope.configure_channel("2", offset_v=-1.2, scale_v_per_div=0.3, probe_ratio=1, display=True)
    assert result["channel"] == "CHAN2"
    assert result["changes"]["scale_v_per_div"] == {"requested": 0.3, "before": 1.0, "after": 0.5}
    assert result["changes"]["display"] == {"requested": True, "before": False, "after": True}
    # probe ratio rescales V/div, and the offset range depends on V/div, so the order matters
    assert sent_commands(fake) == [
        ":CHANnel2:DISPlay 1", ":CHANnel2:PROBe 1", ":CHANnel2:SCALe 0.3", ":CHANnel2:OFFSet -1.2",
    ]


def test_before_values_are_a_true_undo_even_when_changes_interact(make_scope):
    scope, fake = make_scope(state=settings_state())

    def set_probe(value):  # like the scope: a new probe ratio rescales V/div to keep the trace
        old = float(fake.state[":CHANnel2:PROBe"])
        fake.state[":CHANnel2:SCALe"] = f"{float(fake.state[':CHANnel2:SCALe']) * float(value) / old:e}"
        return value

    fake.snap[":CHANnel2:PROBe"] = set_probe
    result = scope.configure_channel(2, probe_ratio=1, scale_v_per_div=0.5)
    assert result["changes"]["scale_v_per_div"]["before"] == 1.0  # not the rescaled 0.1
    scope.configure_channel(2, **{k: v["before"] for k, v in result["changes"].items()})
    assert (fake.state[":CHANnel2:PROBe"], float(fake.state[":CHANnel2:SCALe"])) == ("10", 1.0)


@pytest.mark.parametrize("bad", [
    dict(coupling="XX"), dict(probe_ratio=3), dict(scale_v_per_div=float("nan")),
    dict(display="yes"), dict(bw_limit="100M"), dict(offset_v=float("inf")),
])
def test_configure_channel_validates_everything_before_sending(make_scope, bad):
    scope, fake = make_scope(state=settings_state())
    with pytest.raises(ValueError):
        scope.configure_channel(2, **({"scale_v_per_div": 0.5} | bad))  # a valid change alongside
    assert fake.commands == []


def test_nothing_to_change_is_an_error(make_scope):
    scope, _ = make_scope(state=settings_state())
    with pytest.raises(ValueError, match="nothing to change"):
        scope.configure_timebase()


def test_scope_errors_are_raised_with_the_values_now_in_effect(make_scope):
    scope, fake = make_scope(state=settings_state())

    def reject(value):
        fake.errors.append('-224,"Illegal parameter value"')
        return "1.000000e-06"

    fake.snap[":TIMebase:MAIN:SCALe"] = reject
    with pytest.raises(ScopeError, match="Illegal parameter value.*'after': 1e-06"):
        scope.configure_timebase(scale_s_per_div=1e3)


def test_errors_left_by_someone_else_are_not_blamed_on_the_change(make_scope):
    scope, fake = make_scope(state=settings_state())
    fake.errors.append('-113,"Undefined header"')
    result = scope.configure_timebase(scale_s_per_div=2e-6, offset_s=1e-7)
    assert result["changes"]["scale_s_per_div"]["after"] == 2e-6


def test_configure_trigger_switches_to_edge_and_sets_level_after_source(make_scope):
    scope, fake = make_scope(state=settings_state() | {":TRIGger:MODE": "PULS"})
    result = scope.configure_trigger(level_v=1.5, slope="falling", source="ch2", sweep="normal")
    assert sent_commands(fake) == [
        ":TRIGger:MODE EDGE", ":TRIGger:EDGe:SOURce CHAN2", ":TRIGger:EDGe:SLOPe NEG",
        ":TRIGger:EDGe:LEVel 1.5", ":TRIGger:SWEep NORM",
    ]
    assert result["changes"]["mode"] == {"requested": "EDGE", "before": "PULS", "after": "EDGE"}


def test_configure_trigger_sweep_alone_leaves_the_mode_alone(make_scope):
    scope, fake = make_scope(state=settings_state() | {":TRIGger:MODE": "PULS"})
    scope.configure_trigger(sweep="SINGle", coupling="hfreject", holdoff_s=1e-6)
    assert sent_commands(fake) == [":TRIGger:COUPling HFR", ":TRIGger:HOLDoff 1e-06", ":TRIGger:SWEep SING"]


def test_trigger_source_can_be_the_mains(make_scope):
    scope, fake = make_scope(state=settings_state())
    scope.configure_trigger(source="line")
    assert ":TRIGger:EDGe:SOURce AC" in sent_commands(fake)


def test_configure_acquisition(make_scope):
    scope, fake = make_scope(state=settings_state())
    result = scope.configure_acquisition(acquisition_type="average", averages=16, memory_depth=12000)
    assert sent_commands(fake) == [":ACQuire:TYPE AVER", ":ACQuire:AVERages 16", ":ACQuire:MDEPth 12000"]
    assert result["changes"]["memory_depth"] == {"requested": 12000, "before": "AUTO", "after": 12000}
    scope.configure_acquisition(memory_depth="auto")
    assert sent_commands(fake)[-1] == ":ACQuire:MDEPth AUTO"


@pytest.mark.parametrize("bad", [dict(averages=3), dict(averages=2048), dict(memory_depth=5000), dict(acquisition_type="FAST")])
def test_configure_acquisition_validates(make_scope, bad):
    scope, fake = make_scope(state=settings_state())
    with pytest.raises(ValueError):
        scope.configure_acquisition(**bad)
    assert fake.commands == []


# --- run control ---

def test_run_and_stop_report_the_resulting_state(make_scope):
    scope, fake = make_scope(state={":TRIGger:STATus": "STOP", ":TRIGger:SWEep": "AUTO"})
    fake.effects[":RUN"] = lambda f: f.state.update({":TRIGger:STATus": "AUTO"})
    fake.effects[":STOP"] = lambda f: f.state.update({":TRIGger:STATus": "STOP"})
    assert scope.run() == {"status": "AUTO", "sweep": "AUTO"}
    assert scope.stop() == {"status": "STOP", "sweep": "AUTO"}


def test_single_waits_for_the_capture(make_scope, monkeypatch):
    monkeypatch.setattr("rigol_remote.scope.time.sleep", lambda s: None)
    statuses = iter(["WAIT", "WAIT", "TD", "STOP"])
    scope, fake = make_scope(state={":TRIGger:STATus": lambda: next(statuses), ":TRIGger:SWEep": "SING"})
    fake.effects[":SINGle"] = lambda f: None
    assert scope.single(wait_s=5) == {"status": "STOP", "sweep": "SING", "captured": True}


def test_single_without_waiting_returns_once_armed(make_scope):
    scope, fake = make_scope(state={":TRIGger:STATus": "WAIT", ":TRIGger:SWEep": "SING"})
    fake.effects[":SINGle"] = lambda f: None
    assert scope.single() == {"status": "WAIT", "sweep": "SING", "captured": False}


def test_stale_stop_is_not_mistaken_for_a_capture(make_scope, monkeypatch):
    # The status lags the command: a scope that was stopped still reads STOP for ~100 ms.
    monkeypatch.setattr("rigol_remote.scope.time.sleep", lambda s: None)
    statuses = iter(["STOP", "STOP", "RUN"])
    scope, fake = make_scope(state={":TRIGger:STATus": lambda: next(statuses), ":TRIGger:SWEep": "SING"})
    fake.effects[":SINGle"] = lambda f: None
    assert scope.single() == {"status": "RUN", "sweep": "SING", "captured": False}


def test_stop_waits_out_the_status_lag(make_scope, monkeypatch):
    monkeypatch.setattr("rigol_remote.scope.time.sleep", lambda s: None)
    statuses = iter(["TD", "TD", "STOP"])
    scope, fake = make_scope(state={":TRIGger:STATus": lambda: next(statuses), ":TRIGger:SWEep": "AUTO"})
    fake.effects[":STOP"] = lambda f: None
    assert scope.stop() == {"status": "STOP", "sweep": "AUTO"}


# --- whole setup, measurement bar, raw writes ---

def test_save_and_restore_setup(make_scope):
    scope, fake = make_scope(replies={":SYSTem:SETup?": block(SETUP)}, state=settings_state())
    assert scope.save_setup() == SETUP
    settings = scope.restore_setup(SETUP)
    assert fake.blocks[":SYSTem:SETup"] == SETUP
    assert settings["timebase"]["scale_s_per_div"] == 1e-6


def test_restore_setup_refuses_data_that_is_not_a_setup(make_scope):
    scope, fake = make_scope()
    with pytest.raises(ValueError, match="not a DS1054Z setup"):
        scope.restore_setup(b"\x89PNG\r\n\x1a\n" + bytes(100))
    assert fake.commands == []


def test_reset_returns_the_new_settings(make_scope):
    scope, fake = make_scope(state=settings_state() | {":TIMebase:MAIN:SCALe": "5.000000e-08"})
    fake.effects["*RST"] = lambda f: f.state.update({":TIMebase:MAIN:SCALe": "1.000000e-06"})
    assert scope.reset()["timebase"]["scale_s_per_div"] == 1e-6


def test_clear_measurements(make_scope):
    scope, fake = make_scope()
    fake.effects[":MEASure:CLEar ALL"] = fake.effects[":MEASure:CLEar ITEM3"] = lambda f: None
    assert scope.clear_measurements() == {"cleared": "ALL"}
    assert scope.clear_measurements(3) == {"cleared": "ITEM3"}
    with pytest.raises(ValueError):
        scope.clear_measurements(6)


def test_write_raises_when_the_scope_reports_an_error(make_scope):
    scope, fake = make_scope()
    fake.effects[":CHANnel1:VERNier ON"] = lambda f: None
    scope.write(":CHANnel1:VERNier ON")
    with pytest.raises(ScopeError, match="Undefined header"):
        scope.write(":BOGus:COMMand 1")


@pytest.mark.parametrize("ok", [":CHANnel1:VERNier ON", "*RST", ":RUN"])
def test_check_command_accepts(ok):
    assert check_command(ok) == ok


@pytest.mark.parametrize("bad", ["*IDN?", ":TRIG:STAT?", ":RUN;:STOP", ":RUN\n:STOP", ""])
def test_check_command_refuses(bad):
    with pytest.raises(ValueError):
        check_command(bad)
