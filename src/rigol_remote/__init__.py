"""Control a Rigol DS1054Z oscilloscope over USB from Claude Code (CLI + MCP server)."""

from rigol_remote.connection import ScopeBusy, ScopeNotFound, connect
from rigol_remote.scope import DS1054Z, ScopeError, Waveform

__all__ = ["DS1054Z", "ScopeBusy", "ScopeError", "ScopeNotFound", "Waveform", "connect"]
