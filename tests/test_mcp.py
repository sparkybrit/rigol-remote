from contextlib import contextmanager

import pytest
from conftest import SETUP, block, settings_state
from mcp.server.mcpserver.exceptions import ToolError

from rigol_remote import mcp_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fake(make_scope, monkeypatch, tmp_path):
    monkeypatch.setenv("RIGOL_CAPTURE_DIR", str(tmp_path))
    scope, fake = make_scope(replies={":SYSTem:SETup?": block(SETUP)}, state=settings_state())

    @contextmanager
    def connect(address=None, lock_timeout=30.0):
        yield scope

    monkeypatch.setattr(mcp_server, "connect", connect)
    return fake


async def test_configure_channel_tool(fake):
    result = await mcp_server.configure_channel("1", scale_v_per_div=0.5, coupling="AC")
    assert result["changes"]["coupling"] == {"requested": "AC", "before": "DC", "after": "AC"}


async def test_bad_input_becomes_a_tool_error_with_the_reason(fake):
    with pytest.raises(ToolError, match="probe ratio must be one of"):
        await mcp_server.configure_channel("1", probe_ratio=3)


async def test_run_control_dispatches(fake):
    fake.effects[":STOP"] = lambda f: f.state.update({":TRIGger:STATus": "STOP"})
    assert await mcp_server.run_control("stop") == {"status": "STOP", "sweep": "AUTO"}


async def test_restore_setup_backs_up_first_and_checks_the_file(fake, tmp_path):
    saved = tmp_path / "mine.bin"
    saved.write_bytes(SETUP)
    result = await mcp_server.restore_setup(str(saved))
    assert open(result["backup"], "rb").read() == SETUP
    assert fake.blocks[":SYSTem:SETup"] == SETUP
    with pytest.raises(ToolError, match="No such file"):
        await mcp_server.restore_setup(str(tmp_path / "missing.bin"))


async def test_scpi_write_refuses_queries(fake):
    with pytest.raises(ToolError, match="that's a query"):
        await mcp_server.scpi_write("*IDN?")


async def test_tools_are_registered_with_the_right_hints():
    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
    read_only = {"identify", "screenshot", "get_settings", "get_waveform", "scpi_query", "save_setup"}
    destructive = {"run_control", "clear_measurements", "restore_setup", "autoscale", "reset", "scpi_write"}
    assert {n for n, t in tools.items() if t.annotations.read_only_hint} == read_only
    assert {n for n, t in tools.items() if t.annotations.destructive_hint} == destructive
    assert set(tools) == read_only | destructive | {
        "measure", "configure_channel", "configure_timebase", "configure_trigger", "configure_acquisition"}
