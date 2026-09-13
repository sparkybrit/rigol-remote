import pytest
from conftest import block

from rigol_remote.scope import ScopeError, Waveform, channel_name, check_read_only_query, measurement_item, parse_block

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 600


def test_parse_block():
    assert parse_block(b"#15hello\n") == b"hello"
    assert parse_block(block(b"x" * 1200)) == b"x" * 1200


@pytest.mark.parametrize("bad", [b"", b"hello", b"#0xyz", b"#15hel"])
def test_parse_block_rejects(bad):
    with pytest.raises(ValueError):
        parse_block(bad)


@pytest.mark.parametrize("given", ["1", "ch1", "CHAN1", "channel1", " CHANnel1 "])
def test_channel_name(given):
    assert channel_name(given) == "CHAN1"


@pytest.mark.parametrize("bad", ["0", "5", "CHAN5", "MATH", "1;*RST"])
def test_channel_name_rejects(bad):
    with pytest.raises(ValueError):
        channel_name(bad)


def test_measurement_item_accepts_short_and_long_forms():
    assert measurement_item("freq") == measurement_item("FREQuency") == ("FREQ", "Hz")
    assert measurement_item("vpp") == ("VPP", "V")
    with pytest.raises(ValueError):
        measurement_item("FREQU")  # SCPI allows only the short or the full long form
    with pytest.raises(ValueError):
        measurement_item("VPP,CHAN1;*RST")


@pytest.mark.parametrize("ok", [":TRIGger:STATus?", "*IDN?", ":MEAS:ITEM? VPP,CHAN1"])
def test_read_only_query_accepts(ok):
    assert check_read_only_query(ok) == ok


@pytest.mark.parametrize("bad", [":RUN", "*RST", ":CHAN1:SCAL 1", ":TRIG:STAT?;:RUN", "*IDN?\n:RUN", "*TST?", ""])
def test_read_only_query_refuses(bad):
    with pytest.raises(ValueError):
        check_read_only_query(bad)


def test_identify(make_scope):
    scope, _ = make_scope({"*IDN?": "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA224210859,00.04.04.SP4\n"})
    assert scope.identify() == {
        "manufacturer": "RIGOL TECHNOLOGIES", "model": "DS1054Z",
        "serial": "DS1ZA224210859", "firmware": "00.04.04.SP4",
    }


def test_measure_maps_invalid_to_none(make_scope):
    scope, fake = make_scope({":MEASure:ITEM? VPP,CHAN2": "1.230000e-01\n", ":MEASure:ITEM? FREQ,CHAN2": "9.9E37\n"})
    assert scope.measure(2, ["vpp", "FREQuency"]) == {
        "channel": "CHAN2",
        "measurements": {"VPP": {"value": 0.123, "unit": "V"}, "FREQ": {"value": None, "unit": "Hz"}},
    }


def test_measure_validates_before_sending_anything(make_scope):
    scope, fake = make_scope({})
    with pytest.raises(ValueError):
        scope.measure(1, ["VPP", "BOGUS"])
    assert fake.commands == []


def test_screenshot(make_scope):
    scope, _ = make_scope({":DISPlay:DATA? ON,OFF,PNG": block(PNG)})
    assert scope.screenshot_png() == PNG


def test_screenshot_rejects_non_png(make_scope):
    scope, _ = make_scope({":DISPlay:DATA? ON,OFF,PNG": block(b"BM" + b"\0" * 100)})
    with pytest.raises(ScopeError):
        scope.screenshot_png()


def test_waveform_scaling():
    # yincrement 8 mV/count, yorigin -33, yreference 127: raw 94 is 0 V, as on the real scope.
    w = Waveform.from_preamble("CHAN1", "0,0,4,1,2.0e-10,7.9852e-05,0,8.0e-03,-33,127", bytes([94, 119, 69, 255]))
    assert w.volts() == pytest.approx([0.0, 0.2, -0.2, 1.288])
    assert w.times() == pytest.approx([7.9852e-05 + i * 2e-10 for i in range(4)])
    s = w.summary()
    assert s["points"] == 4 and s["v_pp"] == pytest.approx(1.488) and s["clipped"] is True
    assert w.csv().splitlines()[0] == "time_s,volts"


def test_waveform_from_scope(make_scope):
    raw = bytes([94] * 1200)
    scope, fake = make_scope({
        ":CHAN1:DISPlay?": "1\n",
        ":WAVeform:PREamble?": "0,0,1200,1,2.0e-10,7.9852e-05,0,8.0e-03,-33,127\n",
        ":WAVeform:DATA?": block(raw),
    })
    w = scope.waveform("1")
    assert w.raw == raw and w.summary()["v_pp"] == 0
    assert fake.commands[1:4] == [":WAVeform:SOURce CHAN1", ":WAVeform:MODE NORMal", ":WAVeform:FORMat BYTE"]


def test_waveform_refuses_channel_that_is_off(make_scope):
    scope, _ = make_scope({":CHAN3:DISPlay?": "0\n"})
    with pytest.raises(ScopeError, match="switched off"):
        scope.waveform(3)


def test_query_summarises_binary_blocks(make_scope):
    scope, _ = make_scope({":DISPlay:DATA?": block(b"BM" + b"\0" * 1000), "*OPC?": "1\n"})
    assert scope.query(":DISPlay:DATA?") == "<binary block, 1002 bytes>"
    assert scope.query("*OPC?") == "1"
