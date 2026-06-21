"""Phase 5 — packaging acceptance: the example agent completes a
read -> write -> time-travel task end to end through the real MCP dispatch."""
import asyncio
import os
import sys

import pytest

os.environ.setdefault("FILEENGINE_MCP_USER", "testuser")
os.environ.setdefault("FILEENGINE_MCP_PASSWORD", "password")
os.environ.setdefault("FILEENGINE_MCP_TENANT", "default")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples"))


def _services_up() -> bool:
    try:
        from fileengine_mcp.config import Config
        from fileengine_mcp.ldap_auth import authenticate
        cfg = Config()
        return authenticate(cfg, cfg.agent_user, cfg.agent_password).authenticated
    except Exception:
        return False


@pytest.mark.skipif(not _services_up(), reason="LDAP/core not reachable")
def test_example_agent_recovers_via_time_travel():
    import agent_demo
    from fileengine_mcp import server
    result = asyncio.run(agent_demo.run(server))
    assert result["recovered_ok"] is True
    assert result["time_travelled"] == result["good"]      # old version readable
    assert result["clobbered"] != result["good"]           # it really was clobbered
    assert result["recovered"] == result["good"]           # restored via tools
    assert result["history_len"] >= 3                       # nothing culled
